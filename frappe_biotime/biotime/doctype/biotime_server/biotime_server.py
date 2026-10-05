# Copyright (c) 2026, Malik AlZubaidi and contributors
# For license information, please see license.txt

import contextlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate
from redis.exceptions import LockError

from frappe_biotime.biotime import connection, watermark
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
		"""Queue an import now, and try every waiting punch again."""
		enqueue_import(self.name, retry_all=True)

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
			self.key_generation = (self.key_generation or 1) + 1
			self.read_state = None
			self.import_from = from_date
			self.save()
		finally:
			with contextlib.suppress(LockError):
				lock.release()
		enqueue_import(self.name)
