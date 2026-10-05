"""Import BioTime punches into Employee Checkin.

One run reads every punch that reached BioTime since the previous run, late uploads
included, and stores each as a checkin keyed on ``<server>:<generation>:<transaction id>``.
A punch that cannot become a checkin yet is kept as a BioTime Pending Punch and tried again, so
no punch is dropped. The read state is saved only after the punches are committed, so a
failed run repeats work instead of losing punches.
"""

import contextlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import frappe
from frappe.query_builder import DocType
from frappe.query_builder.functions import Count, Max
from frappe.utils import (
	add_to_date,
	cint,
	get_datetime,
	get_system_timezone,
	getdate,
	now_datetime,
	strip_html,
)
from hrms.hr.doctype.employee_checkin.employee_checkin import CheckinRadiusExceededError
from hrms.hr.doctype.shift_assignment.shift_assignment import get_actual_start_end_datetime_of_shift
from pybiotime import ReadStateError, Transaction
from redis.exceptions import LockError
from rq.timeouts import JobTimeoutException

from frappe_biotime.biotime import connection

if TYPE_CHECKING:
	from frappe_biotime.biotime.doctype.biotime_server.biotime_server import BioTimeServer

#: Punches are committed in batches, so a long first import keeps its progress.
BATCH_SIZE = 500
#: How many waiting punches one scheduled run tries again.
RETRY_LIMIT = 5000
#: Errors may be passing faults, so an Error punch is tried again after this many minutes.
ERROR_RETRY_MINUTES = 10
#: How many unmapped employee codes the server record lists.
MAX_LISTED_CODES = 50
#: What a run reports when BioTime's transaction ids start over.
RESTORED = (
	"BioTime's transaction ids started over, as after a database restore or reinstall. "
	"Use Actions > Start Over to read it again from a date."
)
#: The Shift Type setting under which Frappe HR refuses checkins without a log type.
STRICT_LOG_TYPE = "Strictly based on Log Type in Employee Checkin"
PENDING = "BioTime Pending Punch"

# Why a punch waits: the options of BioTime Pending Punch's Reason.
UNMAPPED = "Unknown device ID"
INACTIVE = "Inactive employee"
AFTER_RELIEVING = "After relieving date"
LOG_TYPE_REQUIRED = "Log type required"
NO_LOCATION = "No terminal coordinates"
OUTSIDE_RADIUS = "Outside check-in radius"
ERROR = "Error"
#: A waiting punch that the server's settings now leave out. It stays, so undoing them lets it in.
LEFT_OUT = "Left out by settings"


@dataclass(frozen=True)
class Punch:
	"""A punch as this site stores it: keyed, and timed in the site's timezone."""

	uid: str
	emp_code: str
	time: datetime
	terminal_sn: str
	punch_state: str | None
	verify_type: int | None
	#: The BioTime Pending Punch row this punch came from, when it was waiting.
	waiting: Any = field(default=None, compare=False)

	@classmethod
	def from_transaction(
		cls, server: "BioTimeServer", punch: Transaction, site_timezone: ZoneInfo
	) -> "Punch":
		return cls(
			uid=f"{server.name}:{server.key_generation or 1}:{punch.id}",
			emp_code=punch.emp_code,
			time=site_time(punch.punch_time, site_timezone),
			terminal_sn=punch.terminal_sn or "",
			punch_state=punch.punch_state,
			verify_type=punch.verify_type,
		)

	@classmethod
	def from_waiting(cls, row: Any) -> "Punch":
		return cls(
			uid=row.biotime_uid,
			emp_code=row.emp_code or "",
			time=get_datetime(row.punch_time),
			terminal_sn=row.terminal_sn or "",
			punch_state=row.punch_state,
			verify_type=row.verify_type,
			waiting=row,
		)


@dataclass
class ImportCounts:
	imported: int = 0
	#: Of `imported`, the punches that had been waiting.
	from_waiting: int = 0
	#: Already a checkin, or already waiting.
	already_there: int = 0
	terminal_off: int = 0
	other_company: int = 0
	#: Punches that started waiting in this run, by reason.
	waiting: Counter = field(default_factory=Counter)
	#: Whether this run has written an Error Log for a punch yet.
	error_logged: bool = False


