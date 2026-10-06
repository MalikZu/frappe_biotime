"""Push employees from Frappe to BioTime. Frappe is the master for employee records.

Once an employee's save commits, a push is queued: a save never waits for BioTime and never
fails because of it. A push brings the employee's copy on each server up to date: the person
with their code, their names, and their department, area and position, created on the server
when missing. An employee who left is resigned or deleted, as the server says, once their
relieving date has passed. BioTime people with no Frappe employee are never touched.
"""

import contextlib
import json
import unicodedata
from collections import Counter
from functools import partial
from time import monotonic

import frappe
from frappe import _
from frappe.utils import add_days, formatdate, getdate, now_datetime, today
from pybiotime import APIError, AuthenticationError, BioTimeError, ResignType, TransportError
from pybiotime.compat import resign_api_from_error
from redis.exceptions import LockError
from rq.timeouts import JobTimeoutException

from frappe_biotime.biotime import connection
from frappe_biotime.biotime.punches import _describe, _save_status, traceback

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
	"relieving_date",
)
#: How many times one job pushes an employee that keeps changing while it is pushed.
MAX_ROUNDS = 3
#: How long one Push All job runs before it queues the rest. Imports share the long queue,
#: and the job must stay within the queue's time limit.
PUSH_ALL_SECONDS = 600
#: How many days back the daily job looks for leavers whose last day has passed.
LEAVER_DAYS = 7
#: Errors after which no other employee can be pushed in the same run either.
UNREACHABLE = (TransportError, AuthenticationError)

# What a server does with an employee who left: the options of On Employee Left.
RESIGN = "Resign in BioTime"
DELETE = "Delete in BioTime"
NOTHING = "Do nothing"


class _Skip(Exception):
	"""The employee cannot be pushed to this server; the message says why."""


def on_employee_update(doc, method=None) -> None:
	"""Employee on_update: once the save commits, queue a push if something BioTime holds changed."""
	if any(doc.has_value_changed(fieldname) for fieldname in SYNCED_FIELDS) and _servers():
		# After the commit, and guarded: queueing checks the queue, and a full or unreachable
		# queue must not roll the save back.
		frappe.db.after_commit.add(partial(_queue_push, doc.name))


def _queue_push(employee: str) -> None:
	try:
		enqueue_push(employee)
	except Exception:
		# Their next save, or Push All Employees, pushes them.
		frappe.log_error(
			title=f"BioTime push not queued for {employee}", message=traceback(), defer_insert=True
		)


def enqueue_push(employee: str, job_id: str | None = None) -> None:
	frappe.enqueue(
		"frappe_biotime.biotime.employees.push_employee",
		job_id=job_id or f"biotime:push:{employee}",
		deduplicate=True,
		employee=employee,
	)


