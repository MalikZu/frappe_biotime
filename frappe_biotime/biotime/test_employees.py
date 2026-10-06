# Copyright (c) 2026, Malik AlZubaidi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from erpnext.setup.doctype.employee.test_employee import make_employee
from pybiotime.testing import _personnel as fake_personnel

from frappe_biotime.biotime import employees
from frappe_biotime.tests.utils import DAY, BioTimeTestCase

PUSH = "frappe_biotime.biotime.employees.push_employee"


class TestEmployeePush(BioTimeTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.server.push_employees = 1
		self.server.save()
		# BioTime needs an area for everyone: Sara's comes from her branch.
		self.set(self.sara, branch=self.branch())

	def test_saving_an_employee_queues_a_push_when_biotime_holds_the_change(self) -> None:
		self.assertEqual(self.pushes_queued(first_name="Sara"), [self.sara])
		# BioTime holds no copy of this field.
		self.assertEqual(self.pushes_queued(employee_number="E-77"), [])

		self.server.db_set("push_employees", 0)
		self.assertEqual(self.pushes_queued(first_name="Sarah"), [])

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

	def test_a_push_adds_an_area_and_takes_none_away(self) -> None:
		# HR gave Sara a second area in BioTime: its terminals must keep knowing her.
		east = self.fake.add_area(code="EAST", name="East gate")
		department = self.fake.add_department(code="OPS", name="Operations")
		self.fake.add_employee(emp_code="1001", department_id=department, area_ids=[east], first_name="Sara")

		employees.push_employee(self.sara)

		(person,) = self.fake.employees
		self.assertEqual(
			sorted(self.code("areas", area) for area in person["area"]), ["EAST", "_Test BioTime Gate"]
		)

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

	def test_leavers_can_be_kept_or_deleted(self) -> None:
		employees.push_employee(self.sara)
		self.set(self.sara, status="Left")

		self.server.db_set("on_employee_left", employees.NOTHING)
		employees.push_employee(self.sara)
		self.assertEqual((len(self.fake.employees), self.fake.resigns), (1, []))

		self.server.db_set("on_employee_left", employees.DELETE)
		employees.push_employee(self.sara)
		self.assertEqual(self.fake.employees, [])

	def test_servers_without_the_resign_api_say_so(self) -> None:
		self.fake.resign_api = False
		employees.push_employee(self.sara)
		self.set(self.sara, status="Left")

		employees.push_employee(self.sara)

		self.server.reload()
		self.assertIn("cannot resign", self.server.last_push_message)

	def test_a_changed_code_never_makes_a_second_person(self) -> None:
		employees.push_employee(self.sara)
		with patch.object(frappe, "enqueue"):
			self.save(self.sara, attendance_device_id="1002")

		employees.push_employee(self.sara)

		# BioTime 9.5 keeps codes: the person stays under the old one, and the server says so.
		self.assertEqual([person["emp_code"] for person in self.fake.employees], ["1001"])
		self.server.reload()
		self.assertIn("did not change it to 1002", self.server.last_push_message)

		# Where BioTime changes codes, the same person gets the new one.
		with patch.dict(fake_personnel._FIXED_ON_UPDATE, clear=True):
			employees.push_employee(self.sara)

		self.assertEqual([person["emp_code"] for person in self.fake.employees], ["1002"])

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

	def test_push_all_needs_push_employees(self) -> None:
		self.server.db_set("push_employees", 0)
		self.assertRaises(frappe.ValidationError, self.server.push_all_employees)

	def pushes_queued(self, **values) -> list[str]:
		"""Save Sara with `values`; return the employees whose push was queued."""
		with patch.object(frappe, "enqueue") as enqueue:
			self.save(self.sara, **values)
		return [
			call.kwargs["employee"] for call in enqueue.call_args_list if call.args and call.args[0] == PUSH
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
