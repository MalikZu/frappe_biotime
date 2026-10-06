# Copyright (c) 2026, Malik AlZubaidi and contributors
# For license information, please see license.txt

"""Match BioTime people to Frappe employees, so HR can set Attendance Device IDs fast."""

import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.query_builder.functions import Count
from frappe.utils import escape_html, strip_html
from pybiotime import BioTimeError

from frappe_biotime.biotime import connection
from frappe_biotime.biotime.punches import PENDING, UNMAPPED, _code_key, _employees_by_code

# How a code stands: the values of the Match column.
LINKED = "Linked"
SAME_NAME = "Same name"
SIMILAR_NAME = "Similar name"
NO_MATCH = "No match"
NOT_IN_BIOTIME = "Not in BioTime"

#: Arabic letters written more than one way: teh marbuta as heh, alef maksura as yeh, and
#: no tatweel. Hamza forms fold through NFKD.
_ARABIC = str.maketrans(
	{
		"\N{ARABIC LETTER TEH MARBUTA}": "\N{ARABIC LETTER HEH}",
		"\N{ARABIC LETTER ALEF MAKSURA}": "\N{ARABIC LETTER YEH}",
		"\N{ARABIC TATWEEL}": "",
	}
)


def execute(filters: dict | None = None) -> tuple:
	filters = frappe._dict(filters or {})
	if not filters.server:
		return _columns(), []
	server = frappe.get_doc("BioTime Server", filters.server)
	server.check_permission("read")
	people, note = _biotime_people(server)
	rows = _rows(server, people, _waiting(server.name))
	if filters.show != "All":
		rows = [row for row in rows if row["match"] != LINKED]
	return _columns(), rows, note


def _columns() -> list[dict]:
	return [
		{"fieldname": "code", "label": _("BioTime Code"), "fieldtype": "Data", "width": 120},
		{"fieldname": "biotime_name", "label": _("BioTime Name"), "fieldtype": "Data", "width": 200},
		{"fieldname": "department", "label": _("BioTime Department"), "fieldtype": "Data", "width": 160},
		{"fieldname": "waiting", "label": _("Waiting Punches"), "fieldtype": "Int", "width": 120},
		{"fieldname": "match", "label": _("Match"), "fieldtype": "Data", "width": 120},
		{
			"fieldname": "employee",
			"label": _("Employee"),
			"fieldtype": "Link",
			"options": "Employee",
			"width": 160,
		},
		{"fieldname": "employee_name", "label": _("Employee Name"), "fieldtype": "Data", "width": 200},
	]


def _biotime_people(server) -> tuple[dict[str, Any] | None, str | None]:
	"""BioTime's employees by code, or None and why they could not be read."""
	if server.mode != "Pull":
		return None, _(
			"This server is in Agent mode, so BioTime is not read from here. Only codes with "
			"waiting punches are listed."
		)
	try:
		with connection.connect(server) as client:
			return {person.emp_code: person for person in client.employees.list()}, None
	except BioTimeError as exc:
		return None, _("BioTime could not be read: {0}. Only codes with waiting punches are listed.").format(
			escape_html(str(exc))
		)


def _waiting(server: str) -> Counter:
	"""How many punches wait with each unknown code."""
	table = DocType(PENDING)
	rows = (
		frappe.qb.from_(table)
		.select(table.emp_code, Count("*"))
		.where((table.server == server) & (table.reason == UNMAPPED))
		.groupby(table.emp_code)
		.run()
	)
	return Counter(dict(rows))