def enqueue_push_all(
	server: str, after: str | None = None, counts: dict | None = None, problems: list | None = None
) -> None:
	frappe.enqueue(
		"frappe_biotime.biotime.employees.push_all",
		queue="long",
		job_id=f"biotime:push-all:{server}" + (f":{after}" if after else ""),
		deduplicate=True,
		server=server,
		after=after,
		counts=counts,
		problems=problems,
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
	# Still changing after the last round: another job pushes the rest.
	enqueue_push(employee, job_id=f"biotime:push-again:{employee}")


def push_leavers() -> None:
	"""Daily: push employees whose last day has just passed, so they are resigned or deleted."""
	if not _servers():
		return
	for employee in frappe.get_all(
		"Employee",
		filters={
			"status": "Left",
			"attendance_device_id": ["is", "set"],
			"relieving_date": ["between", [add_days(today(), -LEAVER_DAYS), add_days(today(), -1)]],
		},
		pluck="name",
	):
		enqueue_push(employee)


def push_all(
	server: str, after: str | None = None, counts: dict | None = None, problems: list | None = None
) -> None:
	"""Job: push every employee with an Attendance Device ID to `server`, in name order.

	It stops after PUSH_ALL_SECONDS and queues the employees after the last one it pushed, with
	the counts so far.
	"""
	started = monotonic()
	server = frappe.get_doc("BioTime Server", server)
	if not (server.enabled and server.push_employees and server.mode == "Pull"):
		if after:
			# A part queued before the server stopped pushing: say the run ended.
			_report_push_all(
				server.name,
				Counter(counts or {}),
				[_("Stopped: the server no longer pushes employees.")],
			)
		return
	counts = Counter(counts or {})
	problems = list(problems or [])
	filters = {"attendance_device_id": ["is", "set"]}
	if server.company:
		filters["company"] = server.company
	if after:
		filters["name"] = [">", after]
	# One Error Log per job is enough to find the cause.
	logged = False
	# Every job gets at least one employee further, so the parts always end.
	done = 0
	try:
		with connection.connect(server) as client:
			for name in frappe.get_all("Employee", filters=filters, pluck="name", order_by="name asc"):
				if done and monotonic() - started > PUSH_ALL_SECONDS:
					enqueue_push_all(server.name, after, dict(counts), problems[:20])
					_report_push_all(server.name, counts, problems, more=True)
					return
				after, done = name, done + 1
				lock = _lock(name)
				if not lock.acquire(blocking=True, blocking_timeout=60):
					counts[_("skipped while another push of them ran")] += 1
					continue
				try:
					# A fresh read, not the one from when this job started.
					frappe.db.commit()  # nosemgrep
					counts[_push(server, client, frappe.get_doc("Employee", name))] += 1
				except (*UNREACHABLE, JobTimeoutException):
					raise
				except Exception as exc:
					if not isinstance(exc, _Skip) and not logged:
						frappe.log_error(title=f"BioTime push failed for {server.name}", message=traceback())
						logged = True
					counts[_("not pushed")] += 1
					problems.append(f"{name}: {exc if isinstance(exc, _Skip) else _describe(exc)}")
				finally:
					with contextlib.suppress(LockError):
						lock.release()
	except UNREACHABLE as exc:
		problems.insert(0, _("Stopped: BioTime could not be reached ({0}).").format(str(exc)))
	except JobTimeoutException:
		problems.insert(0, _("Stopped: the job ran out of time. Run Push All Employees again."))
		_report_push_all(server.name, counts, problems)
		raise
	_report_push_all(server.name, counts, problems)


def _report_push_all(server: str, counts: Counter, problems: list, more: bool = False) -> None:
	parts = [", ".join(f"{count} {outcome}" for outcome, count in counts.most_common()) or _("No employees.")]
	if more:
		parts.append(_("Still running: the rest is queued."))
	parts.extend(problems[:20])
	_write_status(server, last_push_at=now_datetime(), last_push_message="\n".join(parts)[:1000])


def _write_status(server: str, **values) -> None:
	"""Keep a push's outcome on the server. A failure here never stops the push."""
	try:
		# A fresh read view: the server's row may have changed while BioTime was read, and an
		# update from an older view fails under snapshot isolation.
		frappe.db.commit()  # nosemgrep
		_save_status(server, **values)
	except JobTimeoutException:
		raise
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"BioTime push status not saved for {server}", message=traceback())


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
			message=traceback(),
			reference_doctype="Employee",
			reference_name=employee.name,
		)
		message = f"{employee.name}: {_describe(exc)}"
	_write_status(server.name, last_push_at=now_datetime(), last_push_message=message[:1000])


def _pushes(server, employee) -> bool:
	"""Whether `server` holds a copy of `employee`."""
	if server.company and employee.company != server.company:
		return False
	# No code, no BioTime person. Removing a code does not remove the person.
	return bool((employee.attendance_device_id or "").strip())


def _push(server, client, employee) -> str:
	"""Bring `server`'s copy of `employee` up to date. Returns what happened."""
	code = employee.attendance_device_id.strip()
	if employee.status == "Left":
		# Resigning or deleting goes by the current code only.
		return _leave(server, client, employee, _by_code(client, code))
	person = _find(client, employee, code)
	area = _area(server, client, employee)
	values = {
		"department_id": _department(server, client, employee),
		"first_name": " ".join(name for name in (employee.first_name, employee.middle_name) if name),
		# An empty value clears BioTime's; None would leave it as it is.
		"last_name": employee.last_name or "",
		**_position(server, client, employee),
	}
	if person is None:
		client.employees.create(_plain_code(code), area_ids=[area], **values)
		return _("created in BioTime")
	# Areas decide which terminals know a person: add theirs, and take none away.
	values["area_ids"] = sorted({area, *(existing.id for existing in person.area)})
	outcome = _("up to date in BioTime")
	client.employees.upsert(person.emp_code, **values)
	if employee.status == "Active" and server.on_employee_left == RESIGN and _reinstate(client, person):
		outcome = _("reinstated in BioTime")
	return outcome