def summary(counts: ImportCounts, waiting_now: Counter) -> str:
	"""The run's message: this run's counts, and every punch still waiting."""
	imported = f"Imported {counts.imported}"
	if counts.from_waiting:
		imported += f", {counts.from_waiting} of them waiting from before"
	parts = [imported + "."]
	if counts.already_there:
		parts.append(f"Already in Frappe: {counts.already_there}.")
	left_out = [
		f"{count} {reason}"
		for count, reason in (
			(counts.terminal_off, "terminal not imported"),
			(counts.other_company, "employee of another company"),
		)
		if count
	]
	if left_out:
		parts.append(f"Left out by this server's settings: {', '.join(left_out)}.")
	if waiting_now:
		reasons = ", ".join(f"{count} {reason}" for reason, count in waiting_now.most_common())
		parts.append(f"Waiting: {waiting_now.total()} ({reasons}).")
	return " ".join(parts)


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


def enqueue_import(server: str, retry_all: bool = False) -> None:
	frappe.enqueue(
		"frappe_biotime.biotime.punches.import_punches",
		queue="long",
		job_id=f"biotime:import:{server}",
		deduplicate=True,
		server=server,
		retry_all=retry_all,
	)


def import_lock(server: str):
	"""The lock that keeps a server's imports from overlapping."""
	return frappe.cache.lock(f"{frappe.local.site}:biotime:import:{server}", timeout=3600)


def import_punches(server: str, retry_all: bool = False) -> ImportCounts | None:
	"""Read new punches from one BioTime server and store them.

	Each punch becomes a checkin or waits as a BioTime Pending Punch. Waiting punches are
	tried again when something they depend on changed, once a day, or every time when
	`retry_all` is set. Returns the counts, or ``None`` when the run failed or another run
	holds the lock. A failure is recorded on the server and in the Error Log; the read state
	stays as it was, so the next run reads the same punches again.
	"""
	lock = import_lock(server)
	if not lock.acquire(blocking=False):
		return None
	try:
		# End the job's open transaction, so the run reads everything committed before
		# `started`. Anything committed later is newer than every last_tried the run writes,
		# so the next run tries those punches again.
		frappe.db.commit()  # nosemgrep
		started = now_datetime()
		return _import(frappe.get_doc("BioTime Server", server), retry_all, started)
	finally:
		# The lock may have expired during a very long run.
		with contextlib.suppress(LockError):
			lock.release()


def _import(server: "BioTimeServer", retry_all: bool, started: datetime) -> ImportCounts | None:
	# Imported here because the watermark module builds on this one.
	from frappe_biotime.biotime import watermark

	counts = ImportCounts()
	try:
		with connection.connect(server) as client:
			# Read before the punches: a terminal that reconnects after this read cannot
			# move the watermark past punches it uploads later.
			terminals, terminals_error = watermark.read_terminals(client)
			result = client.transactions.read_new(
				_read_state(server),
				start=datetime.combine(getdate(server.import_from), time.min) if server.import_from else None,
				lookback=timedelta(days=server.lookback_days or 3),
			)
		site_timezone = ZoneInfo(get_system_timezone())
		store_punches(
			server,
			[Punch.from_transaction(server, t, site_timezone) for t in result.transactions],
			counts,
			started,
		)
		# Every new punch is now a checkin or waiting, so the next run can start after them.
		_save_status(
			server.name, read_state=json.dumps(result.state), upload_order=result.state.get("upload_order")
		)
		retries = _retry_candidates(server, retry_all, started)
		store_punches(server, [Punch.from_waiting(row) for row in retries], counts, started)
		waiting_now = _waiting_counts(server.name)
		reach = watermark.reach(server, started, terminals, terminals_error)
		_save_status(
			server.name,
			last_run_at=started,
			last_run_result="Success",
			last_run_message=summary(counts, waiting_now),
			last_imported_count=counts.imported,
			waiting_punches=waiting_now.total(),
			unmapped_codes=_unmapped_codes(server.name),
			held_back_by=reach.held_back_by,
			# Unknown this run: keep the last known reach.
			**({"imported_up_to": reach.at} if reach.at else {}),
		)
		watermark.move_shift_types()
		frappe.db.commit()
	except ReadStateError as exc:
		return _fail(server, started, f"{RESTORED} {exc}")
	except Exception as exc:
		return _fail(server, started, f"{type(exc).__name__}: {exc}")
	return counts


