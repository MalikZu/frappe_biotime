"""Read days of BioTime punches again, by punch time, for every employee or only some.

For punches the imports left out or never read: from a terminal or a company that the
server's settings left out at the time, or from before Import From. The imports read the days,
one at a time and under their lock, and each day's punches go through the import as usual:
punches already in Frappe are recognized, and the rest become checkins or wait. The imports'
read state is not touched.
"""

import json
from collections import Counter
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import add_days, format_datetime, formatdate, getdate, now_datetime
from rq.timeouts import JobTimeoutException

from frappe_biotime.biotime import connection
from frappe_biotime.biotime.punches import (
	ImportCounts,
	Punch,
	enqueue_import,
	store_punches,
	summary,
	traceback,
)

if TYPE_CHECKING:
	from frappe_biotime.biotime.doctype.biotime_server.biotime_server import BioTimeServer

#: How long one import may go on re-importing, counted from its start. It reads at least one
#: day, so a re-import moves on however long the import took; the rest waits for the next.
REIMPORT_SECONDS = 600
#: Each code is one BioTime read per day. For more, re-import everyone.
MAX_CODES = 20
#: The counts a re-import adds up over its days.
COUNTED = ("imported", "already_there", "terminal_off", "other_company")


def request(server: "BioTimeServer", from_date: Any, to_date: Any, codes: str | None = None) -> None:
	"""Ask the imports to read the punches from `from_date` to `to_date` again.

	`codes` limits it to these employee codes, separated by commas, spaces or new lines.
	"""
	if not (server.enabled and server.mode == "Pull" and server.import_punches):
		frappe.throw(_("Re-import needs an enabled Pull server with Import Punches on."))
	if not (from_date and to_date):
		frappe.throw(_("Choose the days to re-import."))
	from_date, to_date = getdate(from_date), getdate(to_date)
	if from_date > to_date:
		frappe.throw(_("From Date must be on or before To Date."))
	if to_date > getdate():
		frappe.throw(_("To Date cannot be after today."))
	codes = list(dict.fromkeys((codes or "").replace(",", " ").split()))
	if len(codes) > MAX_CODES:
		frappe.throw(
			_("Choose at most {0} employee codes, or leave them empty to re-import everyone.").format(
				MAX_CODES
			)
		)
	# Locked, so two requests cannot both find that none is running.
	if _stored(server.name, lock=True):
		frappe.throw(_("A re-import is running. Stop it first, or wait until it is done."))
	new = {"from": str(from_date), "to": str(to_date), "codes": codes, "next": str(from_date), "counts": {}}
	# Saved without the document: a new modified time would make the next import try every
	# waiting punch again.
	frappe.db.set_value(
		server.doctype,
		server.name,
		{
			"reimport_request": json.dumps(new),
			"reimport_status": f"Starts at the next import: {_described(new)}.",
		},
		update_modified=False,
	)
	server.add_comment("Info", _("Re-import requested: {0}.").format(_described(new)))
	# Committed before queueing, so a run that starts at once sees the request.
	frappe.db.commit()  # nosemgrep
	enqueue_import(server.name)


def stop(server: "BioTimeServer") -> None:
	"""Stop the server's re-import. What it imported stays."""
	stored = _stored(server.name, lock=True)
	if not stored:
		frappe.throw(_("No re-import is running."))
	stopped = f"Stopped by {frappe.session.user} at {format_datetime(now_datetime())}"
	frappe.db.set_value(
		server.doctype,
		server.name,
		{"reimport_request": None, "reimport_status": f"{stopped}, after {_progress(stored)}"},
		update_modified=False,
	)
	server.add_comment("Info", _("Re-import stopped."))


