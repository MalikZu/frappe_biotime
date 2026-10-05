# Copyright (c) 2026, Malik AlZubaidi and Contributors
# See license.txt

from datetime import date
from unittest.mock import patch

import frappe
from hrms.tests.utils import HRMSTestSuite
from pybiotime import BioTimeClient, TokenAuth
from pybiotime.testing import FakeBioTime

from frappe_biotime.biotime.doctype.biotime_server import biotime_server

GATE = "GATE0000001"


# Frappe HR's test base: its bootstrap creates _Test Company and friends once, and each
# test is rolled back in tearDown. IntegrationTestCase would instead walk every link from
# BioTime Server, through Company, into apps this bench may not have.
class TestBioTimeServer(HRMSTestSuite):
	def setUp(self) -> None:
		self.fake = FakeBioTime()
		self.fake.add_terminal(sn=GATE, alias="Main gate")
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

	def client_for(self, server) -> BioTimeClient:
		return BioTimeClient(
			"http://biotime.test",
			auth=TokenAuth("api", "secret"),
			timezone=server.timezone or None,
			transport=self.fake.transport(),
			page_size=1000,
		)

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
