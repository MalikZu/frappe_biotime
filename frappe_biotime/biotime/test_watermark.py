# Copyright (c) 2026, Malik AlZubaidi and Contributors
# See license.txt

from datetime import date, datetime, timedelta
from unittest.mock import patch

import frappe
from erpnext.setup.doctype.employee.test_employee import make_employee
from frappe.utils import add_to_date, get_datetime, now_datetime
from hrms.hr.doctype.shift_type.test_shift_type import setup_shift_type
from hrms.tests.utils import HRMSTestSuite

from frappe_biotime.biotime import punches, watermark
from frappe_biotime.tests.utils import DAY, GATE, BioTimeTestCase

IMPORT_FROM = datetime(2026, 10, 1)


class TestAttendanceWatermark(BioTimeTestCase):
	def test_a_current_server_moves_attendance_to_its_import_start(self) -> None:
		shift = self.shift("_Test BioTime Day", last_sync=IMPORT_FROM)
		self.punch("1001", 8)

		self.run_import()

		self.assertEqual(self.server.held_back_by, "This import's start")
		self.assertEqual(self.last_sync(shift), self.server.imported_up_to - timedelta(minutes=60))

	def test_an_offline_terminal_holds_attendance(self) -> None:
		shift = self.shift("_Test BioTime Day", last_sync=IMPORT_FROM)
		self.fake.add_terminal(sn="DOOR000001", last_activity="2026-10-03 18:00:00")

		self.run_import()

		self.assertEqual(self.server.imported_up_to, datetime(2026, 10, 3, 18, 0))
		self.assertIn("DOOR000001", self.server.held_back_by)
		self.assertEqual(self.last_sync(shift), datetime(2026, 10, 3, 17, 0))

	def test_terminals_left_out_do_not_hold_attendance(self) -> None:
		self.fake.add_terminal(sn="OFF0000001", last_activity="2026-10-02 08:00:00")
		self.server.append("terminals", {"serial_number": "OFF0000001", "import_punches": 0})
		self.server.save()

		self.run_import()

		self.assertEqual(self.server.held_back_by, "This import's start")

	def test_a_recent_waiting_punch_holds_attendance(self) -> None:
		recent = now_datetime().replace(microsecond=0) - timedelta(hours=2)
		self.fake.add_transaction(emp_code="9999", punch_time=recent, terminal_sn=GATE)

		self.run_import()

		self.assertEqual(self.server.imported_up_to, recent)
		self.assertIn("9999", self.server.held_back_by)

	def test_old_and_inactive_waiting_punches_do_not_hold_attendance(self) -> None:
		idle = make_employee("biotime.idle@example.com", company="_Test Company", attendance_device_id="1004")
		frappe.db.set_value("Employee", idle, "status", "Inactive")
		recent = now_datetime().replace(microsecond=0) - timedelta(hours=2)
		self.fake.add_transaction(emp_code="1004", punch_time=recent, terminal_sn=GATE)
		self.punch("9999", 8)  # days ago, older than the 24-hour hold

		self.run_import()

		self.assertEqual(len(self.waiting()), 2)
		self.assertEqual(self.server.held_back_by, "This import's start")

	def test_attendance_waits_for_every_server(self) -> None:
		shift = self.shift("_Test BioTime Day", last_sync=IMPORT_FROM)
		other = frappe.get_doc(
			{
				"doctype": "BioTime Server",
				"server_name": "_Test BioTime Branch",
				"url": "http://branch.test",
				"import_from": date(2026, 10, 1),
			}
		).insert()

		self.run_import()

		self.assertEqual(self.last_sync(shift), IMPORT_FROM)
		self.assertEqual(watermark.notes(self.server)["waiting_for"], [other.name])

		frappe.db.set_value("BioTime Server", other.name, "imported_up_to", datetime(2026, 10, 2, 12, 0))
		watermark.move_shift_types()

		self.assertEqual(self.last_sync(shift), datetime(2026, 10, 2, 11, 0))

	def test_shift_types_that_must_not_move(self) -> None:
		moved = self.shift("_Test BioTime Moved", last_sync=IMPORT_FROM)
		not_started = self.shift("_Test BioTime New", last_sync=None)
		behind = self.shift(
			"_Test BioTime Behind",
			last_sync=datetime(2026, 9, 30, 23, 0),
			process_attendance_after=date(2026, 9, 1),
		)
		# Frappe HR marks nothing before Process Attendance After, so this one is safe to move.
		safe = self.shift(
			"_Test BioTime Safe",
			last_sync=datetime(2026, 9, 30, 23, 0),
			process_attendance_after=date(2026, 10, 1),
		)
		self_moving = self.shift("_Test BioTime Self", last_sync=IMPORT_FROM, auto_update_last_sync=1)
		tomorrow = add_to_date(now_datetime(), days=1).replace(microsecond=0)
		ahead = self.shift("_Test BioTime Ahead", last_sync=tomorrow)

		self.run_import()

		self.assertGreater(self.last_sync(moved), IMPORT_FROM)
		self.assertIsNone(self.last_sync(not_started))
		self.assertEqual(self.last_sync(behind), datetime(2026, 9, 30, 23, 0))
		self.assertGreater(self.last_sync(safe), IMPORT_FROM)
		self.assertEqual(self.last_sync(self_moving), IMPORT_FROM)
		self.assertEqual(self.last_sync(ahead), tomorrow)
		notes = watermark.notes(self.server)
		self.assertIn(moved, notes["moved"])
		self.assertIn(safe, notes["moved"])
		self.assertIn(not_started, notes["not_started"])
		self.assertIn(behind, notes["behind"])
		self.assertIn(self_moving, notes["self_moving"])
		self.assertEqual(notes["floor"], "2026-10-01")

	def test_unreadable_terminals_keep_attendance_where_it_is(self) -> None:
		shift = self.shift("_Test BioTime Day", last_sync=IMPORT_FROM)
		self.fake.fail_next(500, path="/iclock/api/terminals/")
		self.punch("1001", 8)

		counts = self.run_import()

		self.assertEqual((counts.imported, self.server.last_run_result), (1, "Success"))
		self.assertIsNone(self.server.imported_up_to)
		self.assertIn("terminals could not be read", self.server.held_back_by)
		self.assertEqual(self.last_sync(shift), IMPORT_FROM)

	def test_the_floor_is_the_latest_import_from(self) -> None:
		# The shift sits between two servers' Import From dates: before ours, none of our punches.
		shift = self.shift(
			"_Test BioTime Night", last_sync=datetime(2026, 5, 1), process_attendance_after=date(2026, 1, 1)
		)
		self.other_server(
			"_Test BioTime Branch", import_from=date(2026, 1, 1), imported_up_to=datetime(2026, 10, 2, 12, 0)
		)

		self.run_import()

		self.assertEqual(self.last_sync(shift), datetime(2026, 5, 1))
		self.assertEqual(watermark.notes(self.server)["floor"], "2026-10-01")

	def test_editing_import_from_later_keeps_the_floor(self) -> None:
		behind = self.shift(
			"_Test BioTime Behind", last_sync=datetime(2026, 9, 1), process_attendance_after=date(2026, 9, 1)
		)
		self.run_import()
		self.assertEqual(self.server.imported_from, date(2026, 10, 1))
		# Nothing from September is read again: the import goes on from where it was.
		self.server.import_from = date(2026, 9, 1)
		self.server.save()

		self.run_import()

		self.assertEqual(self.last_sync(behind), datetime(2026, 9, 1))
		self.assertEqual(watermark.notes(self.server)["floor"], "2026-10-01")

	def test_start_over_from_a_later_date_keeps_the_floor(self) -> None:
		self.run_import()
		# Held back before the restore, by a terminal that was offline for a while.
		held = datetime(2026, 10, 2, 8, 0)
		shift = self.shift("_Test BioTime Day", last_sync=held, process_attendance_after=date(2026, 9, 1))
		with patch.object(frappe, "enqueue"):
			self.server.start_over("2026-10-03")

		self.run_import()

		# The punches before the new date were imported before the restore.
		self.assertEqual(watermark.notes(self.server)["floor"], "2026-10-01")
		self.assertGreater(self.last_sync(shift), held)

	@HRMSTestSuite.change_settings("HR Settings", {"allow_geolocation_tracking": 1})
	def test_refusals_hold_attendance_until_resolved(self) -> None:
		self.punch("1001", 10)  # days ago, and refused: the gate has no coordinates

		self.run_import()

		self.assertEqual([w.reason for w in self.waiting()], [punches.NO_LOCATION])
		self.assertEqual(self.server.imported_up_to, DAY.replace(hour=10))

	def test_unknown_codes_seen_before_do_not_hold_attendance(self) -> None:
		recent = now_datetime().replace(microsecond=0) - timedelta(hours=2)
		self.punch("9999", 8)  # days ago: the code has waited a while, like a visitor's
		self.fake.add_transaction(emp_code="9999", punch_time=recent, terminal_sn=GATE)

		self.run_import()

		self.assertEqual(len(self.waiting()), 2)
		self.assertEqual(self.server.held_back_by, "This import's start")

	def test_a_terminal_still_uploading_its_backlog_holds_attendance(self) -> None:
		# The gate was offline and is uploading what it stored: its 10:00 punch arrives now.
		uploaded = now_datetime().replace(microsecond=0)
		self.fake.add_transaction(
			emp_code="1001", punch_time=DAY.replace(hour=10), terminal_sn=GATE, upload_time=uploaded
		)

		self.run_import()

		self.assertEqual(self.server.imported_up_to, DAY.replace(hour=10))
		self.assertIn("still uploading", self.server.held_back_by)

		# Once nothing late arrives, the hold ends.
		self.run_import()

		self.assertEqual(self.server.held_back_by, "This import's start")

	def test_terminals_that_do_not_hold_attendance(self) -> None:
		self.fake.add_terminal(sn="DOOR000001", last_activity="2026-10-03 18:00:00")
		self.server.append("terminals", {"serial_number": "DOOR000001", "holds_attendance": 0})
		self.server.save()
		self.punch("1001", 8, terminal_sn="DOOR000001")

		counts = self.run_import()

		self.assertEqual(counts.imported, 1)
		self.assertEqual(self.server.held_back_by, "This import's start")

	def test_a_disabled_server_holds_until_import_punches_is_off(self) -> None:
		shift = self.shift("_Test BioTime Day", last_sync=IMPORT_FROM)
		branch = self.other_server(
			"_Test BioTime Branch", enabled=0, imported_up_to=datetime(2026, 10, 2, 12, 0)
		)

		self.run_import()

		self.assertEqual(self.last_sync(shift), datetime(2026, 10, 2, 11, 0))
		self.assertTrue(watermark.notes(frappe.get_doc("BioTime Server", branch))["disabled_holding"])

		frappe.db.set_value("BioTime Server", branch, "import_punches", 0)
		watermark.move_shift_types()

		self.assertEqual(self.last_sync(shift), self.server.imported_up_to - timedelta(minutes=60))

	def test_agent_servers_do_not_hold_attendance(self) -> None:
		shift = self.shift("_Test BioTime Day", last_sync=IMPORT_FROM)
		self.other_server("_Test BioTime Agent", mode="Agent")

		self.run_import()

		self.assertGreater(self.last_sync(shift), IMPORT_FROM)

	def test_moving_attendance_leaves_the_shift_type_unmodified(self) -> None:
		shift = self.shift("_Test BioTime Day", last_sync=IMPORT_FROM)
		modified = frappe.db.get_value("Shift Type", shift, "modified")

		self.run_import()

		self.assertGreater(self.last_sync(shift), IMPORT_FROM)
		self.assertEqual(frappe.db.get_value("Shift Type", shift, "modified"), modified)

	def other_server(self, name: str, imported_up_to: datetime | None = None, **fields) -> str:
		server = frappe.get_doc(
			{
				"doctype": "BioTime Server",
				"server_name": name,
				"url": "http://other.test",
				"import_from": date(2026, 10, 1),
				**fields,
			}
		).insert()
		if imported_up_to:
			frappe.db.set_value("BioTime Server", server.name, "imported_up_to", imported_up_to)
		return server.name

	def shift(self, name: str, last_sync: datetime | None, **fields) -> str:
		shift = setup_shift_type(shift_type=name, last_sync_of_checkin=last_sync or now_datetime(), **fields)
		if last_sync is None:
			frappe.db.set_value("Shift Type", shift.name, "last_sync_of_checkin", None)
		return shift.name

	def last_sync(self, shift: str) -> datetime | None:
		value = frappe.db.get_value("Shift Type", shift, "last_sync_of_checkin")
		return get_datetime(value) if value else None
