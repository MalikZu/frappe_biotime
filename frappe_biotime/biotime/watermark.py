"""Move Frappe HR's attendance watermark, Shift Type ``last_sync_of_checkin``, safely.

Frappe HR marks attendance, Absent included, for shifts that end before a Shift Type's last
sync. Moving it before every punch is in Frappe marks people Absent who were there. So it
moves only forward, and only to the earliest time every server has fully imported, minus
a buffer.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import frappe
from frappe.query_builder import DocType
from frappe.utils import add_to_date, format_datetime, get_datetime, getdate
from pybiotime import BioTimeError

from frappe_biotime.biotime.punches import (
	ERROR,
	LOG_TYPE_REQUIRED,
	NO_LOCATION,
	OUTSIDE_RADIUS,
	PENDING,
	UNMAPPED,
	site_time,
)

if TYPE_CHECKING:
	from frappe_biotime.biotime.doctype.biotime_server.biotime_server import BioTimeServer

#: Waiting punches that hold attendance until they import or are deleted: refusals and errors
#: can hit every punch, so letting them expire would mark everyone Absent a day later.
HOLD_UNTIL_RESOLVED = (LOG_TYPE_REQUIRED, NO_LOCATION, OUTSIDE_RADIUS, ERROR)


@dataclass
class Reach:
	"""How far one server's punches are in Frappe, and what keeps it there."""

	#: Every punch from before this time is a checkin or waiting. None when unknown.
	at: datetime | None
	held_back_by: str


def read_terminals(client) -> tuple[list | None, str | None]:
	"""BioTime's terminals, or why they could not be read."""
	try:
		return list(client.terminals.list()), None
	except BioTimeError as exc:
		return None, str(exc)


def backlogs(server: "BioTimeServer", transactions: list) -> dict[str, datetime]:
	"""Per terminal, the newest punch this run read that arrived later than the buffer.

	A terminal that comes back online uploads what it stored oldest first, so while it still
	sends late punches, the rest of its backlog may not be in BioTime yet.
	"""
	late = timedelta(minutes=server.attendance_buffer_minutes or 0)
	newest: dict[str, datetime] = {}
	for punch in transactions:
		if not punch.terminal_sn or punch.upload_time is None:
			continue
		punched = site_time(punch.punch_time)
		if site_time(punch.upload_time) - punched > late:
			newest[punch.terminal_sn] = max(newest.get(punch.terminal_sn, punched), punched)
	return newest


def reach(
	server: "BioTimeServer",
	started: datetime,
	terminals: list | None,
	error: str | None,
	backlogs: dict[str, datetime] | None = None,
) -> Reach:
	"""How far `server` has imported, given the terminals read before its punches.

	The earliest of: the import's start; the last contact of each terminal that holds
	attendance, since an offline terminal may still hold punches; the newest late punch of a
	terminal still uploading its backlog; and the oldest waiting punch that holds (see
	_holding_punch).
	"""
	if terminals is None:
		return Reach(None, f"BioTime's terminals could not be read: {error}"[:140])
	rows = {row.serial_number: row for row in server.terminals}

	def holds(serial_number: str) -> bool:
		row = rows.get(serial_number)
		return row is None or bool(row.import_punches and row.holds_attendance)

	candidates = [(started, "This import's start")]
	for terminal in terminals:
		if terminal.last_activity is None or not holds(terminal.sn):
			continue
		seen = site_time(terminal.last_activity)
		candidates.append((seen, f"Terminal {terminal.sn}, last seen {format_datetime(seen)}"))
	for serial_number, punched in (backlogs or {}).items():
		if holds(serial_number):
			candidates.append(
				(
					punched,
					f"Terminal {serial_number}, still uploading punches from {format_datetime(punched)}",
				)
			)
	waiting = _holding_punch(server, started)
	if waiting:
		punched = get_datetime(waiting.punch_time)
		candidates.append(
			(punched, f"Waiting punch of {waiting.emp_code} at {format_datetime(punched)}: {waiting.reason}")
		)
	at, held_back_by = min(candidates, key=lambda candidate: candidate[0])
	return Reach(at, held_back_by[:140])


