"""Push employees from Frappe to BioTime. Frappe is the master for employee records.

Saving an employee queues a push after the save commits, so a save never waits for BioTime
or fails because of it. A push brings the employee's copy on each server up to date: the
person with their code, their names, and their department, area and position, created on
the server when missing. An employee who left is resigned or deleted, as the server says.
BioTime people with no Frappe employee are never touched.
"""

import contextlib
import json
from collections import Counter

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, today
from pybiotime import APIError, BioTimeError, ResignType
from pybiotime.compat import resign_api_from_error
from redis.exceptions import LockError
from rq.timeouts import JobTimeoutException

from frappe_biotime.biotime import connection
from frappe_biotime.biotime.punches import _describe, _save_status

#: The Employee fields BioTime holds a copy of. A change to one of them is pushed.
SYNCED_FIELDS = (
	"attendance_device_id",
	"first_name",
	"middle_name",
	"last_name",
	"department",
	"branch",
	"designation",
	"status",
	"company",
)
#: How many times one job pushes an employee that keeps changing while it is pushed.
MAX_ROUNDS = 3

# What a server does with an employee who left: the options of On Employee Left.
RESIGN = "Resign in BioTime"
DELETE = "Delete in BioTime"
NOTHING = "Do nothing"


class _Skip(Exception):
	"""The employee cannot be pushed to this server; the message says why."""


def on_employee_update(doc, method=None) -> None:
	"""Employee on_update: queue a push when something BioTime holds changed."""
	if any(doc.has_value_changed(fieldname) for fieldname in SYNCED_FIELDS) and _servers():
		enqueue_push(doc.name)


def enqueue_push(employee: str) -> None:
	frappe.enqueue(
		"frappe_biotime.biotime.employees.push_employee",
		job_id=f"biotime:push:{employee}",
		deduplicate=True,
		enqueue_after_commit=True,
		employee=employee,
	)


def enqueue_push_all(server: str) -> None:
	frappe.enqueue(
		"frappe_biotime.biotime.employees.push_all",
		queue="long",
		job_id=f"biotime:push-all:{server}",
		deduplicate=True,
		server=server,
	)


def push_employee(employee: str) -> None:
	"""Job: bring the employee's copy on every pushing server up to date."""
	lock = _lock(employee)
	if not lock.acquire(blocking=True, blocking_timeout=120):
		raise LockError(f"Another push of {employee} did not finish.")
	try:
		for _round in range(MAX_ROUNDS):
			# Read what is committed now. A save made while this job runs is not queued again,
			# because the job counts as queued, so push until the employee stops changing.
			frappe.db.commit()  # nosemgrep
			if not frappe.db.exists("Employee", employee):
				return
			doc = frappe.get_doc("Employee", employee)
			for server in _servers():
				_push_and_report(frappe.get_doc("BioTime Server", server), doc)
			frappe.db.commit()  # nosemgrep
			if frappe.db.get_value("Employee", employee, "modified") == doc.modified:
				return
	finally:
		with contextlib.suppress(LockError):
			lock.release()


def push_all(server: str) -> None:
	"""Job: push every employee with an Attendance Device ID to `server`."""
	server = frappe.get_doc("BioTime Server", server)
	filters = {"attendance_device_id": ["is", "set"]}
	if server.company:
		filters["company"] = server.company
	counts = Counter()
	problems = []
	# One Error Log per run is enough to find the cause.
	logged = False
	try:
		with connection.connect(server) as client:
			for name in frappe.get_all("Employee", filters=filters, pluck="name", order_by="name asc"):
				lock = _lock(name)
				if not lock.acquire(blocking=False):
					# Its own push is running and brings it up to date.
					counts[_("pushed by their own job")] += 1
					continue
				try:
					outcome = _push(server, client, frappe.get_doc("Employee", name))
					counts[outcome] += 1
				except JobTimeoutException:
					raise
				except Exception as exc:
					if not isinstance(exc, _Skip) and not logged:
						frappe.log_error(title=f"BioTime push failed for {server.name}")
						logged = True
					counts[_("not pushed")] += 1
					problems.append(f"{name}: {exc if isinstance(exc, _Skip) else _describe(exc)}")
				finally:
					with contextlib.suppress(LockError):
						lock.release()
	except BioTimeError as exc:
		problems.insert(0, _("BioTime could not be reached: {0}").format(exc))
	parts = [", ".join(f"{count} {outcome}" for outcome, count in counts.most_common()) or _("No employees.")]
	parts.extend(problems[:20])
	_save_status(server.name, last_push_at=now_datetime(), last_push_message="\n".join(parts)[:1000])


def _push_and_report(server, employee) -> None:
	"""Push `employee` to `server` and keep what happened in the server's status."""
	if not _pushes(server, employee):
		return
	try:
		with connection.connect(server) as client:
			message = f"{employee.name}: {_push(server, client, employee)}"
	except _Skip as exc:
		message = f"{employee.name}: {exc}"
	except JobTimeoutException:
		raise
	except Exception as exc:
		# Logged and reported, so the other servers are still pushed.
		frappe.log_error(
			title=f"BioTime push failed for {server.name}",
			reference_doctype="Employee",
			reference_name=employee.name,
		)
		message = f"{employee.name}: {_describe(exc)}"
	_save_status(server.name, last_push_at=now_datetime(), last_push_message=message[:1000])


def _pushes(server, employee) -> bool:
	"""Whether `server` holds a copy of `employee`."""
	if server.company and employee.company != server.company:
		return False
	# No code, no BioTime person. Removing a code does not remove the person.
	return bool((employee.attendance_device_id or "").strip())


