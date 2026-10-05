"""What this app's tests share: a BioTime server backed by a fake BioTime."""

from datetime import date, datetime
from unittest.mock import patch

import frappe
from erpnext.setup.doctype.employee.test_employee import make_employee
from hrms.tests.utils import HRMSTestSuite
from pybiotime import BioTimeClient, TokenAuth
from pybiotime.testing import FakeBioTime

from frappe_biotime.biotime import connection, punches

GATE = "GATE0000001"
DAY = datetime(2026, 10, 1)


# Frappe HR's test base: its bootstrap creates _Test Company and friends once, and each
# test is rolled back in tearDown. IntegrationTestCase would instead walk every link from
# BioTime Server, through Company, into apps this bench may not have.
class BioTimeTestCase(HRMSTestSuite):
	"""A BioTime server talking to a fake BioTime, and one employee with code 1001."""

	def setUp(self) -> None:
		# The import commits per batch; keep each test's data inside its transaction.
		self.enterContext(patch.object(frappe.db, "commit"))
		self.fake = FakeBioTime()
		self.fake.add_terminal(sn=GATE, alias="Main gate")
		self.enterContext(patch.object(connection, "connect", side_effect=self.client_for))
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

	def run_import(self, retry_all: bool = False) -> punches.ImportCounts:
		counts = punches.import_punches(self.server.name, retry_all=retry_all)
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

	def waiting(self, **filters) -> list:
		return frappe.get_all(
			punches.PENDING,
			filters={"server": self.server.name, **filters},
			fields=["*"],
			order_by="punch_time asc",
		)