def _holding_punch(server: "BioTimeServer", started: datetime) -> Any:
	"""The oldest waiting punch that holds attendance, if any.

	Refusals and errors hold until they import or are deleted. An unknown code holds only
	while it is new: punched within the server's hold, and not seen before it, so a code that
	never gets linked, such as a visitor's, stops holding. Inactive, After relieving date and
	Left out by settings never hold, because Frappe HR marks no attendance from them.
	"""
	table = DocType(PENDING)
	holds = table.reason.isin(HOLD_UNTIL_RESOLVED)
	if server.attendance_hold_hours:
		cutoff = add_to_date(started, hours=-server.attendance_hold_hours)
		older = DocType(PENDING).as_("older")
		seen_before = (
			frappe.qb.from_(older)
			.select(older.emp_code)
			.where((older.server == server.name) & (older.reason == UNMAPPED) & (older.punch_time < cutoff))
		)
		holds = holds | (
			(table.reason == UNMAPPED) & (table.punch_time >= cutoff) & table.emp_code.notin(seen_before)
		)
	rows = (
		frappe.qb.from_(table)
		.select(table.emp_code, table.punch_time, table.reason)
		.where(table.server == server.name)
		.where(holds)
		.orderby(table.punch_time)
		.limit(1)
		.run(as_dict=True)
	)
	return rows[0] if rows else None


def move_shift_types() -> None:
	"""Move each eligible Shift Type's last sync forward to what every server has imported."""
	servers = _servers()
	target = _target(servers)
	if target is None:
		return
	floor = _floor(servers)
	table = DocType("Shift Type")
	for shift in frappe.get_all(
		"Shift Type",
		filters={"enable_auto_attendance": 1, "auto_update_last_sync": 0},
		fields=["name", "last_sync_of_checkin", "process_attendance_after"],
	):
		if not _movable(shift, floor):
			continue
		# Forward only in one statement, so a run with an older target cannot win a race, and
		# without touching modified, which waiting punches watch.
		(
			frappe.qb.update(table)
			.set(table.last_sync_of_checkin, target)
			.where((table.name == shift.name) & (table.last_sync_of_checkin < target))
			.run()
		)


def _movable(shift: Any, floor: datetime | None) -> bool:
	"""Whether moving `shift` forward can only mark days whose punches are in Frappe.

	Frappe HR marks Absent on days from Process Attendance After that have no attendance, up
	to the last sync. So a move is safe once the last sync, or Process Attendance After, is on
	or after the floor. A Shift Type without a last sync is never started.
	"""
	if not shift.last_sync_of_checkin:
		return False
	if floor is None or get_datetime(shift.last_sync_of_checkin) >= floor:
		return True
	return bool(shift.process_attendance_after) and getdate(shift.process_attendance_after) >= floor.date()


def notes(server: "BioTimeServer") -> dict[str, Any]:
	"""What the server form says about the Shift Types this app moves."""
	if not server.import_punches or server.mode != "Pull":
		return {}
	servers = _servers()
	floor = _floor(servers)
	moved, self_moving, not_started, behind = [], [], [], []
	for shift in frappe.get_all(
		"Shift Type",
		filters={"enable_auto_attendance": 1},
		fields=["name", "last_sync_of_checkin", "auto_update_last_sync", "process_attendance_after"],
		order_by="name asc",
	):
		if shift.auto_update_last_sync:
			self_moving.append(shift.name)
		elif not shift.last_sync_of_checkin:
			not_started.append(shift.name)
		elif not _movable(shift, floor):
			behind.append(shift.name)
		else:
			moved.append(shift.name)
	return {
		"disabled_holding": not server.enabled and bool(server.imported_up_to),
		"waiting_for": [other.name for other in servers if not other.imported_up_to],
		"moved": moved,
		"self_moving": self_moving,
		"not_started": not_started,
		"behind": behind,
		"floor": str(floor.date()) if floor else None,
	}


def _servers() -> list:
	"""The servers whose punches every moved Shift Type waits for.

	Pull servers with Import Punches on. A disabled one still counts once it has imported,
	because its punches may still come: unticking Import Punches is what releases it. Agent
	mode does not count until agent ingest exists.
	"""
	servers = frappe.get_all(
		"BioTime Server",
		filters={"import_punches": 1, "mode": "Pull"},
		fields=["name", "enabled", "imported_up_to", "import_from", "attendance_buffer_minutes"],
	)
	return [server for server in servers if server.enabled or server.imported_up_to]


def _target(servers: list) -> datetime | None:
	"""The earliest server reach minus its buffer, or None until every server has one."""
	if not servers or any(not server.imported_up_to for server in servers):
		return None
	return min(
		get_datetime(server.imported_up_to) - timedelta(minutes=server.attendance_buffer_minutes or 0)
		for server in servers
	)


def _floor(servers: list) -> datetime | None:
	"""Midnight of the latest Import From: before it, some server's punches are not in Frappe."""
	dates = [getdate(server.import_from) for server in servers if server.import_from]
	return datetime.combine(max(dates), time.min) if dates else None
