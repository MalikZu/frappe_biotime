"""Import BioTime punches into Employee Checkin.

One run reads every punch that reached BioTime since the previous run, late uploads
included, and stores each as a checkin keyed on ``<server>:<transaction id>``. The read
state is saved only after the checkins are committed, so a failed run repeats work
instead of losing punches.
"""

import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_system_timezone, getdate, now_datetime
from pybiotime import Transaction
from redis.exceptions import LockError

from frappe_biotime.biotime.connection import connect

if TYPE_CHECKING:
	from frappe_biotime.biotime.doctype.biotime_server.biotime_server import BioTimeServer

#: Checkins are committed in batches, so a long first import keeps its progress.
BATCH_SIZE = 500
#: How many unmapped employee codes the server record lists.
MAX_LISTED_CODES = 50


@dataclass
class ImportCounts:
	imported: int = 0
	already_imported: int = 0
	duplicate: int = 0
	unmapped: int = 0
	other_company: int = 0
	terminal_off: int = 0
	inactive: int = 0
	after_relieving: int = 0
	unmapped_codes: set[str] = field(default_factory=set)

	def summary(self) -> str:
		skipped = [
			(self.already_imported, "already imported"),
			(self.duplicate, "same time already logged"),
			(self.unmapped, "no employee with that attendance device id"),
			(self.other_company, "employee of another company"),
			(self.terminal_off, "terminal not imported"),
			(self.inactive, "employee inactive"),
			(self.after_relieving, "after the employee's relieving date"),
		]
		parts = [f"{count} {reason}" for count, reason in skipped if count]
		message = f"Imported {self.imported}."
		return f"{message} Skipped: {', '.join(parts)}." if parts else message


def enqueue_imports() -> None:
	"""Scheduler job: queue an import for every enabled pull-mode server."""
	servers = frappe.get_all(
		"BioTime Server",
		filters={"enabled": 1, "mode": "Pull", "import_punches": 1},
		pluck="name",
		order_by="name asc",
	)
	for server in servers:
		enqueue_import(server)


def enqueue_import(server: str) -> None:
	frappe.enqueue(
		"frappe_biotime.biotime.punches.import_punches",
		queue="long",
		job_id=f"biotime:import:{server}",
		deduplicate=True,
		server=server,
	)


def import_punches(server: str) -> ImportCounts | None:
	"""Read new punches from one BioTime server and store them as checkins.

	Returns the counts, or ``None`` when the run failed or another run holds the lock.
	A failure is recorded on the server and in the Error Log; the read state stays as it
	was, so the next run reads the same punches again.
	"""
	lock = frappe.cache.lock(f"{frappe.local.site}:biotime:import:{server}", timeout=3600)
	if not lock.acquire(blocking=False):
		return None
	try:
		return _import(frappe.get_doc("BioTime Server", server))
	finally:
		# The lock may have expired during a very long run.
		with contextlib.suppress(LockError):
			lock.release()


def _import(server: "BioTimeServer") -> ImportCounts | None:
	started = now_datetime()
	try:
		with connect(server) as client:
			result = client.transactions.read_new(
				_read_state(server),
				start=datetime.combine(getdate(server.import_from), time.min) if server.import_from else None,
				lookback=timedelta(days=server.lookback_days or 3),
			)
		counts = store_punches(server, result.transactions)
	except Exception as exc:
		frappe.log_error(
			title=f"BioTime import failed for {server.name}",
			reference_doctype=server.doctype,
			reference_name=server.name,
		)
		_save_status(
			server.name,
			last_run_at=started,
			last_run_result="Failed",
			last_run_message=f"{type(exc).__name__}: {exc}"[:1000],
		)
		return None
	_save_status(
		server.name,
		last_run_at=started,
		last_run_result="Success",
		last_run_message=counts.summary(),
		last_imported_count=counts.imported,
		unmapped_codes=", ".join(sorted(counts.unmapped_codes)[:MAX_LISTED_CODES]),
		upload_order=result.state.get("upload_order"),
		read_state=json.dumps(result.state),
	)
	return counts


def store_punches(server: "BioTimeServer", transactions: list[Transaction]) -> ImportCounts:
	"""Insert checkins for `transactions` and commit them in batches."""
	counts = ImportCounts()
	employees = _employees_by_code({punch.emp_code for punch in transactions})
	terminals = {row.serial_number: row for row in server.terminals}
	in_states, out_states = _codes(server.in_states), _codes(server.out_states)
	site_timezone = ZoneInfo(get_system_timezone())

	for start in range(0, len(transactions), BATCH_SIZE):
		batch = transactions[start : start + BATCH_SIZE]
		# A failed batch is undone on its own; batches committed before it stay, and the
		# next run skips them by biotime_uid.
		frappe.db.savepoint("biotime_batch")
		try:
			_store_batch(server, batch, counts, employees, terminals, in_states, out_states, site_timezone)
		except Exception:
			frappe.db.rollback(save_point="biotime_batch")
			raise
		frappe.db.commit()
	return counts


