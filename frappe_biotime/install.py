"""Custom fields this app adds to Frappe HR doctypes."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Employee Checkin": [
		{
			"fieldname": "biotime_section",
			"fieldtype": "Section Break",
			"label": "BioTime",
			"collapsible": 1,
		},
		{
			"fieldname": "biotime_server",
			"fieldtype": "Link",
			"label": "BioTime Server",
			"options": "BioTime Server",
			"insert_after": "biotime_section",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "biotime_uid",
			"fieldtype": "Data",
			"label": "BioTime Transaction",
			"description": "Server, key generation and transaction id. Each BioTime punch is imported once.",
			"insert_after": "biotime_server",
			"unique": 1,
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "biotime_column_break",
			"fieldtype": "Column Break",
			"insert_after": "biotime_uid",
		},
		{
			"fieldname": "biotime_punch_state",
			"fieldtype": "Data",
			"label": "BioTime Punch State",
			"insert_after": "biotime_column_break",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "biotime_verify_type",
			"fieldtype": "Int",
			"label": "BioTime Verify Type",
			"insert_after": "biotime_punch_state",
			"read_only": 1,
			"no_copy": 1,
		},
	]
}


def after_install() -> None:
	create_custom_fields(CUSTOM_FIELDS, update=True)


def after_migrate() -> None:
	create_custom_fields(CUSTOM_FIELDS, update=True)


def before_uninstall() -> None:
	for doctype, fields in CUSTOM_FIELDS.items():
		names = [f"{doctype}-{field['fieldname']}" for field in fields]
		for name in names:
			frappe.delete_doc("Custom Field", name, ignore_missing=True, force=True)
		frappe.clear_cache(doctype=doctype)