def _push(server, client, employee) -> str:
	"""Bring `server`'s copy of `employee` up to date. Returns what happened."""
	code = employee.attendance_device_id.strip()
	person, old_code = _find(client, employee, code)
	if employee.status == "Left":
		return _leave(server, client, employee, person)
	area = _area(server, client, employee)
	values = {
		"department_id": _department(server, client, employee),
		"position_id": _position(server, client, employee),
		"first_name": " ".join(filter(None, (employee.first_name, employee.middle_name))) or None,
		"last_name": employee.last_name or None,
	}
	if person is None:
		client.employees.create(code, area_ids=[area], **values)
		return _("created in BioTime")
	# Areas decide which terminals know a person: add theirs, and take none away.
	values["area_ids"] = sorted({area, *(existing.id for existing in person.area)})
	outcome = _("up to date in BioTime")
	if old_code:
		try:
			client.employees.update(person.id, emp_code=code)
		except APIError as exc:
			# BioTime 9.5 never changes a code. A second person under the new code would
			# have no fingerprints, and terminals would keep sending the old code.
			raise _Skip(
				_(
					"BioTime has this person under code {0} and did not change it to {1} ({2}). "
					"Change it in BioTime, or set the Attendance Device ID back to {0}."
				).format(old_code, code, exc)
			) from None
		outcome = _("code changed in BioTime from {0}").format(old_code)
	client.employees.upsert(code, **values)
	if server.on_employee_left == RESIGN and _reinstate(client, person):
		outcome = _("reinstated in BioTime")
	return outcome


def _find(client, employee, code: str) -> tuple:
	"""The BioTime person of `employee`, and the old code they were found under, if any."""
	person = client.employees.get_by_code(code)
	if person is not None:
		return person, None
	for old_code in _old_codes(employee.name, code):
		# Another employee's code now: that person is theirs.
		if frappe.db.exists("Employee", {"attendance_device_id": old_code}):
			continue
		person = client.employees.get_by_code(old_code)
		if person is not None:
			return person, old_code
	return None, None


def _old_codes(employee: str, current: str) -> list[str]:
	"""The codes `employee` had before, newest first, from its change history."""
	codes = []
	for data in frappe.get_all(
		"Version",
		filters={"ref_doctype": "Employee", "docname": employee, "data": ["like", "%attendance_device_id%"]},
		pluck="data",
		order_by="creation desc",
		limit=20,
	):
		for fieldname, old, _new in json.loads(data).get("changed", []):
			old = (old or "").strip()
			if fieldname == "attendance_device_id" and old and old != current and old not in codes:
				codes.append(old)
	return codes


def _leave(server, client, employee, person) -> str:
	if person is None:
		return _("left, and not in BioTime")
	if server.on_employee_left == DELETE:
		client.employees.delete(person.id)
		return _("left, so deleted from BioTime")
	if server.on_employee_left != RESIGN:
		return _("left, and kept in BioTime as the server says")
	resigns = _resigns(client, person)
	if resigns is None:
		raise _Skip(_("left, but this BioTime version cannot resign people. Resign them in BioTime."))
	if resigns:
		return _("left, and already resigned in BioTime")
	client.resigns.create(
		person.id,
		resign_date=getdate(employee.relieving_date or today()),
		resign_type=ResignType.QUIT,
		reason=_("Left in Frappe"),
	)
	return _("left, so resigned in BioTime")


def _reinstate(client, person) -> bool:
	"""Bring back a person resigned before. Returns whether there was one to bring back."""
	resigns = _resigns(client, person)
	if resigns:
		client.resigns.reinstate([resign.id for resign in resigns])
	return bool(resigns)


def _resigns(client, person) -> list | None:
	"""The person's resignations, or None when this BioTime has no resign API."""
	try:
		resigns = list(client.resigns.list(employee_id=person.id))
	except BioTimeError as exc:
		if resign_api_from_error(exc) is False:
			return None
		raise
	# Servers ignore filters they do not know, so check the employee here too.
	return [resign for resign in resigns if resign.employee.id == person.id]


def _department(server, client, employee) -> int:
	if server.department_mapping != "Fixed code" and employee.department:
		name = frappe.db.get_value("Department", employee.department, "department_name")
		return client.departments.upsert(employee.department, name or employee.department).id
	return _fixed(client.departments, server.default_department_code, _("Default Department Code"))


def _area(server, client, employee) -> int:
	if server.area_mapping != "Fixed code" and employee.branch:
		return client.areas.upsert(employee.branch, employee.branch).id
	return _fixed(client.areas, server.default_area_code, _("Default Area Code"))


def _position(server, client, employee) -> int | None:
	if server.position_mapping == "Designation" and employee.designation:
		return client.positions.upsert(employee.designation, employee.designation).id
	return None


def _fixed(resource, code: str | None, label: str) -> int:
	"""The id of the department or area the server names, which must exist in BioTime."""
	if not code:
		raise _Skip(_("no department or area to give them: set {0} on the server.").format(label))
	found = resource.get_by_code(code)
	if found is None:
		raise _Skip(_("BioTime has nothing with code {0}, which {1} names.").format(code, label))
	return found.id


def _servers() -> list[str]:
	"""The enabled pull-mode servers that push employees."""
	return frappe.get_all(
		"BioTime Server",
		filters={"enabled": 1, "mode": "Pull", "push_employees": 1},
		pluck="name",
		order_by="name asc",
	)


def _lock(employee: str):
	"""The lock that keeps two pushes of one employee from overlapping."""
	return frappe.cache.lock(f"{frappe.local.site}:biotime:push:{employee}", timeout=600)
