# Copyright (c) 2026, Malik AlZubaidi and Contributors
# See license.txt

from unittest.mock import patch

import frappe

from frappe_biotime.install import CUSTOM_FIELDS
from frappe_biotime.tests.utils import GATE, BioTimeTestCase


class TestBioTimeServer(BioTimeTestCase):
	def test_custom_fields_exist(self) -> None:
		meta = frappe.get_meta("Employee Checkin")
		for field in CUSTOM_FIELDS["Employee Checkin"]:
			self.assertTrue(meta.has_field(field["fieldname"]), field["fieldname"])

	def test_sync_terminals(self) -> None:
		self.server.reload()
		self.assertEqual(self.server.sync_terminals(), 1)

		row = self.server.terminals[0]
		self.assertEqual((row.serial_number, row.alias, row.import_punches), (GATE, "Main gate", 1))

	def test_test_connection_reports_the_version(self) -> None:
		result = self.server.test_connection()

		self.assertEqual(result["version"], "9.5")
		self.assertEqual(frappe.db.get_value("BioTime Server", self.server.name, "detected_version"), "9.5")

	def test_rejects_unknown_timezones(self) -> None:
		self.server.timezone = "Mars/Olympus"
		self.assertRaises(frappe.ValidationError, self.server.save)

	def test_import_now_tries_every_waiting_punch(self) -> None:
		with patch.object(frappe, "enqueue") as enqueue:
			self.server.import_now()

		self.assertEqual(enqueue.call_args.kwargs["server"], self.server.name)
		self.assertTrue(enqueue.call_args.kwargs["retry_all"])
