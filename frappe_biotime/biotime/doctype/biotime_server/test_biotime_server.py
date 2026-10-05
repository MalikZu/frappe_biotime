# Copyright (c) 2026, Malik AlZubaidi and Contributors
# See license.txt

import json
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import frappe
from erpnext.setup.doctype.employee.test_employee import make_employee
from frappe.utils import get_system_timezone
from hrms.hr.doctype.employee_checkin.test_employee_checkin import make_shift_location
from hrms.hr.doctype.shift_assignment import shift_assignment
from hrms.hr.doctype.shift_type.test_shift_type import make_shift_assignment, setup_shift_type
from hrms.tests.utils import HRMSTestSuite
from pybiotime import BioTimeClient, TokenAuth
from pybiotime.testing import FakeBioTime

from frappe_biotime.biotime import punches
from frappe_biotime.biotime.doctype.biotime_server import biotime_server
from frappe_biotime.install import CUSTOM_FIELDS

GATE = "GATE0000001"
DAY = datetime(2026, 10, 1)


# Frappe HR's test base: its bootstrap creates _Test Company and friends once, and each
# test is rolled back in tearDown. IntegrationTestCase would instead walk every link from
# BioTime Server, through Company, into apps this bench may not have.
class TestBioTimeServer(HRMSTestSuite):
	def setUp(self) -> None:
		# The import commits per batch; keep each test's data inside its transaction.
		self.enterContext(patch.object(frappe.db, "commit"))
		self.fake = FakeBioTime()
		self.fake.add_terminal(sn=GATE, alias="Main gate")
		self.enterContext(patch.object(punches, "connect", side_effect=self.client_for))
		self.enterContext(patch.object(biotime_server, "connect", side_effect=self.client_for))
		self.server = frappe.get_doc(
			{
				"doctype": "BioTime Server",
				"server_name": "_Test BioTime",
				"url": "http://biotime.test",
				"username": "api",
				"password": "secret",
				"import_from": date(2026, 10, 1),
			}
		).insert()
		self.sara = make_employee(
			"biotime.sara@example.com", company="_Test Company", attendance_device_id="1001"
		)

	def client_for(self, server) -> BioTimeClient:
		return BioTimeClient(
			"http://biotime.test",
			auth=TokenAuth("api", "secret"),
			timezone=server.timezone or None,
			transport=self.fake.transport(),
			page_size=1000,
		)

	def punch(self, emp_code: str, hour: int, minute: int = 0, **fields) -> None:
		fields.setdefault("terminal_sn", GATE)
		self.fake.add_transaction(
			emp_code=emp_code, punch_time=DAY.replace(hour=hour, minute=minute), **fields
		)

	def run_import(self) -> punches.ImportCounts:
		counts = punches.import_punches(self.server.name)
		self.server.reload()
		self.assertIsNotNone(counts, self.server.last_run_message)
		return counts

	def checkins(self, **filters) -> list:
		return frappe.get_all(
			"Employee Checkin",
			filters={"biotime_server": self.server.name, **filters},
			fields=["*"],
			order_by="time asc",
		)

	def test_custom_fields_exist(self) -> None:
		meta = frappe.get_meta("Employee Checkin")
		for field in CUSTOM_FIELDS["Employee Checkin"]:
			self.assertTrue(meta.has_field(field["fieldname"]), field["fieldname"])

	def test_first_import_stores_checkins(self) -> None:
		self.punch("1001", 8, punch_state="0", verify_type=15)
		self.punch("1001", 17, punch_state="1")
		self.punch("9999", 9)

		counts = self.run_import()

		self.assertEqual(counts.imported, 2)
		self.assertEqual(counts.unmapped, 1)
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
		self.assertEqual(self.server.unmapped_codes, "9999")
		self.assertIn("Imported 2.", self.server.last_run_message)
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

	def test_skips_are_counted_not_imported(self) -> None:
		left = make_employee(
			"biotime.left@example.com",
			company="_Test Company",
			attendance_device_id="1002",
		)
		frappe.db.set_value("Employee", left, {"status": "Left", "relieving_date": date(2026, 10, 1)})
		other = make_employee(
			"biotime.other@example.com", company="_Test Company 1", attendance_device_id="1003"
		)
		self.server.company = "_Test Company"
		self.server.append("terminals", {"serial_number": "OFF0000001", "import_punches": 0})
		self.server.save()
		self.fake.add_terminal(sn="OFF0000001")
		self.punch("1002", 8)  # on the relieving date: imported
		self.fake.add_transaction(emp_code="1002", punch_time=datetime(2026, 10, 2, 8, 0), terminal_sn=GATE)
		self.punch("1003", 8)
		self.punch("1001", 9, terminal_sn="OFF0000001")

		counts = self.run_import()

		self.assertEqual(counts.imported, 1)
		self.assertEqual(counts.after_relieving, 1)
		self.assertEqual(counts.other_company, 1)
		self.assertEqual(counts.terminal_off, 1)
		self.assertEqual([c.employee for c in self.checkins()], [left])
		self.assertFalse(frappe.db.exists("Employee Checkin", {"employee": other}))

	def test_same_time_already_logged_is_skipped(self) -> None:
		frappe.get_doc(
			{"doctype": "Employee Checkin", "employee": self.sara, "time": DAY.replace(hour=8)}
		).insert()
		self.punch("1001", 8)

		counts = self.run_import()

		self.assertEqual(counts.duplicate, 1)
		self.assertEqual(counts.imported, 0)

	def test_blank_log_types_in_a_strict_shift_are_counted(self) -> None:
		# Frappe HR 16 leaves this setting out of the shift its checkin validation reads, so it
		# accepts blank log types there; Frappe HR 15 refuses them. Put it back to get the refusal.
		get_shift_type = shift_assignment.get_shift_type

		def with_strict_setting(name):
			shift_type = get_shift_type(name)
			shift_type.determine_check_in_and_check_out = frappe.db.get_value(
				"Shift Type", name, "determine_check_in_and_check_out"
			)
			return shift_type

		self.enterContext(patch.object(shift_assignment, "get_shift_type", with_strict_setting))
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

		self.assertEqual((counts.imported, counts.log_type_required), (1, 1))
		self.assertEqual([c.time.hour for c in self.checkins()], [20])
		self.assertEqual(frappe.get_message_log(), [])

	@HRMSTestSuite.change_settings("HR Settings", {"allow_geolocation_tracking": 1})
	def test_geolocation_refusals_are_counted(self) -> None:
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

		self.assertEqual((counts.imported, counts.outside_radius, counts.no_location), (1, 1, 1))
		(checkin,) = self.checkins()
		self.assertEqual((checkin.device_id, checkin.latitude, checkin.longitude), ("NEAR000001", 25.2, 55.3))

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
