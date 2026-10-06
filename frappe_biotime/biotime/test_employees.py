# Copyright (c) 2026, Malik AlZubaidi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from erpnext.setup.doctype.employee.test_employee import make_employee
from frappe.utils import add_days, today
from pybiotime import BadRequestError, TransportError
from pybiotime._sync.personnel import Areas
from pybiotime.testing import _personnel as fake_personnel

from frappe_biotime.biotime import employees
from frappe_biotime.tests.utils import DAY, BioTimeTestCase


class TestEmployeePush(BioTimeTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.server.push_employees = 1
		self.server.save()
		# BioTime needs an area for everyone: Sara's comes from her branch.
		self.set(self.sara, branch=self.branch())

	def test_saving_an_employee_queues_a_push_after_the_commit(self) -> None:
		self.assertEqual(self.pushes_queued(first_name="Sara"), [self.sara])
		# BioTime holds no copy of this field.
		self.assertEqual(self.pushes_queued(employee_number="E-77"), [])

		self.server.db_set("push_employees", 0)
		self.assertEqual(self.pushes_queued(first_name="Sarah"), [])

	def test_a_push_that_cannot_be_queued_never_fails_the_save(self) -> None:
		with (
			patch.object(frappe, "enqueue", side_effect=RuntimeError("Too many queued background jobs")),
			patch.object(frappe, "log_error") as log_error,
		):
			employees._queue_push(self.sara)

		log_error.assert_called_once()

	def test_a_push_creates_the_person_with_their_department_area_and_position(self) -> None:
		department = frappe.db.get_value("Employee", self.sara, "department")
		self.set(self.sara, first_name="Sara", last_name="Khan", designation=self.designation())

		employees.push_employee(self.sara)

		(person,) = self.fake.employees
		self.assertEqual(
			(person["emp_code"], person["first_name"], person["last_name"]), ("1001", "Sara", "Khan")
		)
		self.assertEqual(self.code("departments", person["department"]), department)
		self.assertEqual([self.code("areas", area) for area in person["area"]], ["_Test BioTime Gate"])
		self.assertEqual(self.code("positions", person["position"]), "_Test BioTime Guard")
		self.server.reload()
		self.assertEqual(self.server.last_push_message, f"{self.sara}: created in BioTime")

	def test_pushing_again_updates_the_same_person(self) -> None:
		employees.push_employee(self.sara)
		self.set(self.sara, last_name="Haddad")

		employees.push_employee(self.sara)

		(person,) = self.fake.employees
		self.assertEqual(person["last_name"], "Haddad")

	def test_cleared_fields_are_cleared_in_biotime(self) -> None:
		self.set(self.sara, last_name="Khan", designation=self.designation())
		employees.push_employee(self.sara)
		self.set(self.sara, last_name="", designation=None)

		employees.push_employee(self.sara)

		(person,) = self.fake.employees
		self.assertEqual((person["last_name"], person["position"]), ("", None))

	def test_a_push_adds_an_area_and_takes_none_away(self) -> None:
		# HR gave Sara a second area in BioTime: its terminals must keep knowing her.
		self.person("1001", "Sara")

		employees.push_employee(self.sara)

		(person,) = self.fake.employees
		self.assertEqual(
			sorted(self.code("areas", area) for area in person["area"]), ["EAST", "_Test BioTime Gate"]
		)

	def test_codes_are_matched_as_the_database_matches_them(self) -> None:
		# The import maps punches with code 2002 to Sara, whose code was typed in Arabic digits.
		self.person("2002", "Sara")
		self.set(self.sara, attendance_device_id="٢٠٠٢")

		employees.push_employee(self.sara)

		self.assertEqual([person["emp_code"] for person in self.fake.employees], ["2002"])

		# A new code typed that way reaches BioTime in digits 0-9.
		omar = make_employee(
			"biotime.omar@example.com",
			company="_Test Company",
			attendance_device_id="٣٠٠٣",
			branch=self.branch(),
		)
		employees.push_employee(omar)

		self.assertEqual(sorted(person["emp_code"] for person in self.fake.employees), ["2002", "3003"])

	def test_employees_without_a_code_or_of_another_company_are_not_pushed(self) -> None:
		employees.push_employee(make_employee("biotime.nocode@example.com", company="_Test Company"))
		self.server.db_set("company", "_Test Company 1")
		employees.push_employee(self.sara)

		self.assertEqual(self.fake.employees, [])

	def test_a_fixed_code_must_exist_in_biotime(self) -> None:
		self.server.update({"department_mapping": "Fixed code", "default_department_code": "HQ"})
		self.server.save()

		employees.push_employee(self.sara)

		self.assertEqual(self.fake.employees, [])
		self.server.reload()
		self.assertIn("HQ", self.server.last_push_message)

		head_office = self.fake.add_department(code="HQ", name="Head office")
		employees.push_employee(self.sara)

		self.assertEqual(self.fake.employees[0]["department"], head_office)

	def test_a_fixed_code_needs_its_default(self) -> None:
		self.server.area_mapping = "Fixed code"
		self.assertRaises(frappe.ValidationError, self.server.save)

	def test_an_employee_who_left_is_resigned_and_brought_back(self) -> None:
		employees.push_employee(self.sara)
		self.set(self.sara, status="Left", relieving_date=DAY.date())

		employees.push_employee(self.sara)
		employees.push_employee(self.sara)

		(resign,) = self.fake.resigns
		self.assertEqual(resign["employee"], self.fake.employees[0]["id"])

		self.set(self.sara, status="Active", relieving_date=None)
		employees.push_employee(self.sara)

		self.assertEqual(self.fake.resigns, [])
		self.server.reload()
		self.assertIn("reinstated", self.server.last_push_message)

	def test_only_an_active_employee_is_reinstated(self) -> None:
		employees.push_employee(self.sara)
		self.set(self.sara, status="Left", relieving_date=DAY.date())
		employees.push_employee(self.sara)
		self.set(self.sara, status="Suspended", relieving_date=None)

		employees.push_employee(self.sara)

		self.assertEqual(len(self.fake.resigns), 1)

	def test_a_leaver_changes_in_biotime_after_their_last_day(self) -> None:
		employees.push_employee(self.sara)
		self.set(self.sara, status="Left", relieving_date=add_days(today(), 3))

		employees.push_employee(self.sara)

		self.assertEqual(self.fake.resigns, [])
		self.server.reload()
		self.assertIn("leaving after", self.server.last_push_message)

		# Their last day has passed: the daily job pushes them, and they are resigned.
		self.set(self.sara, relieving_date=add_days(today(), -1))
		with patch.object(frappe, "enqueue") as enqueue:
			employees.push_leavers()
		self.assertIn(self.sara, [call.kwargs["employee"] for call in enqueue.call_args_list])
		employees.push_employee(self.sara)

		self.assertEqual(len(self.fake.resigns), 1)

	def test_leavers_can_be_kept_or_deleted(self) -> None:
		employees.push_employee(self.sara)
		self.set(self.sara, status="Left", relieving_date=DAY.date())

		self.server.db_set("on_employee_left", employees.NOTHING)
		employees.push_employee(self.sara)
		self.assertEqual((len(self.fake.employees), self.fake.resigns), (1, []))

		self.server.db_set("on_employee_left", employees.DELETE)
		employees.push_employee(self.sara)
		self.assertEqual(self.fake.employees, [])

	def test_servers_without_the_resign_api_say_so(self) -> None:
		self.fake.resign_api = False
		employees.push_employee(self.sara)
		self.set(self.sara, status="Left", relieving_date=DAY.date())

		employees.push_employee(self.sara)

		self.server.reload()
		self.assertIn("cannot resign", self.server.last_push_message)

	def test_a_changed_code_waits_until_biotime_has_it(self) -> None:
		employees.push_employee(self.sara)
		# A new code and a new name in one save: still the same person in BioTime.
		self.save(self.sara, attendance_device_id="1002", last_name="Haddad")

		employees.push_employee(self.sara)

		self.assertEqual(self.names(), {"1001": "biotime.sara@example.com"})
		self.server.reload()
		self.assertIn("change their code to 1002 in BioTime", self.server.last_push_message)

		# HR changes the code in BioTime, so the fingerprints stay with her.
		self.fake.employees[0]["emp_code"] = "1002"
		employees.push_employee(self.sara)

		(person,) = self.fake.employees
		self.assertEqual((person["emp_code"], person["last_name"]), ("1002", "Haddad"))

	def test_a_corrected_code_never_takes_over_the_other_person(self) -> None:
		# 2005 in BioTime is a contractor. Sara gets it by mistake, and her push renames him.
		self.person("2005", "Zaid", "Odeh")
		self.save(self.sara, attendance_device_id="2005")
		employees.push_employee(self.sara)
		self.save(self.sara, attendance_device_id="2050")

		employees.push_employee(self.sara)

		# The app changes no code, so the contractor keeps his: HR fixes the rest in BioTime.
		self.assertEqual(list(self.names()), ["2005"])
		self.server.reload()
		self.assertIn("add 2050 there", self.server.last_push_message)

		self.person("2050", "Sara")
		employees.push_employee(self.sara)

		self.assertEqual(sorted(self.names()), ["2005", "2050"])

	def test_two_pushes_creating_the_same_area_both_succeed(self) -> None:
		def created_meanwhile(resource, code, name, **kwargs):
			# Another push creates the area between this one's lookup and its create.
			self.fake.add_area(code=code, name=name)
			raise BadRequestError(
				"area with this area_code already exists.",
				field_errors={"area_code": ["area with this area_code already exists."]},
				status_code=400,
				method="POST",
				path=Areas.path,
			)

		with patch.object(Areas, "upsert", created_meanwhile):
			employees.push_employee(self.sara)

		(person,) = self.fake.employees
		self.assertEqual([self.code("areas", area) for area in person["area"]], ["_Test BioTime Gate"])

	def test_a_failed_status_write_never_stops_the_push(self) -> None:
		branch = frappe.get_doc(
			{
				"doctype": "BioTime Server",
				"server_name": "_Test BioTime Branch",
				"url": "http://branch.test",
				"push_employees": 1,
			}
		).insert()
		save_status = employees._save_status

		def lock_wait_on_the_first(server, **values) -> None:
			if server == self.server.name:
				raise RuntimeError("Lock wait timeout exceeded")
			save_status(server, **values)

		with (
			patch.object(employees, "_save_status", side_effect=lock_wait_on_the_first),
			patch.object(frappe.db, "rollback"),
		):
			employees.push_employee(self.sara)

		self.assertTrue(frappe.db.get_value("BioTime Server", branch.name, "last_push_message"))

	def test_an_employee_still_changing_after_the_last_round_is_pushed_again(self) -> None:
		def save_meanwhile(server, doc) -> None:
			frappe.db.set_value("Employee", doc.name, "employee_number", frappe.generate_hash(length=6))

		with (
			patch.object(employees, "_push_and_report", side_effect=save_meanwhile),
			patch.object(frappe, "enqueue") as enqueue,
		):
			employees.push_employee(self.sara)

		self.assertEqual(enqueue.call_args.kwargs["job_id"], f"biotime:push-again:{self.sara}")

	def test_error_logs_hold_no_biotime_secrets(self) -> None:
		self.fake.fail_next(400, path="/personnel/api/employees/")

		employees.push_employee(self.sara)

		errors = frappe.get_all(
			"Error Log", filters={"method": ["like", "BioTime push failed%"]}, pluck="error"
		)
		self.assertTrue(errors)
		self.assertTrue(self.fake.tokens)
		for token in self.fake.tokens:
			self.assertFalse([error for error in errors if token in error])

	def test_push_all_sends_every_employee_with_a_code(self) -> None:
		make_employee(
			"biotime.omar@example.com",
			company="_Test Company",
			attendance_device_id="2002",
			branch=self.branch(),
		)
		self.server.db_set("company", "_Test Company")

		with patch.object(frappe, "enqueue") as enqueue:
			self.server.push_all_employees()
		employees.push_all(enqueue.call_args.kwargs["server"])

		codes = {person["emp_code"] for person in self.fake.employees}
		self.assertLessEqual({"1001", "2002"}, codes)
		self.server.reload()
		self.assertIn("created in BioTime", self.server.last_push_message)

	def test_push_all_goes_on_in_parts(self) -> None:
		make_employee(
			"biotime.omar@example.com",
			company="_Test Company",
			attendance_device_id="2002",
			branch=self.branch(),
		)
		self.server.db_set("company", "_Test Company")

		with patch.object(employees, "PUSH_ALL_SECONDS", -1), patch.object(frappe, "enqueue") as enqueue:
			employees.push_all(self.server.name)
			self.server.reload()
			self.assertIn("Still running", self.server.last_push_message)
			# Every part pushes one employee and queues the rest, until none is left.
			while enqueue.call_args_list:
				call = enqueue.call_args_list.pop(0)
				employees.push_all(
					**{key: call.kwargs[key] for key in ("server", "after", "counts", "problems")}
				)

		self.assertLessEqual({"1001", "2002"}, {person["emp_code"] for person in self.fake.employees})
		self.server.reload()
		self.assertNotIn("Still running", self.server.last_push_message)

	def test_push_all_stops_when_biotime_cannot_be_reached(self) -> None:
		with patch.object(employees, "_push", side_effect=TransportError("Connection refused")) as push:
			employees.push_all(self.server.name)

		push.assert_called_once()
		self.server.reload()
		self.assertIn("could not be reached", self.server.last_push_message)

	def test_a_push_all_part_on_a_server_that_stopped_pushing_says_so(self) -> None:
		self.server.db_set("push_employees", 0)

		employees.push_all(self.server.name, after=self.sara, counts={"created in BioTime": 3})

		self.server.reload()
		self.assertIn("no longer pushes", self.server.last_push_message)
		self.assertNotIn("Still running", self.server.last_push_message)

	def test_push_all_needs_an_enabled_server_that_pushes(self) -> None:
		self.server.db_set("enabled", 0)
		self.assertRaises(frappe.ValidationError, self.server.push_all_employees)

		self.server.db_set({"enabled": 1, "push_employees": 0})
		self.assertRaises(frappe.ValidationError, self.server.push_all_employees)

	def pushes_queued(self, **values) -> list[str]:
		"""Save Sara with `values`; return the employees whose push waits for the commit."""
		with patch.object(frappe.db, "after_commit") as after_commit:
			self.save(self.sara, **values)
		return [
			call.args[0].args[0]
			for call in after_commit.add.call_args_list
			if getattr(call.args[0], "func", None) is employees._queue_push
		]

	def save(self, employee: str, **values) -> None:
		"""Save the employee with `values`, through its hooks."""
		doc = frappe.get_doc("Employee", employee)
		doc.update(values)
		# Tests skip the change history unless asked; a push reads old codes from it.
		doc.save(ignore_version=False)

	def set(self, employee: str, **values) -> None:
		"""Change the employee without hooks."""
		frappe.db.set_value("Employee", employee, values)

	def person(self, code: str, first_name: str, last_name: str | None = None) -> None:
		"""A person already in BioTime, in a department and area of BioTime's own."""
		department = self.fake.add_department(code="OPS", name="Operations")
		area = self.fake.add_area(code="EAST", name="East gate")
		self.fake.add_employee(
			emp_code=code,
			department_id=department,
			area_ids=[area],
			first_name=first_name,
			last_name=last_name,
		)

	def names(self) -> dict[str, str]:
		return {person["emp_code"]: person.get("first_name") for person in self.fake.employees}

	def branch(self) -> str:
		if not frappe.db.exists("Branch", "_Test BioTime Gate"):
			frappe.get_doc({"doctype": "Branch", "branch": "_Test BioTime Gate"}).insert()
		return "_Test BioTime Gate"

	def designation(self) -> str:
		if not frappe.db.exists("Designation", "_Test BioTime Guard"):
			frappe.get_doc({"doctype": "Designation", "designation_name": "_Test BioTime Guard"}).insert()
		return "_Test BioTime Guard"

	def code(self, kind: str, object_id: int) -> str:
		spec = fake_personnel._CODED[kind]
		return next(row[spec.code] for row in getattr(self.fake, kind) if row["id"] == object_id)
