# Copyright (c) 2026, Malik AlZubaidi and contributors
# For license information, please see license.txt

import contextlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime
from redis.exceptions import LockError

from frappe_biotime.biotime import connection, watermark
from frappe_biotime.biotime.employees import enqueue_push_all
from frappe_biotime.biotime.punches import STRICT_LOG_TYPE, enqueue_import, import_lock, site_time


class BioTimeServer(Document):
	def onload(self) -> None:
		self.set_onload("attendance", watermark.notes(self))

	def validate(self) -> None:
		if self.timezone:
			try:
				ZoneInfo(self.timezone)
			except ZoneInfoNotFoundError, ValueError:
				frappe.throw(_("{0} is not a timezone name, such as Asia/Dubai.").format(self.timezone))
		if self.lookback_days is not None and self.lookback_days < 0:
			frappe.throw(_("Lookback cannot be negative."))
		if self.push_employees:
			for mapping, code, label in (
				(self.department_mapping, self.default_department_code, _("Default Department Code")),
				(self.area_mapping, self.default_area_code, _("Default Area Code")),
			):
				if mapping == "Fixed code" and not code:
					frappe.throw(_("Set {0} to push employees with a fixed code.").format(label))
		self.warn_about_strict_shifts()

	def warn_about_strict_shifts(self) -> None:
		if not self.import_punches or self.skip_auto_attendance or self.log_type_mode != "Leave blank":
			return
		strict = frappe.get_all(
			"Shift Type", filters={"determine_check_in_and_check_out": STRICT_LOG_TYPE}, pluck="name"
		)
		if strict:
			frappe.msgprint(
				_(
					"These shift types need a log type on every checkin: {0}. This server leaves it "
					"blank, so Frappe HR may refuse its punches in those shifts or leave them out of "
					"attendance. Map punch states or set terminal directions instead."
				).format(", ".join(strict)),
				title=_("Log type"),
				indicator="orange",
			)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Log in, and report what the server shows about its version."""
		with connection.connect(self) as client:
			info = client.server_info()
		frappe.db.set_value(self.doctype, self.name, "detected_version", info.version, update_modified=False)
		return {
			"version": info.version,
			"docs_title": info.docs_title,
			"has_resigns": info.has_resigns,
		}

	@frappe.whitelist()
	def sync_terminals(self) -> int:
		"""Add BioTime's terminals to the table and refresh their details. Returns the count."""
		# Start from the stored record: the browser's copy may hold stale status and read state.
		self.reload()
		with connection.connect(self) as client:
			terminals = list(client.terminals.list())
		rows = {row.serial_number: row for row in self.terminals}
		for terminal in terminals:
			row = rows.get(terminal.sn) or self.append("terminals", {"serial_number": terminal.sn})
			row.alias = terminal.alias
			row.area_name = terminal.area_name or (terminal.area.area_name if terminal.area else None)
			row.ip_address = terminal.ip_address
			row.state = None if terminal.state is None else str(terminal.state)
			row.last_activity = site_time(terminal.last_activity) if terminal.last_activity else None
		self.save()
		return len(terminals)

	@frappe.whitelist()
	def import_now(self) -> None:
		"""Queue an import now, and ask it to try every waiting punch again."""
		self.check_permission("write")
		# Asked through the server: a run that is already queued would drop a new job's arguments.
		frappe.db.set_value(
			self.doctype, self.name, "retry_requested_at", now_datetime(), update_modified=False
		)
		# Committed before queueing, so a run that starts at once sees the request.
		frappe.db.commit()  # nosemgrep
		enqueue_import(self.name)

	@frappe.whitelist()
	def push_all_employees(self) -> None:
		"""Queue a push of every employee with an Attendance Device ID to this server."""
		self.check_permission("write")
		if not (self.enabled and self.push_employees and self.mode == "Pull"):
			frappe.throw(_("Push All Employees needs an enabled Pull server with Push Employees on."))
		enqueue_push_all(self.name)

	@frappe.whitelist()
	def start_over(self, from_date: str) -> None:
		"""Read BioTime again from `from_date`, after its database was restored or reinstalled.

		New transaction keys get the next generation, so they never match old ones. Punches
		read again are recognized by their time.
		"""
		self.check_permission("write")
		if not from_date:
			frappe.throw(_("Choose the date to read BioTime again from."))
		from_date = getdate(from_date)
		lock = import_lock(self.name)
		if not lock.acquire(blocking=False):
			frappe.throw(_("An import is running. Try again in a minute."))
		try:
			# Start from the stored record, not the browser's copy with its stale status.
			self.reload()
			self.key_generation = (self.key_generation or 1) + 1
			self.read_state = None
			self.import_from = from_date
			self.save()
			# Committed while the lock is held, so the queued run cannot read the old state.
			frappe.db.commit()  # nosemgrep
		finally:
			with contextlib.suppress(LockError):
				lock.release()
		enqueue_import(self.name)
