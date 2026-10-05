# Copyright (c) 2026, Malik AlZubaidi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from pybiotime.testing import FakeBioTime

from frappe_biotime.biotime import punches
from frappe_biotime.install import CUSTOM_FIELDS
from frappe_biotime.tests.utils import DAY, GATE, BioTimeTestCase


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

	def test_start_over_reads_again_under_new_keys(self) -> None:
		self.punch("1001", 8, id=101)
		self.punch("1001", 9, id=102)
		self.run_import()
		# BioTime comes back from a backup taken before 9:00 and gives the next punch, at 10:00,
		# the id that 9:00 had. Under the old keys that punch would look imported already.
		self.fake = FakeBioTime()
		self.punch("1001", 8, id=101)
		self.punch("1001", 10, id=102)

		with patch.object(frappe, "enqueue") as enqueue:
			self.server.start_over(str(DAY.date()))

		self.assertEqual(enqueue.call_args.kwargs["server"], self.server.name)
		self.server.reload()
		self.assertEqual(self.server.key_generation, 2)
		self.assertFalse(self.server.read_state)
		counts = self.run_import()
		self.assertEqual((counts.imported, counts.already_there), (1, 1))
		checkins = self.checkins()
		self.assertEqual([c.time.hour for c in checkins], [8, 9, 10])
		self.assertTrue(checkins[2].biotime_uid.startswith(f"{self.server.name}:2:"))

	def test_start_over_waits_for_a_running_import(self) -> None:
		lock = punches.import_lock(self.server.name)
		self.assertTrue(lock.acquire(blocking=False))
		self.addCleanup(lock.release)

		self.assertRaises(frappe.ValidationError, self.server.start_over, str(DAY.date()))
		self.assertEqual(frappe.db.get_value("BioTime Server", self.server.name, "key_generation"), 1)

	def test_start_over_needs_a_date(self) -> None:
		self.assertRaises(frappe.ValidationError, self.server.start_over, "")

	def test_a_reinstalled_biotime_points_to_start_over(self) -> None:
		self.punch("1001", 8, id=50001)
		self.run_import()
		# BioTime is reinstalled, and its ids start again from 1.
		self.punch("1001", 10, id=1, upload_time=DAY.replace(hour=11))

		self.assertIsNone(punches.import_punches(self.server.name))

		self.server.reload()
		self.assertEqual(self.server.last_run_result, "Failed")
		self.assertTrue(self.server.last_run_message.startswith(punches.RESTORED))

	def test_start_over_does_not_duplicate_waiting_punches(self) -> None:
		self.punch("9999", 8, id=101)
		self.punch("1001", 9, id=102)
		self.run_import()
		# The employee is set Inactive later: their imported punch must not come back as waiting.
		frappe.db.set_value("Employee", self.sara, "status", "Inactive")
		self.fake = FakeBioTime()
		self.punch("9999", 8, id=101)
		self.punch("1001", 9, id=102)
		with patch.object(frappe, "enqueue"):
			self.server.start_over(str(DAY.date()))

		counts = self.run_import()

		self.assertEqual(counts.already_there, 2)
		self.assertEqual([w.emp_code for w in self.waiting()], ["9999"])