def _store_batch(server, transactions, counts, employees, terminals, in_states, out_states, site_timezone):
	"""Insert the checkins for one batch of punches, adding to `counts`."""
	batch = {f"{server.name}:{punch.id}": punch for punch in transactions}
	known = set(
		frappe.get_all("Employee Checkin", filters={"biotime_uid": ["in", list(batch)]}, pluck="biotime_uid")
	)
	planned = []
	for uid, punch in batch.items():
		if uid in known:
			counts.already_imported += 1
			continue
		terminal = terminals.get(punch.terminal_sn or "")
		if terminal and not terminal.import_punches:
			counts.terminal_off += 1
			continue
		employee = employees.get(punch.emp_code)
		if employee is None:
			counts.unmapped += 1
			counts.unmapped_codes.add(punch.emp_code)
			continue
		if server.company and employee.company != server.company:
			counts.other_company += 1
			continue
		if employee.status == "Inactive":
			counts.inactive += 1
			continue
		punch_time = _site_time(punch.punch_time, site_timezone)
		if (
			employee.status == "Left"
			and employee.relieving_date
			and punch_time.date() > getdate(employee.relieving_date)
		):
			counts.after_relieving += 1
			continue
		log_type = _log_type(server, punch, terminal, in_states, out_states)
		planned.append((uid, punch, employee.name, punch_time, log_type, terminal))

	logged = _logged_times(planned)
	for uid, punch, employee, punch_time, log_type, terminal in planned:
		# Frappe HR refuses a second checkin with the same employee, time and log type.
		if (employee, punch_time, log_type or "") in logged:
			counts.duplicate += 1
			continue
		_insert_checkin(server, uid, punch, employee, punch_time, log_type, terminal)
		logged.add((employee, punch_time, log_type or ""))
		counts.imported += 1


def _insert_checkin(server, uid, punch, employee, punch_time, log_type, terminal) -> None:
	checkin = frappe.new_doc("Employee Checkin")
	checkin.update(
		{
			"employee": employee,
			"time": punch_time,
			"log_type": log_type,
			"device_id": punch.terminal_sn or None,
			"skip_auto_attendance": server.skip_auto_attendance,
			"biotime_server": server.name,
			"biotime_uid": uid,
			"biotime_punch_state": punch.punch_state,
			"biotime_verify_type": punch.verify_type,
		}
	)
	if terminal and (terminal.latitude or terminal.longitude):
		checkin.latitude = terminal.latitude
		checkin.longitude = terminal.longitude
	# `time` is a permlevel 1 field: without level 1 write access it is silently reset to now.
	checkin.insert(ignore_permissions=True)


def _employees_by_code(codes: set[str]) -> dict[str, Any]:
	if not codes:
		return {}
	rows = frappe.get_all(
		"Employee",
		filters={"attendance_device_id": ["in", list(codes)]},
		fields=["name", "attendance_device_id", "company", "status", "relieving_date"],
	)
	return {row.attendance_device_id: row for row in rows}


def _logged_times(planned: list) -> set[tuple[str, datetime, str]]:
	"""(employee, time, log type) of the checkins that already exist for these punches."""
	if not planned:
		return set()
	times = [entry[3] for entry in planned]
	rows = frappe.get_all(
		"Employee Checkin",
		filters={
			"employee": ["in", list({entry[2] for entry in planned})],
			"time": ["between", [min(times), max(times)]],
		},
		fields=["employee", "time", "log_type"],
	)
	return {(row.employee, row.time, row.log_type or "") for row in rows}


def _log_type(server, punch: Transaction, terminal, in_states: set[str], out_states: set[str]) -> str | None:
	if server.log_type_mode == "Map punch states":
		if punch.punch_state in in_states:
			return "IN"
		if punch.punch_state in out_states:
			return "OUT"
		return None
	if server.log_type_mode == "Terminal direction":
		return (terminal.direction if terminal else None) or None
	return None


def _site_time(value: datetime, site_timezone: ZoneInfo) -> datetime:
	"""A punch time as this site stores datetimes: naive, in the site's timezone."""
	if value.tzinfo is not None:
		value = value.astimezone(site_timezone).replace(tzinfo=None)
	return value.replace(microsecond=0)


def _codes(value: str | None) -> set[str]:
	return {code.strip() for code in (value or "").split(",") if code.strip()}


def _read_state(server: "BioTimeServer") -> dict[str, Any] | None:
	state = server.read_state
	if isinstance(state, str):
		state = json.loads(state) if state.strip() else None
	return state or None


def _save_status(server: str, **values: Any) -> None:
	# Saved without the document, so routine runs create no versions or modified stamps.
	frappe.db.set_value("BioTime Server", server, values, update_modified=False)
	frappe.db.commit()