def _fail(server: "BioTimeServer", started: datetime, message: str) -> None:
	frappe.log_error(
		title=f"BioTime import failed for {server.name}",
		reference_doctype=server.doctype,
		reference_name=server.name,
	)
	_save_status(server.name, last_run_at=started, last_run_result="Failed", last_run_message=message[:1000])


def store_punches(
	server: "BioTimeServer", punches: list[Punch], counts: ImportCounts, stamp: datetime
) -> None:
	"""Turn `punches` into checkins or waiting punches, and commit them in batches.

	`stamp` is the run's start, written as last_tried on every waiting punch.
	"""
	if not punches:
		return
	context = _Context.of(server, stamp)
	for start in range(0, len(punches), BATCH_SIZE):
		# A failed batch is undone on its own; batches committed before it stay, and the
		# next run recognizes them by biotime_uid.
		frappe.db.savepoint("biotime_batch")
		try:
			_store_batch(context, punches[start : start + BATCH_SIZE], counts)
		except Exception:
			frappe.db.rollback(save_point="biotime_batch")
			raise
		frappe.db.commit()  # nosemgrep


@dataclass
class _Context:
	"""What every batch of one run needs from the server."""

	server: Any
	stamp: datetime
	terminals: dict[str, Any]
	in_states: set[str]
	out_states: set[str]

	@classmethod
	def of(cls, server: "BioTimeServer", stamp: datetime) -> "_Context":
		return cls(
			server=server,
			stamp=stamp,
			terminals={row.serial_number: row for row in server.terminals},
			in_states=_codes(server.in_states),
			out_states=_codes(server.out_states),
		)


def _store_batch(context: _Context, batch: list[Punch], counts: ImportCounts) -> None:
	"""Store one batch of punches, adding to `counts`."""
	server = context.server
	known = _known_uids(batch)
	waiting = _waiting_keys(server.name, batch)
	employees = _employees_by_code({punch.emp_code for punch in batch})
	changes = _WaitingChanges(server.name, context.stamp)
	mapped = []
	for punch in batch:
		# A punch read again under a new key generation still matches its waiting row by time.
		if punch.uid in known or (not punch.waiting and _waiting_key(punch) in waiting):
			counts.already_there += 1
			changes.resolve(punch)
			continue
		terminal = context.terminals.get(punch.terminal_sn)
		employee = employees.get(_code_key(punch.emp_code))
		terminal_off = bool(terminal and not terminal.import_punches)
		other_company = bool(employee and server.company and employee.company != server.company)
		if punch.waiting and (terminal_off or other_company):
			# Kept before the setting changed: it stays, so undoing the change lets it in.
			changes.wait(punch, LEFT_OUT, counts, employee=employee.name if employee else None)
			continue
		if terminal_off:
			counts.terminal_off += 1
			continue
		if employee is None:
			changes.wait(punch, UNMAPPED, counts)
			continue
		if other_company:
			counts.other_company += 1
			continue
		mapped.append((punch, employee, terminal))

	logged = _logged_times(mapped)
	for punch, employee, terminal in mapped:
		# One physical punch is one checkin: a checkin at the same second already covers it,
		# whatever the employee's status is now.
		if (employee.name, punch.time) in logged:
			counts.already_there += 1
			changes.resolve(punch)
			continue
		if employee.status == "Inactive":
			changes.wait(punch, INACTIVE, counts, employee=employee.name)
			continue
		if (
			employee.status == "Left"
			and employee.relieving_date
			and punch.time.date() > getdate(employee.relieving_date)
		):
			changes.wait(punch, AFTER_RELIEVING, counts, employee=employee.name)
			continue
		log_type = _log_type(server, punch, terminal, context.in_states, context.out_states)
		checkin = _new_checkin(server, punch, employee.name, log_type, terminal)
		failure = _insert(checkin, server, counts)
		if failure:
			reason, message = failure
			changes.wait(punch, reason, counts, employee=employee.name, message=message)
			continue
		logged.add((employee.name, punch.time))
		counts.imported += 1
		if punch.waiting:
			counts.from_waiting += 1
		changes.resolve(punch)
	changes.apply()