def _find(client, employee, code: str):
	"""The BioTime person under the employee's code, or None when a new one may be created.

	The app never changes a code in BioTime. When the code changed and BioTime has someone
	under an earlier one, only HR can tell who that is: the employee, whose fingerprints
	terminals keep sending under that code, or someone else after a typo or a wrong link.
	"""
	person = _by_code(client, code)
	if person is not None:
		return person
	for old_code in _old_codes(employee.name, code):
		# Another employee's code now: that person is theirs.
		if frappe.db.exists("Employee", {"attendance_device_id": old_code, "name": ["!=", employee.name]}):
			continue
		if _by_code(client, old_code) is not None:
			raise _Skip(
				_(
					"BioTime has someone under {0}, this employee's earlier code, and no one under {1}. "
					"If that is this employee, change their code to {1} in BioTime; if not, add {1} "
					"there. Until then this employee is not pushed."
				).format(old_code, code)
			)
	return None


def _by_code(client, code: str):
	"""The BioTime person with `code`, as typed, in digits 0-9, in capitals or in small letters.

	BioTime compares codes exactly, while the import maps punches the way the database
	compares codes, where case and digits of other scripts, such as Arabic, do not count.
	"""
	plain = _plain_code(code)
	for spelling in dict.fromkeys((code, plain, plain.upper(), plain.lower())):
		person = client.employees.get_by_code(spelling)
		if person is not None:
			return person
	return None


def _plain_code(code: str) -> str:
	"""`code` as BioTime keeps codes: compatibility forms folded, and digits written 0-9."""
	text = unicodedata.normalize("NFKC", code).strip()
	return "".join(str(unicodedata.decimal(char)) if char.isdecimal() else char for char in text)


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
	if server.on_employee_left not in (RESIGN, DELETE):
		return _("left, and kept in BioTime as the server says")
	last_day = getdate(employee.relieving_date) if employee.relieving_date else None
	if last_day and last_day >= getdate(today()):
		# They may punch until their last day is over. The daily job pushes them after it.
		return _("leaving after {0}, so changed in BioTime the day after").format(formatdate(last_day))
	if server.on_employee_left == DELETE:
		client.employees.delete(person.id)
		return _("left, so deleted from BioTime")
	resigns = _resigns(client, person)
	if resigns is None:
		raise _Skip(_("left, but this BioTime version cannot resign people. Resign them in BioTime."))
	if resigns:
		return _("left, and already resigned in BioTime")
	client.resigns.create(
		person.id,
		resign_date=last_day or getdate(today()),
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
		return _coded(client.departments, employee.department, name or employee.department)
	return _fixed(client.departments, server.default_department_code, _("Default Department Code"))


def _area(server, client, employee) -> int:
	if server.area_mapping != "Fixed code" and employee.branch:
		return _coded(client.areas, employee.branch, employee.branch)
	return _fixed(client.areas, server.default_area_code, _("Default Area Code"))


def _position(server, client, employee) -> dict:
	"""The position to send: the Designation's, an empty one to clear it, or none to leave it."""
	if server.position_mapping != "Designation":
		return {}
	if not employee.designation:
		return {"fields": {"position": None}}
	return {"position_id": _coded(client.positions, employee.designation, employee.designation)}


def _coded(resource, code: str, name: str) -> int:
	"""The id of the department, area or position with `code`, created or renamed as needed."""
	try:
		return resource.upsert(code, name).id
	except APIError:
		# Another push may have created it a moment ago, and BioTime refused this one.
		found = resource.get_by_code(code)
		if found is None:
			raise
		return found.id


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
