# Copyright (c) 2026, Malik AlZubaidi and Contributors
# See license.txt

import frappe
from erpnext.setup.doctype.employee.test_employee import make_employee

from frappe_biotime.biotime.report.biotime_employee_mapping import biotime_employee_mapping as mapping
from frappe_biotime.tests.utils import BioTimeTestCase


class TestEmployeeMapping(BioTimeTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.department = self.fake.add_department(code="1", name="Head office")
		self.area = self.fake.add_area(code="1", name="Main")

	def test_each_code_next_to_its_employee(self) -> None:
		self.person("1001", "Sara", "Khan")
		self.person("2002", "Omar", "Haddad")
		self.person("2003", "Layla Nasser", "Saeed")
		self.person("2004", "Zaid", "Odeh")
		omar = self.employee("omar", "Omar", "Haddad")
		layla = self.employee("layla", "Layla", "Saeed")
		self.punch("2002", 8)
		self.punch("9999", 9)
		self.run_import()

		rows = self.rows(show="All")

		self.assertEqual((rows["1001"]["match"], rows["1001"]["employee"]), (mapping.LINKED, self.sara))
		self.assertEqual(
			(rows["2002"]["match"], rows["2002"]["employee"], rows["2002"]["waiting"]),
			(mapping.SAME_NAME, omar, 1),
		)
		self.assertEqual(
			(rows["2002"]["biotime_name"], rows["2002"]["department"]), ("Omar Haddad", "Head office")
		)
		self.assertEqual((rows["2003"]["match"], rows["2003"]["employee"]), (mapping.SIMILAR_NAME, layla))
		self.assertEqual((rows["2004"]["match"], rows["2004"]["employee"]), (mapping.NO_MATCH, None))
		self.assertEqual((rows["9999"]["match"], rows["9999"]["waiting"]), (mapping.NOT_IN_BIOTIME, 1))
		# Codes with waiting punches come first.
		self.assertEqual(list(rows)[:2], ["2002", "9999"])

	def test_linked_codes_are_left_out_unless_all_are_shown(self) -> None:
		self.person("1001", "Sara", "Khan")
		self.person("2002", "Omar", "Haddad")

		self.assertEqual(list(self.rows()), ["2002"])

	def test_an_employee_matching_several_codes_is_suggested_for_none(self) -> None:
		self.person("2002", "Omar", "Haddad")
		self.person("2005", "Omar", "Haddad")
		self.employee("omar", "Omar", "Haddad")

		rows = self.rows()

		self.assertEqual({rows["2002"]["match"], rows["2005"]["match"]}, {mapping.NO_MATCH})

	def test_names_match_without_case_accents_or_arabic_variants(self) -> None:
		self.assertEqual(mapping._words("أحمد  الحسن"), mapping._words("احمد الحسن"))
		self.assertEqual(mapping._words("فاطمة"), mapping._words("فاطمه"))
		self.assertEqual(mapping._words("Zoë AL-Hassan"), mapping._words("zoe al hassan"))

	def test_unreadable_biotime_lists_the_waiting_codes(self) -> None:
		self.punch("9999", 9)
		self.run_import()
		self.fake.fail_next(500, path="/personnel/api/employees/")

		_columns, rows, note = mapping.execute({"server": self.server.name})

		self.assertEqual(
			[(row["code"], row["match"], row["waiting"]) for row in rows], [("9999", mapping.NO_MATCH, 1)]
		)
		self.assertIn("could not be read", note)

	def test_agent_servers_are_not_read(self) -> None:
		self.person("2002", "Omar", "Haddad")
		self.server.db_set("mode", "Agent")

		_columns, rows, note = mapping.execute({"server": self.server.name})

		self.assertEqual(rows, [])
		self.assertIn("Agent mode", note)

	def test_linking_a_code_lets_its_waiting_punches_in(self) -> None:
		omar = self.employee("omar", "Omar", "Haddad")
		self.punch("2002", 8)
		self.run_import()

		result = mapping.link_codes([{"code": "2002", "employee": omar}])

		self.assertEqual(result, {"linked": [omar], "refused": []})
		self.assertEqual(frappe.db.get_value("Employee", omar, "attendance_device_id"), "2002")
		self.assertEqual(self.run_import().from_waiting, 1)

	def test_linking_keeps_every_code_where_it_is(self) -> None:
		omar = self.employee("omar", "Omar", "Haddad")

		# Sara has a code already, and 1001 is hers.
		result = mapping.link_codes(
			[{"code": "2002", "employee": self.sara}, {"code": "1001", "employee": omar}]
		)

		self.assertEqual((result["linked"], len(result["refused"])), ([], 2))
		self.assertEqual(frappe.db.get_value("Employee", self.sara, "attendance_device_id"), "1001")
		self.assertFalse(frappe.db.get_value("Employee", omar, "attendance_device_id"))

	def test_linking_needs_write_access_to_the_employee(self) -> None:
		omar = self.employee("omar", "Omar", "Haddad")

		with self.set_user("Guest"):
			self.assertRaises(
				frappe.PermissionError, mapping.link_codes, [{"code": "2002", "employee": omar}]
			)

	def person(self, code: str, first_name: str, last_name: str) -> None:
		self.fake.add_employee(
			emp_code=code,
			department_id=self.department,
			area_ids=[self.area],
			first_name=first_name,
			last_name=last_name,
		)

	def employee(self, user: str, first_name: str, last_name: str) -> str:
		return make_employee(
			f"biotime.{user}@example.com", company="_Test Company", first_name=first_name, last_name=last_name
		)

	def rows(self, show: str = "Needs a match") -> dict[str, dict]:
		_columns, rows, _note = mapping.execute({"server": self.server.name, "show": show})
		return {row["code"]: row for row in rows}