class _WaitingChanges:
	"""What one batch changes in BioTime Pending Punch, written together at the end."""

	def __init__(self, server: str, now: datetime) -> None:
		self.server = server
		self.now = now
		self.resolved: list[str] = []
		self.new: list[tuple] = []
		self.tried: list[str] = []
		self.changed: list[tuple[str, dict]] = []

	def resolve(self, punch: Punch) -> None:
		"""`punch` is in Frappe now: it waits no more."""
		if punch.waiting:
			self.resolved.append(punch.waiting.name)

	def wait(
		self,
		punch: Punch,
		reason: str,
		counts: ImportCounts,
		*,
		employee: str | None = None,
		message: str | None = None,
	) -> None:
		if not punch.waiting:
			counts.waiting[reason] += 1
			self.new.append((punch, reason, employee, message))
			return
		row = punch.waiting
		values = {"reason": reason, "employee": employee, "message": message}
		if any(row.get(key) != value for key, value in values.items()):
			self.changed.append((row.name, values))
		else:
			self.tried.append(row.name)

	def apply(self) -> None:
		if self.resolved:
			frappe.db.delete(PENDING, {"name": ["in", self.resolved]})
		if self.new:
			_insert_waiting(self.server, self.new, self.now)
		for name, values in self.changed:
			frappe.db.set_value(PENDING, name, {**values, "last_tried": self.now}, update_modified=False)
		if self.tried:
			table = DocType(PENDING)
			frappe.qb.update(table).set(table.last_tried, self.now).where(table.name.isin(self.tried)).run()


def _insert_waiting(server: str, rows: list[tuple], now: datetime) -> None:
	user = frappe.session.user
	fields = [
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"server",
		"biotime_uid",
		"emp_code",
		"punch_time",
		"terminal_sn",
		"punch_state",
		"verify_type",
		"reason",
		"employee",
		"message",
		"last_tried",
	]
	values = [
		(
			frappe.generate_hash(length=10),
			user,
			now,
			now,
			user,
			0,
			0,
			server,
			punch.uid,
			punch.emp_code,
			punch.time,
			punch.terminal_sn or None,
			punch.punch_state,
			cint(punch.verify_type),
			reason,
			employee,
			message,
			now,
		)
		for punch, reason, employee, message in rows
	]
	frappe.db.bulk_insert(PENDING, fields, values)


def _new_checkin(server, punch: Punch, employee: str, log_type: str | None, terminal):
	checkin = frappe.new_doc("Employee Checkin")
	checkin.update(
		{
			"employee": employee,
			"time": punch.time,
			"log_type": log_type,
			"device_id": punch.terminal_sn or None,
			"skip_auto_attendance": server.skip_auto_attendance,
			"biotime_server": server.name,
			"biotime_uid": punch.uid,
			"biotime_punch_state": punch.punch_state,
			"biotime_verify_type": punch.verify_type,
		}
	)
	if terminal and (terminal.latitude or terminal.longitude):
		checkin.latitude = terminal.latitude
		checkin.longitude = terminal.longitude
	return checkin


