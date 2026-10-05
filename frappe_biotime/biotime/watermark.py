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

from frappe_biotime.biotime.punches import AFTER_RELIEVING, INACTIVE, PENDING, site_time

if TYPE_CHECKING:
	from frappe_biotime.biotime.doctype.biotime_server.biotime_server import BioTimeServer


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


def reach(server: "BioTimeServer", started: datetime, terminals: list | None, error: str | None) -> Reach:
	"""How far `server` has imported, given the terminals read before its punches.

	The earliest of: the import's start; the last contact of each terminal whose punches are
	imported, since an offline terminal may still hold punches; and the oldest waiting punch
	within the server's hold. Waiting punches of Inactive or Left employees do not hold,
	because Frappe HR marks no attendance for them.
	"""
	if terminals is None:
		return Reach(None, f"BioTime's terminals could not be read: {error}"[:140])
	rows = {row.serial_number: row for row in server.terminals}
	candidates = [(started, "This import's start")]
	for terminal in terminals:
		row = rows.get(terminal.sn)
		if terminal.last_activity is None or (row is not None and not row.import_punches):
			continue
		seen = site_time(terminal.last_activity)
		candidates.append((seen, f"Terminal {terminal.sn}, last seen {format_datetime(seen)}"))
	waiting = _holding_punch(server, started)
	if waiting:
		punched = get_datetime(waiting.punch_time)
		candidates.append(
			(punched, f"Waiting punch of {waiting.emp_code} at {format_datetime(punched)}: {waiting.reason}")
		)
	at, held_back_by = min(candidates, key=lambda candidate: candidate[0])
	return Reach(at, held_back_by[:140])


def _holding_punch(server: "BioTimeServer", started: datetime) -> Any:
	if not server.attendance_hold_hours:
		return None
	table = DocType(PENDING)
	rows = (
		frappe.qb.from_(table)
		.select(table.emp_code, table.punch_time, table.reason)
		.where(table.server == server.name)
		.where(table.reason.notin([INACTIVE, AFTER_RELIEVING]))
		.where(table.punch_time >= add_to_date(started, hours=-server.attendance_hold_hours))
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
	for shift in frappe.get_all(
		"Shift Type",
		filters={"enable_auto_attendance": 1, "auto_update_last_sync": 0},
		fields=["name", "last_sync_of_checkin"],
	):
		current = get_datetime(shift.last_sync_of_checkin) if shift.last_sync_of_checkin else None
		# Never start a shift, or carry it over days before the import began: Frappe HR
		# would mark those days Absent.
		if current is None or (floor and current < floor) or current >= target:
			continue
		# Without touching modified: waiting punches are retried when a Shift Type changes.
		frappe.db.set_value("Shift Type", shift.name, "last_sync_of_checkin", target, update_modified=False)


def notes(server: "BioTimeServer") -> dict[str, Any]:
	"""What the server form says about the Shift Types this app moves."""
	if not (server.enabled and server.import_punches):
		return {}
	servers = _servers()
	floor = _floor(servers)
	moved, self_moving, not_started, behind = [], [], [], []
	for shift in frappe.get_all(
		"Shift Type",
		filters={"enable_auto_attendance": 1},
		fields=["name", "last_sync_of_checkin", "auto_update_last_sync"],
		order_by="name asc",
	):
		if shift.auto_update_last_sync:
			self_moving.append(shift.name)
		elif not shift.last_sync_of_checkin:
			not_started.append(shift.name)
		elif floor and get_datetime(shift.last_sync_of_checkin) < floor:
			behind.append(shift.name)
		else:
			moved.append(shift.name)
	return {
		"waiting_for": [other.name for other in servers if not other.imported_up_to],
		"moved": moved,
		"self_moving": self_moving,
		"not_started": not_started,
		"behind": behind,
		"floor": str(floor.date()) if floor else None,
	}


def _servers() -> list:
	"""The servers whose punches every moved Shift Type waits for."""
	return frappe.get_all(
		"BioTime Server",
		filters={"enabled": 1, "import_punches": 1},
		fields=["name", "imported_up_to", "import_from", "attendance_buffer_minutes"],
	)


def _target(servers: list) -> datetime | None:
	"""The earliest server reach minus its buffer, or None until every server has one."""
	if not servers or any(not server.imported_up_to for server in servers):
		return None
	return min(
		get_datetime(server.imported_up_to) - timedelta(minutes=server.attendance_buffer_minutes or 0)
		for server in servers
	)


def _floor(servers: list) -> datetime | None:
	"""Midnight of the earliest Import From: no punch from before it is in Frappe."""
	dates = [getdate(server.import_from) for server in servers if server.import_from]
	return datetime.combine(min(dates), time.min) if dates else None
