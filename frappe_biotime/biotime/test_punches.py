# Copyright (c) 2026, Malik AlZubaidi and Contributors
# See license.txt

import json
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import frappe
from erpnext.setup.doctype.employee.test_employee import make_employee
from frappe.utils import add_to_date, get_system_timezone
from hrms.hr.doctype.employee_checkin.employee_checkin import EmployeeCheckin
from hrms.hr.doctype.employee_checkin.test_employee_checkin import make_shift_location
from hrms.hr.doctype.shift_assignment import shift_assignment
from hrms.hr.doctype.shift_type.test_shift_type import make_shift_assignment, setup_shift_type
from hrms.tests.utils import HRMSTestSuite

from frappe_biotime.biotime import punches
from frappe_biotime.tests.utils import DAY, GATE, BioTimeTestCase


class TestPunchImport(BioTimeTestCase):
	def test_first_import_stores_checkins(self) -> None:
		self.punch("1001", 8, punch_state="0", verify_type=15)
		self.punch("1001", 17, punch_state="1")
		self.punch("9999", 9)

		counts = self.run_import()

		self.assertEqual(counts.imported, 2)
		self.assertEqual(counts.waiting, {punches.UNMAPPED: 1})
		first, second = self.checkins()
		self.assertEqual(first.employee, self.sara)
		self.assertEqual(first.time, DAY.replace(hour=8))
		self.assertEqual(first.device_id, GATE)
		self.assertEqual(first.biotime_punch_state, "0")
		self.assertEqual(first.biotime_verify_type, 15)
		self.assertTrue(first.biotime_uid.startswith(f"{self.server.name}:"))
		self.assertFalse(first.log_type)
		self.assertEqual(second.time, DAY.replace(hour=17))
		self.assertEqual(self.server.last_run_result, "Success")
		self.assertEqual(self.server.last_imported_count, 2)
		self.assertEqual(self.server.waiting_punches, 1)
		self.assertEqual(self.server.unmapped_codes, "9999")
		self.assertEqual(self.server.last_run_message, "Imported 2. Waiting: 1 (1 Unknown device ID).")
		state = json.loads(self.server.read_state)
		self.assertIn("max_id", state)
		self.assertEqual(self.server.upload_order, state.get("upload_order"))

	def test_later_runs_import_only_new_punches_including_late_uploads(self) -> None:
		self.punch("1001", 8)
		self.run_import()
		self.assertEqual(self.run_import().imported, 0)

		# A device that was offline uploads an old punch a day later.
		self.punch("1001", 12, upload_time=datetime(2026, 10, 2, 9, 0))
		counts = self.run_import()

		self.assertEqual(counts.imported, 1)
		self.assertEqual([c.time.hour for c in self.checkins()], [8, 12])

	def test_a_waiting_punch_imports_once_its_code_maps(self) -> None:
		self.punch("2002", 8)
		self.run_import()
		(waiting,) = self.waiting()
		self.assertEqual(
			(waiting.emp_code, waiting.reason, waiting.punch_time),
			("2002", punches.UNMAPPED, DAY.replace(hour=8)),
		)

		# Nothing it depends on changed, so the next run leaves it alone.
		self.run_import()
		self.assertEqual(self.waiting()[0].last_tried, waiting.last_tried)

		dana = make_employee("biotime.dana@example.com", company="_Test Company", attendance_device_id="2002")
		counts = self.run_import()

		self.assertEqual((counts.imported, counts.from_waiting), (1, 1))
		self.assertEqual(self.waiting(), [])
		(checkin,) = self.checkins(employee=dana)
		self.assertEqual(checkin.biotime_uid, waiting.biotime_uid)
		self.assertEqual(self.server.waiting_punches, 0)
		self.assertFalse(self.server.unmapped_codes)
		self.assertEqual(self.server.last_run_message, "Imported 1, 1 of them waiting from before.")

	def test_waiting_punches_are_tried_again_daily(self) -> None:
		self.punch("2002", 8)
		self.run_import()
		(waiting,) = self.waiting()
		frappe.db.set_value(
			punches.PENDING, waiting.name, "last_tried", add_to_date(waiting.last_tried, days=-2)
		)
		# Only the daily rule can pick it up: nothing it depends on changed since.
		self.enterContext(patch.object(punches, "_settings_changed_at", return_value=datetime(2000, 1, 1)))

		self.run_import()

		self.assertGreater(self.waiting()[0].last_tried, waiting.last_tried)

	def test_retry_all_tries_every_waiting_punch(self) -> None:
		self.punch("2002", 8)
		self.run_import()
		(waiting,) = self.waiting()

		self.run_import(retry_all=True)

		self.assertGreater(self.waiting()[0].last_tried, waiting.last_tried)

	def test_reading_again_duplicates_nothing(self) -> None:
		self.punch("1001", 8)
		self.punch("2002", 9)
		self.run_import()
		# As after a crash between committing punches and saving the read state.
		frappe.db.set_value("BioTime Server", self.server.name, "read_state", None)

		counts = self.run_import()

		self.assertEqual((counts.imported, counts.already_there), (0, 2))
		self.assertEqual(len(self.checkins()), 1)
		self.assertEqual(len(self.waiting()), 1)

	def test_punches_left_out_by_the_settings_are_not_kept(self) -> None:
		other = make_employee(
			"biotime.other@example.com", company="_Test Company 1", attendance_device_id="1003"
		)
		self.server.company = "_Test Company"
		self.server.append("terminals", {"serial_number": "OFF0000001", "import_punches": 0})
		self.server.save()
		self.punch("1003", 8)
		self.punch("1001", 9, terminal_sn="OFF0000001")

		counts = self.run_import()

		self.assertEqual((counts.imported, counts.other_company, counts.terminal_off), (0, 1, 1))
		self.assertEqual(self.waiting(), [])
		self.assertFalse(frappe.db.exists("Employee Checkin", {"employee": other}))
		self.assertEqual(
			self.server.last_run_message,
			"Imported 0. Left out by this server's settings: 1 terminal not imported, "
			"1 employee of another company.",
		)

	def test_inactive_and_left_employees_wait(self) -> None:
		left = make_employee("biotime.left@example.com", company="_Test Company", attendance_device_id="1002")
		frappe.db.set_value("Employee", left, {"status": "Left", "relieving_date": date(2026, 10, 1)})
		idle = make_employee("biotime.idle@example.com", company="_Test Company", attendance_device_id="1004")
		frappe.db.set_value("Employee", idle, "status", "Inactive")
		self.punch("1002", 8)  # on the relieving date: imported
		self.fake.add_transaction(emp_code="1002", punch_time=datetime(2026, 10, 2, 8, 0), terminal_sn=GATE)
		self.punch("1004", 8)

		counts = self.run_import()

		self.assertEqual(counts.imported, 1)
		self.assertEqual(counts.waiting, {punches.AFTER_RELIEVING: 1, punches.INACTIVE: 1})
		self.assertEqual(
			{(w.employee, w.reason) for w in self.waiting()},
			{(left, punches.AFTER_RELIEVING), (idle, punches.INACTIVE)},
		)

		# The relieving date was a day early. Correcting it lets the punch in.
		frappe.db.set_value("Employee", left, "relieving_date", date(2026, 10, 2))
		counts = self.run_import()

		self.assertEqual(counts.from_waiting, 1)
		self.assertEqual([w.employee for w in self.waiting()], [idle])

	def test_a_checkin_at_the_same_time_covers_the_punch(self) -> None:
		frappe.get_doc(
			{
				"doctype": "Employee Checkin",
				"employee": self.sara,
				"time": DAY.replace(hour=8),
				"log_type": "IN",
			}
		).insert()
		self.server.log_type_mode = "Map punch states"
		self.server.save()
		self.punch("1001", 8, punch_state="1")  # mapped to OUT, but the same punch

		counts = self.run_import()

		self.assertEqual((counts.imported, counts.already_there), (0, 1))
		self.assertEqual(self.waiting(), [])

	def test_blank_log_types_in_a_strict_shift_wait(self) -> None:
		self.put_back_the_strict_setting()
		shift = setup_shift_type(
			shift_type="_Test BioTime Strict",
			start_time="07:00:00",
			end_time="16:00:00",
			determine_check_in_and_check_out=punches.STRICT_LOG_TYPE,
		)
		make_shift_assignment(shift.name, self.sara, DAY.date())
		frappe.clear_messages()
		self.server.save()
		self.assertIn("need a log type", str(frappe.get_message_log()))
		self.punch("1001", 8)  # inside the shift
		self.punch("1001", 20)  # after it
		frappe.clear_messages()

		counts = self.run_import()

		self.assertEqual(counts.imported, 1)
		self.assertEqual(counts.waiting, {punches.LOG_TYPE_REQUIRED: 1})
		self.assertEqual(frappe.get_message_log(), [])
		self.assertIn("Log Type is required", self.waiting()[0].message)

		# Mapping punch states gives the punch a log type, and changing the server retries it.
		self.server.log_type_mode = "Map punch states"
		self.server.save()
		counts = self.run_import()

		self.assertEqual(counts.from_waiting, 1)
		self.assertEqual([c.log_type or "" for c in self.checkins()], ["IN", ""])

	@HRMSTestSuite.change_settings("HR Settings", {"allow_geolocation_tracking": 1})
	def test_geolocation_refusals_wait(self) -> None:
		shift = setup_shift_type(shift_type="_Test BioTime Shift", start_time="07:00:00", end_time="16:00:00")
		site = make_shift_location("_Test BioTime Site", 25.2, 55.3, checkin_radius=500)
		make_shift_assignment(shift.name, self.sara, DAY.date(), shift_location=site.name)
		self.server.append("terminals", {"serial_number": "NEAR000001", "latitude": 25.2, "longitude": 55.3})
		self.server.append("terminals", {"serial_number": "FAR0000001", "latitude": 25.3, "longitude": 55.4})
		self.server.save()
		self.punch("1001", 8, terminal_sn="NEAR000001")
		self.punch("1001", 9, terminal_sn="FAR0000001")  # about 15 km from the site
		self.punch("1001", 10)  # the gate has no coordinates

		counts = self.run_import()

		self.assertEqual(counts.imported, 1)
		self.assertEqual(counts.waiting, {punches.OUTSIDE_RADIUS: 1, punches.NO_LOCATION: 1})
		(checkin,) = self.checkins()
		self.assertEqual((checkin.device_id, checkin.latitude, checkin.longitude), ("NEAR000001", 25.2, 55.3))

		# Giving the gate its coordinates lets its punch in.
		self.server.append("terminals", {"serial_number": GATE, "latitude": 25.2, "longitude": 55.3})
		self.server.save()
		counts = self.run_import()

		self.assertEqual(counts.from_waiting, 1)
		self.assertEqual([w.reason for w in self.waiting()], [punches.OUTSIDE_RADIUS])

	def test_an_unexpected_error_waits_and_the_run_goes_on(self) -> None:
		validate = EmployeeCheckin.validate

		def fail_at_nine(checkin) -> None:
			if checkin.time.hour == 9:
				raise RuntimeError("disk on fire")
			validate(checkin)

		self.enterContext(patch.object(EmployeeCheckin, "validate", fail_at_nine))
		self.punch("1001", 8)
		self.punch("1001", 9)
		self.punch("1001", 10)

		counts = self.run_import()

		self.assertEqual(counts.imported, 2)
		self.assertEqual(counts.waiting, {punches.ERROR: 1})
		self.assertEqual(self.waiting()[0].message, "RuntimeError: disk on fire")
		self.assertEqual(self.server.last_run_result, "Success")
		self.assertTrue(frappe.db.exists("Error Log", {"reference_name": self.server.name}))

	def test_log_type_from_punch_states(self) -> None:
		self.server.log_type_mode = "Map punch states"
		self.server.save()
		self.punch("1001", 8, punch_state="0")
		self.punch("1001", 12, punch_state="255")
		self.punch("1001", 17, punch_state="1")

		self.run_import()

		# Frappe stores a blank Select as "".
		self.assertEqual([c.log_type or "" for c in self.checkins()], ["IN", "", "OUT"])

	def test_log_type_from_terminal_direction(self) -> None:
		self.server.log_type_mode = "Terminal direction"
		self.server.append("terminals", {"serial_number": GATE, "direction": "OUT"})
		self.server.save()
		self.punch("1001", 17)

		self.run_import()

		self.assertEqual(self.checkins()[0].log_type, "OUT")

	def test_punch_times_move_to_the_site_timezone(self) -> None:
		biotime_zone = "UTC" if get_system_timezone() != "UTC" else "Asia/Dubai"
		self.server.timezone = biotime_zone
		self.server.save()
		self.punch("1001", 8)

		self.run_import()

		expected = (
			DAY.replace(hour=8, tzinfo=ZoneInfo(biotime_zone))
			.astimezone(ZoneInfo(get_system_timezone()))
			.replace(tzinfo=None)
		)
		self.assertEqual(self.checkins()[0].time, expected)

	def test_a_failed_run_keeps_the_read_state(self) -> None:
		self.punch("1001", 8)
		self.fake.fail_next(500, path="/iclock/api/transactions/")

		self.assertIsNone(punches.import_punches(self.server.name))

		self.server.reload()
		self.assertEqual(self.server.last_run_result, "Failed")
		self.assertIn("500", self.server.last_run_message)
		self.assertFalse(self.server.read_state)
		self.assertEqual(self.checkins(), [])
		# The next run reads the same punches again.
		self.assertEqual(self.run_import().imported, 1)

	def test_scheduler_queues_enabled_pull_servers(self) -> None:
		frappe.get_doc(
			{
				"doctype": "BioTime Server",
				"server_name": "_Test BioTime Agent",
				"url": "http://agent.test",
				"mode": "Agent",
				"import_from": date(2026, 10, 1),
			}
		).insert()
		with patch.object(frappe, "enqueue") as enqueue:
			punches.enqueue_imports()

		queued = [call.kwargs["server"] for call in enqueue.call_args_list]
		self.assertEqual(queued, [self.server.name])
		self.assertEqual(enqueue.call_args.kwargs["job_id"], f"biotime:import:{self.server.name}")

	def put_back_the_strict_setting(self) -> None:
		"""Frappe HR 16 leaves the strict setting out of the shift its checkin validation reads,
		so it accepts blank log types there; Frappe HR 15 refuses them. Put it back."""
		get_shift_type = shift_assignment.get_shift_type

		def with_strict_setting(name):
			shift_type = get_shift_type(name)
			shift_type.determine_check_in_and_check_out = frappe.db.get_value(
				"Shift Type", name, "determine_check_in_and_check_out"
			)
			return shift_type

		self.enterContext(patch.object(shift_assignment, "get_shift_type", with_strict_setting))

	def test_a_waiting_punch_without_a_verify_type_is_kept(self) -> None:
		self.punch("9999", 8, verify_type="")

		self.run_import()

		self.assertEqual(self.waiting()[0].verify_type, 0)

	def test_waiting_punches_carry_the_runs_stamp(self) -> None:
		self.punch("2002", 8)

		self.run_import()

		self.assertEqual(self.waiting()[0].last_tried, self.server.last_run_at)

	def test_error_punches_are_tried_again_soon(self) -> None:
		with patch.object(EmployeeCheckin, "validate", side_effect=RuntimeError("lock wait timeout")):
			self.punch("1001", 9)
			self.run_import()
		(waiting,) = self.waiting()
		self.assertEqual(waiting.reason, punches.ERROR)
		# Only the error rule can pick it up: nothing it depends on changed since.
		self.enterContext(patch.object(punches, "_settings_changed_at", return_value=datetime(2000, 1, 1)))
		frappe.db.set_value("Employee", self.sara, "modified", datetime(2000, 1, 1), update_modified=False)

		self.run_import()
		self.assertEqual(self.waiting()[0].last_tried, waiting.last_tried)

		earlier = add_to_date(waiting.last_tried, minutes=-(punches.ERROR_RETRY_MINUTES + 1))
		frappe.db.set_value(punches.PENDING, waiting.name, "last_tried", earlier)
		counts = self.run_import()

		self.assertEqual(counts.from_waiting, 1)

	def test_waiting_punches_stay_when_the_settings_now_leave_them_out(self) -> None:
		self.punch("2002", 8)
		self.run_import()
		self.server.append("terminals", {"serial_number": GATE, "import_punches": 0})
		self.server.save()

		self.run_import()

		self.assertEqual([w.reason for w in self.waiting()], [punches.LEFT_OUT])

		# Undoing the setting and linking the code lets the punch in.
		self.server.terminals[0].import_punches = 1
		self.server.save()
		make_employee("biotime.dana@example.com", company="_Test Company", attendance_device_id="2002")
		counts = self.run_import()

		self.assertEqual(counts.from_waiting, 1)
		self.assertEqual(self.waiting(), [])

	@HRMSTestSuite.change_settings("HR Settings", {"allow_geolocation_tracking": 1})
	def test_refusals_are_named_by_what_frappe_hr_checked(self) -> None:
		# As in Frappe HR 16.16: the shift it checks lacks the strict setting, so it does not
		# refuse the blank log type, and the real refusal is the missing coordinates.
		get_shift_type = shift_assignment.get_shift_type

		def without_strict_setting(name):
			shift_type = get_shift_type(name)
			shift_type.pop("determine_check_in_and_check_out", None)
			return shift_type

		self.enterContext(patch.object(shift_assignment, "get_shift_type", without_strict_setting))
		shift = setup_shift_type(
			shift_type="_Test BioTime Strict",
			start_time="07:00:00",
			end_time="16:00:00",
			determine_check_in_and_check_out=punches.STRICT_LOG_TYPE,
		)
		make_shift_assignment(shift.name, self.sara, DAY.date())
		self.punch("1001", 8)

		self.run_import()

		self.assertEqual([w.reason for w in self.waiting()], [punches.NO_LOCATION])

	def test_codes_match_whatever_their_case(self) -> None:
		make_employee("biotime.case@example.com", company="_Test Company", attendance_device_id="ab12")
		self.punch("AB12", 8)

		counts = self.run_import()

		self.assertEqual(counts.imported, 1)
		self.assertEqual(self.waiting(), [])