def _insert(checkin, server, counts: ImportCounts) -> tuple[str, str] | None:
	"""Insert `checkin`. Returns (reason, message) when its punch has to wait instead."""
	messages = len(frappe.local.message_log)
	frappe.db.savepoint("biotime_checkin")
	try:
		# `time` is a permlevel 1 field: without level 1 write access it is silently reset to now.
		checkin.insert(ignore_permissions=True)
	except JobTimeoutException:
		raise
	except Exception as exc:
		# Undo only this punch. If the database cannot roll back, this raises and the batch fails.
		frappe.db.rollback(save_point="biotime_checkin")
		del frappe.local.message_log[messages:]
		reason = _refusal(checkin, exc)
		if reason is None:
			reason = ERROR
			if not counts.error_logged:
				frappe.log_error(
					title=f"BioTime punch could not be imported for {server.name}",
					reference_doctype=server.doctype,
					reference_name=server.name,
				)
				counts.error_logged = True
		return reason, _describe(exc)
	frappe.db.release_savepoint("biotime_checkin")
	return None


def _refusal(checkin, exc: Exception) -> str | None:
	"""The reason for a checkin Frappe HR refused on purpose, else None.

	These checks repeat Frappe HR's own, in its order, and run only after it refused, so a
	refusal is never assumed for a punch it would accept.
	"""
	if not isinstance(exc, frappe.ValidationError):
		return None
	if isinstance(exc, CheckinRadiusExceededError):
		return OUTSIDE_RADIUS
	if not checkin.log_type and not checkin.skip_auto_attendance:
		shift = get_actual_start_end_datetime_of_shift(checkin.employee, checkin.time, True)
		# Read from the record: Frappe HR 16 leaves this setting out of the shift it returns.
		if shift and STRICT_LOG_TYPE == frappe.get_cached_value(
			"Shift Type", shift.shift_type.name, "determine_check_in_and_check_out"
		):
			return LOG_TYPE_REQUIRED
	if not (checkin.latitude or checkin.longitude) and frappe.db.get_single_value(
		"HR Settings", "allow_geolocation_tracking"
	):
		return NO_LOCATION
	return None


def _describe(exc: Exception) -> str:
	text = strip_html(str(exc)).strip()
	if not isinstance(exc, frappe.ValidationError):
		text = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
	return text[:500]


def _known_uids(batch: list[Punch]) -> set[str]:
	"""Keys in `batch` that are already checkins, or, for new punches, already waiting."""
	known = set(
		frappe.get_all(
			"Employee Checkin", filters={"biotime_uid": ["in", [p.uid for p in batch]]}, pluck="biotime_uid"
		)
	)
	new = [punch.uid for punch in batch if not punch.waiting]
	if new:
		known.update(frappe.get_all(PENDING, filters={"biotime_uid": ["in", new]}, pluck="biotime_uid"))
	return known


def _waiting_keys(server: str, batch: list[Punch]) -> set[tuple[str, datetime]]:
	"""(code, time) of the new punches in `batch` that already wait on this server, under any key."""
	new = [punch for punch in batch if not punch.waiting]
	if not new:
		return set()
	times = [punch.time for punch in new]
	rows = frappe.get_all(
		PENDING,
		filters={
			"server": server,
			"emp_code": ["in", list({punch.emp_code for punch in new})],
			"punch_time": ["between", [min(times), max(times)]],
		},
		fields=["emp_code", "punch_time"],
	)
	return {(_code_key(row.emp_code), get_datetime(row.punch_time)) for row in rows}


def _waiting_key(punch: Punch) -> tuple[str, datetime]:
	return (_code_key(punch.emp_code), punch.time)


def _code_key(code: str | None) -> str:
	"""A code as the database compares codes: case and trailing spaces do not count."""
	return (code or "").rstrip().casefold()


def _employees_by_code(codes: set[str]) -> dict[str, Any]:
	if not codes:
		return {}
	rows = frappe.get_all(
		"Employee",
		filters={"attendance_device_id": ["in", list(codes)]},
		fields=["name", "attendance_device_id", "company", "status", "relieving_date"],
	)
	return {_code_key(row.attendance_device_id): row for row in rows}


