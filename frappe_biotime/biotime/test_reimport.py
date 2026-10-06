# Copyright (c) 2026, Malik AlZubaidi and Contributors
# See license.txt

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import add_days, get_datetime, get_system_timezone, now_datetime, today
from hrms.hr.doctype.shift_type.test_shift_type import setup_shift_type

from frappe_biotime.biotime import punches, reimport
from frappe_biotime.tests.utils import DAY, GATE, BioTimeTestCase

SEPTEMBER = datetime(2026, 9, 1)


class TestReimport(BioTimeTestCase):
	def test_reads_days_before_import_from(self) -> None:
		self.fake.add_transaction(emp_code="1001", punch_time=SEPTEMBER.replace(hour=8), terminal_sn=GATE)
		self.punch("1001", 9)
		self.run_import()
		read_state = punches._read_state(self.server)

		self.reimport("2026-09-01", "2026-10-01")
		self.run_import()

		self.assertEqual([c.time for c in self.checkins()], [SEPTEMBER.replace(hour=8), DAY.replace(hour=9)])
		self.assertFalse(self.server.reimport_request)
		self.assertTrue(self.server.reimport_status.startswith("Done"), self.server.reimport_status)
		self.assertIn("31 of 31 days", self.server.reimport_status)
		self.assertIn("Imported 1. Already in Frappe: 1.", self.server.reimport_status)
		# The imports go on from where they were.
		self.assertEqual(punches._read_state(self.server), read_state)

	def test_reads_only_the_codes_asked_for(self) -> None:
		for code in ("1001", "9999"):
			self.fake.add_transaction(emp_code=code, punch_time=SEPTEMBER.replace(hour=8), terminal_sn=GATE)

		self.reimport("2026-09-01", "2026-09-01", codes="1001,\n1001")
		self.run_import()

		self.assertEqual([c.time for c in self.checkins()], [SEPTEMBER.replace(hour=8)])
		self.assertEqual(self.waiting(), [])
		self.assertIn("employee codes 1001.", self.server.reimport_status)

	def test_brings_in_the_punches_of_a_terminal_turned_back_on(self) -> None:
		self.server.append("terminals", {"serial_number": GATE, "import_punches": 0})
		self.server.save()
		self.punch("1001", 8)
		self.assertEqual(self.run_import().terminal_off, 1)
		self.server.terminals[0].import_punches = 1
		self.server.save()
		# The imports read each punch once, so they do not bring it back.
		self.run_import()
		self.assertEqual(self.checkins(), [])

		self.reimport("2026-10-01", "2026-10-01")
		self.run_import()

		self.assertEqual(len(self.checkins()), 1)

	def test_a_day_is_a_day_on_this_sites_clock(self) -> None:
		# BioTime runs far behind this site, so this site's first hours are the day before there.
		self.server.timezone = "Etc/GMT+12"
		self.server.save()
		for punched in (datetime(2026, 8, 31, 23), datetime(2026, 9, 1, 1)):
			there = punched.replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(ZoneInfo("Etc/GMT+12"))
			self.fake.add_transaction(
				emp_code="1001", punch_time=there.replace(tzinfo=None), terminal_sn=GATE
			)

		self.reimport("2026-09-01", "2026-09-01")
		self.run_import()

		self.assertEqual([c.time for c in self.checkins()], [datetime(2026, 9, 1, 1)])

	def test_a_long_range_goes_on_over_several_imports(self) -> None:
		self.enterContext(patch.object(reimport, "REIMPORT_SECONDS", 0))
		for day in (1, 2, 3):
			self.fake.add_transaction(
				emp_code="1001", punch_time=SEPTEMBER.replace(day=day), terminal_sn=GATE
			)
		self.reimport("2026-09-01", "2026-09-03")

		self.run_import()

		self.assertEqual(len(self.checkins()), 1)
		self.assertTrue(
			self.server.reimport_status.startswith("Read 1 of 3 days"), self.server.reimport_status
		)
		self.run_import()
		self.run_import()
		self.assertEqual(len(self.checkins()), 3)
		self.assertTrue(self.server.reimport_status.startswith("Done"), self.server.reimport_status)
		self.assertFalse(self.server.reimport_request)

	def test_a_failed_day_is_read_again_by_the_next_import(self) -> None:
		self.fake.add_transaction(emp_code="1001", punch_time=SEPTEMBER.replace(hour=8), terminal_sn=GATE)
		self.reimport("2026-09-01", "2026-09-01")
		self.fake.fail_next(500, path="/iclock/api/transactions/")

		# Returns, so the import goes on with its own work.
		reimport.run(self.server, now_datetime(), ZoneInfo(get_system_timezone()))

		self.server.reload()
		self.assertTrue(self.server.reimport_status.startswith("Failed on"), self.server.reimport_status)
		self.assertTrue(self.server.reimport_request)
		self.run_import()
		self.assertEqual(len(self.checkins()), 1)
		self.assertTrue(self.server.reimport_status.startswith("Done"), self.server.reimport_status)

	def test_stop(self) -> None:
		self.fake.add_transaction(emp_code="1001", punch_time=SEPTEMBER.replace(hour=8), terminal_sn=GATE)
		self.reimport("2026-09-01", "2026-09-03")

		self.server.stop_reimport()

		self.server.reload()
		self.assertFalse(self.server.reimport_request)
		self.assertTrue(self.server.reimport_status.startswith("Stopped by Administrator"))
		self.run_import()
		self.assertEqual(self.checkins(), [])
		self.assertRaises(frappe.ValidationError, self.server.stop_reimport)

	def test_a_stop_while_a_day_is_read_is_kept(self) -> None:
		self.fake.add_transaction(emp_code="1001", punch_time=SEPTEMBER.replace(hour=8), terminal_sn=GATE)
		self.reimport("2026-09-01", "2026-09-03")
		store_punches = reimport.store_punches

		def store_and_stop(*args) -> None:
			store_punches(*args)
			reimport.stop(frappe.get_doc("BioTime Server", self.server.name))

		with patch.object(reimport, "store_punches", side_effect=store_and_stop):
			self.run_import()

		self.assertFalse(self.server.reimport_request)
		self.assertTrue(self.server.reimport_status.startswith("Stopped"), self.server.reimport_status)
		self.assertEqual(len(self.checkins()), 1)

	def test_old_punches_it_leaves_waiting_do_not_stop_attendance(self) -> None:
		shift = setup_shift_type(shift_type="_Test BioTime Day", last_sync_of_checkin=DAY).name
		self.run_import()
		# An unknown code, so its punch waits, and new, so it would hold attendance.
		self.fake.add_transaction(emp_code="9999", punch_time=SEPTEMBER.replace(hour=8), terminal_sn=GATE)
		self.reimport("2026-09-01", "2026-09-01")

		self.run_import()

		self.assertEqual([w.emp_code for w in self.waiting()], ["9999"])
		self.assertEqual(self.server.held_back_by, "This import's start")
		last_sync = get_datetime(frappe.db.get_value("Shift Type", shift, "last_sync_of_checkin"))
		self.assertEqual(last_sync, self.server.imported_up_to - timedelta(minutes=60))

	def test_checks_what_it_is_asked(self) -> None:
		for from_date, to_date, codes in (
			("", "2026-09-01", None),
			("2026-09-02", "2026-09-01", None),
			("2026-09-01", add_days(today(), 1), None),
			("2026-09-01", "2026-09-01", " ".join(str(code) for code in range(reimport.MAX_CODES + 1))),
		):
			self.assertRaises(frappe.ValidationError, self.reimport, from_date, to_date, codes)
		self.reimport("2026-09-01", "2026-09-01")
		self.assertRaises(frappe.ValidationError, self.reimport, "2026-09-01", "2026-09-01")

	def test_needs_an_enabled_server(self) -> None:
		frappe.db.set_value("BioTime Server", self.server.name, "enabled", 0)

		self.assertRaises(frappe.ValidationError, self.reimport, "2026-09-01", "2026-09-01")

	def test_needs_write_permission(self) -> None:
		with self.set_user("Guest"):
			self.assertRaises(frappe.PermissionError, self.reimport, "2026-09-01", "2026-09-01")
			self.assertRaises(frappe.PermissionError, self.server.stop_reimport)

	def reimport(self, from_date: str, to_date: str, codes: str | None = None) -> None:
		with patch.object(frappe, "enqueue"):
			self.server.reimport_punches(from_date, to_date, codes)
		self.server.reload()