def _rows(server, people: dict[str, Any] | None, waiting: Counter) -> list[dict]:
	"""One row per code that BioTime knows or that punches wait with, those waiting first."""
	codes = {_code_key(code): code for code in waiting}
	# BioTime's own spelling of a code wins over a punch's.
	codes.update({_code_key(code): code for code in people or {}})
	waiting_by_key = Counter()
	for code, count in waiting.items():
		waiting_by_key[_code_key(code)] += count
	linked = _employees_by_code(set(codes.values()))
	names = {code: _name(person) for code, person in (people or {}).items() if _code_key(code) not in linked}
	suggestions = _suggestions(names, _candidates(server))

	rows = []
	for key, code in codes.items():
		person = (people or {}).get(code)
		employee, match = linked.get(key), LINKED
		if employee is None:
			unknown = NOT_IN_BIOTIME if people is not None and person is None else NO_MATCH
			employee, match = suggestions.get(code, (None, unknown))
		rows.append(
			{
				"code": code,
				"biotime_name": _name(person) if person else None,
				"department": person.department.dept_name if person and person.department else None,
				"waiting": waiting_by_key[key],
				"match": match,
				"employee": employee.name if employee else None,
				"employee_name": employee.employee_name if employee else None,
			}
		)
	return sorted(rows, key=lambda row: (-row["waiting"], _code_key(row["code"])))


def _name(person) -> str:
	return " ".join(part.strip() for part in (person.first_name, person.last_name) if part and part.strip())


def _candidates(server) -> list:
	"""Employees a code can be linked to: not Left, and without an Attendance Device ID."""
	filters = {"status": ["!=", "Left"], "attendance_device_id": ["is", "not set"]}
	if server.company:
		filters["company"] = server.company
	return frappe.get_all("Employee", filters=filters, fields=["name", "employee_name"], order_by="name asc")


def _suggestions(names: dict[str, str], candidates: list) -> dict[str, tuple[Any, str]]:
	"""Per code, the one employee whose name matches its BioTime name, and how well.

	Same name: the same words, in any order. Similar name: one name is the other with more
	words, and they share at least two. An employee who matches several codes is suggested
	for none of them, so HR picks.
	"""
	words_of = [_words(employee.employee_name) for employee in candidates]
	by_words = defaultdict(list)
	by_word = defaultdict(set)
	for index, words in enumerate(words_of):
		by_words[words].append(index)
		for word in words:
			by_word[word].add(index)

	found = {}
	for code, name in names.items():
		words = _words(name)
		if not words:
			continue
		same = by_words.get(words, [])
		if same:
			if len(same) == 1:
				found[code] = (candidates[same[0]], SAME_NAME)
			continue
		near = {index for word in words for index in by_word.get(word, ())}
		similar = [
			index
			for index in near
			if len(words & words_of[index]) >= 2 and (words < words_of[index] or words_of[index] < words)
		]
		if len(similar) == 1:
			found[code] = (candidates[similar[0]], SIMILAR_NAME)

	times = Counter(employee.name for employee, _match in found.values())
	return {code: pair for code, pair in found.items() if times[pair[0].name] == 1}


def _words(name: str | None) -> frozenset[str]:
	"""The words of a name, compared without case, accents or Arabic spelling variants."""
	text = unicodedata.normalize("NFKD", name or "")
	text = "".join(char for char in text if not unicodedata.combining(char))
	return frozenset(re.findall(r"\w+", text.casefold().translate(_ARABIC)))


@frappe.whitelist(methods=["POST"])
def link_codes(links: str | list) -> dict[str, list[str]]:
	"""Set each employee's Attendance Device ID to a BioTime code.

	`links` holds {code, employee} pairs. An employee who has an Attendance Device ID keeps
	it. Returns the employees linked, and why the others were not.
	"""
	linked, refused = [], []
	for link in frappe.parse_json(links):
		code = (link.get("code") or "").strip()
		employee = frappe.get_doc("Employee", link.get("employee"))
		employee.check_permission("write")
		if not code:
			refused.append(_("{0}: no code to link.").format(employee.employee_name))
			continue
		if employee.attendance_device_id:
			refused.append(
				_("{0} already has Attendance Device ID {1}.").format(
					employee.employee_name, employee.attendance_device_id
				)
			)
			continue
		messages = len(frappe.local.message_log)
		frappe.db.savepoint("biotime_link")
		try:
			employee.attendance_device_id = code
			employee.save()
		except frappe.ValidationError as exc:
			# Such as the code being another employee's: report it, and link the rest.
			frappe.db.rollback(save_point="biotime_link")
			del frappe.local.message_log[messages:]
			refused.append(f"{employee.employee_name}: {strip_html(str(exc)).strip()}")
			continue
		linked.append(employee.name)
	return {"linked": linked, "refused": refused}