def run(server: "BioTimeServer", started: datetime, site_timezone: ZoneInfo) -> None:
	"""Go on with the server's re-import until it is done or the import has run REIMPORT_SECONDS.

	The import calls this under its lock, after storing its own punches. Each day read is saved,
	so the next import goes on from the day after.
	"""
	stored = _stored(server.name)
	if not stored:
		return
	deadline = started + timedelta(seconds=REIMPORT_SECONDS)
	try:
		with connection.connect(server) as client:
			while stored:
				stored = _read_day(server, client, stored, started, site_timezone)
				if now_datetime() >= deadline:
					break
	except JobTimeoutException:
		raise
	except Exception as exc:
		# The next import reads the day again; this one goes on with its own work.
		frappe.log_error(
			title=f"BioTime re-import failed for {server.name}",
			message=traceback(),
			reference_doctype=server.doctype,
			reference_name=server.name,
		)
		# A fresh read view for the status: a failed day leaves no writes behind, and Import Now
		# or a push may have written this server's row meanwhile.
		frappe.db.commit()  # nosemgrep
		if stored:
			error = f"{type(exc).__name__}: {exc}"[:300]
			failed = f"Failed on {formatdate(stored['next'])}, tried again at the next import: {error}"
			_save(server.name, stored, stored, f"{failed}. Read {_progress(stored)}")


def _read_day(
	server: "BioTimeServer", client, stored: dict, stamp: datetime, site_timezone: ZoneInfo
) -> dict | None:
	"""Read the next day of `stored` again. Returns the request as it is now, None once none is."""
	day = getdate(stored["next"])
	start, end = datetime.combine(day, time.min), datetime.combine(day, time(23, 59, 59))
	if server.timezone:
		# As for the import's first read: pybiotime moves an aware time into BioTime's timezone.
		start, end = start.replace(tzinfo=site_timezone), end.replace(tzinfo=site_timezone)
	transactions = [
		transaction
		for code in stored["codes"] or [None]
		for transaction in client.transactions.list(start=start, end=end, emp_code=code)
	]
	# End the transaction the BioTime read kept open, as the import does: under snapshot
	# isolation, its first write would fail if another session changed that row meanwhile.
	frappe.db.commit()  # nosemgrep
	counts = ImportCounts()
	store_punches(
		server, [Punch.from_transaction(server, t, site_timezone) for t in transactions], counts, stamp
	)
	totals = {name: stored["counts"].get(name, 0) + getattr(counts, name) for name in COUNTED}
	totals["waiting"] = dict(Counter(stored["counts"].get("waiting")) + counts.waiting)
	read = {**stored, "next": str(add_days(day, 1)), "counts": totals}
	if getdate(read["next"]) > getdate(read["to"]):
		done = f"Done at {format_datetime(now_datetime())}"
		return _save(server.name, stored, None, f"{done}: read {_progress(read)}")
	return _save(server.name, stored, read, f"Read {_progress(read)}")


def _save(server: str, old: dict, new: dict | None, status: str) -> dict | None:
	"""Replace the request `old` with `new`, unless it was stopped or replaced meanwhile.

	Returns the request stored now.
	"""
	current = _stored(server, lock=True)
	if current == old:
		frappe.db.set_value(
			"BioTime Server",
			server,
			{"reimport_request": json.dumps(new) if new else None, "reimport_status": status},
			update_modified=False,
		)
		current = new
	frappe.db.commit()  # nosemgrep
	return current


def _stored(server: str, lock: bool = False) -> dict | None:
	"""The server's re-import request, if any. `lock` holds the server's row until the commit."""
	value = frappe.db.get_value("BioTime Server", server, "reimport_request", for_update=lock)
	if isinstance(value, str):
		value = json.loads(value) if value.strip() else None
	return value or None


def _described(request: dict) -> str:
	who = f"employee codes {', '.join(request['codes'])}" if request["codes"] else "all employees"
	return f"{formatdate(request['from'])} to {formatdate(request['to'])}, {who}"


def _progress(request: dict) -> str:
	"""How many days of `request` were read, and what became of their punches."""
	first, last = getdate(request["from"]), getdate(request["to"])
	read = (getdate(request["next"]) - first).days
	counts = request["counts"]
	counted = summary(
		ImportCounts(**{name: counts.get(name, 0) for name in COUNTED}), Counter(counts.get("waiting"))
	)
	return f"{read} of {(last - first).days + 1} days of {_described(request)}. {counted}"