def _logged_times(mapped: list) -> set[tuple[str, datetime]]:
	"""(employee, time) of the checkins that already exist for these punches."""
	if not mapped:
		return set()
	times = [punch.time for punch, _employee, _terminal in mapped]
	rows = frappe.get_all(
		"Employee Checkin",
		filters={
			"employee": ["in", list({employee.name for _punch, employee, _terminal in mapped})],
			"time": ["between", [min(times), max(times)]],
		},
		fields=["employee", "time"],
	)
	return {(row.employee, row.time) for row in rows}


def _retry_candidates(server: "BioTimeServer", retry_all: bool, stamp: datetime) -> list:
	"""The waiting punches worth trying again in this run."""
	table = DocType(PENDING)
	employee = DocType("Employee")
	query = (
		frappe.qb.from_(table)
		.left_join(employee)
		.on(employee.attendance_device_id == table.emp_code)
		.select(
			table.name,
			table.biotime_uid,
			table.emp_code,
			table.punch_time,
			table.terminal_sn,
			table.punch_state,
			table.verify_type,
			table.reason,
			table.employee,
			table.message,
		)
		.where(table.server == server.name)
		.orderby(table.punch_time)
	)
	if not retry_all:
		query = query.where(
			(table.last_tried < add_to_date(stamp, days=-1))
			| (table.last_tried < _settings_changed_at(server))
			| (
				(table.reason == ERROR)
				& (table.last_tried < add_to_date(stamp, minutes=-ERROR_RETRY_MINUTES))
			)
			# Its code now belongs to an employee, or that employee changed.
			| (employee.name.isnotnull() & (table.employee.isnull() | (employee.modified > table.last_tried)))
		).limit(RETRY_LIMIT)
	return query.run(as_dict=True)


def _settings_changed_at(server: "BioTimeServer") -> datetime:
	"""When something a refusal depends on last changed: this server, HR Settings, or shifts."""
	times = [server.modified, frappe.db.get_single_value("HR Settings", "modified")]
	for doctype in ("Shift Type", "Shift Assignment", "Shift Location"):
		table = DocType(doctype)
		times.append(frappe.qb.from_(table).select(Max(table.modified)).run()[0][0])
	return max(get_datetime(value) for value in times if value)


def _waiting_counts(server: str) -> Counter:
	table = DocType(PENDING)
	rows = (
		frappe.qb.from_(table)
		.select(table.reason, Count("*"))
		.where(table.server == server)
		.groupby(table.reason)
		.run()
	)
	return Counter(dict(rows))


def _unmapped_codes(server: str) -> str:
	table = DocType(PENDING)
	codes = (
		frappe.qb.from_(table)
		.select(table.emp_code)
		.distinct()
		.where((table.server == server) & (table.reason == UNMAPPED))
		.orderby(table.emp_code)
		.limit(MAX_LISTED_CODES)
		.run(pluck=True)
	)
	return ", ".join(codes)


def _log_type(server, punch: Punch, terminal, in_states: set[str], out_states: set[str]) -> str | None:
	if server.log_type_mode == "Map punch states":
		if punch.punch_state in in_states:
			return "IN"
		if punch.punch_state in out_states:
			return "OUT"
		return None
	if server.log_type_mode == "Terminal direction":
		return (terminal.direction if terminal else None) or None
	return None


def site_time(value: datetime, site_timezone: ZoneInfo | None = None) -> datetime:
	"""A BioTime time as this site stores datetimes: naive, in the site's timezone."""
	if value.tzinfo is not None:
		value = value.astimezone(site_timezone or ZoneInfo(get_system_timezone())).replace(tzinfo=None)
	return value.replace(microsecond=0)


def _codes(value: str | None) -> set[str]:
	return {code.strip() for code in (value or "").split(",") if code.strip()}


def _read_state(server: "BioTimeServer") -> dict[str, Any] | None:
	state = server.read_state
	if isinstance(state, str):
		state = json.loads(state) if state.strip() else None
	return state or None


def _save_status(server: str, **values: Any) -> None:
	# Saved without the document, so routine runs create no versions or modified stamps, and
	# committed at once, so the status survives a run that fails later.
	frappe.db.set_value("BioTime Server", server, values, update_modified=False)
	frappe.db.commit()  # nosemgrep
