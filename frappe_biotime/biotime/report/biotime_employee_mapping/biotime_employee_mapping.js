// Copyright (c) 2026, Malik AlZubaidi and contributors
// For license information, please see license.txt

frappe.query_reports["BioTime Employee Mapping"] = {
	filters: [
		{
			fieldname: "server",
			label: __("BioTime Server"),
			fieldtype: "Link",
			options: "BioTime Server",
			reqd: 1,
		},
		{
			fieldname: "show",
			label: __("Show"),
			fieldtype: "Select",
			options: "Needs a match\nAll",
			default: "Needs a match",
		},
	],
	onload(report) {
		if (frappe.model.can_write("Employee")) {
			report.page.add_inner_button(__("Link Codes"), () => link_selected(report));
		}
	},
	get_datatable_options(options) {
		return Object.assign(options, { checkboxColumn: true });
	},
};

function link_selected(report) {
	const rows = frappe.query_report.datatable.rowmanager
		.getCheckedRows()
		.map((index) => frappe.query_report.data[index])
		.filter((row) => row && row.match !== "Linked");
	if (!rows.length) {
		frappe.msgprint(__("Tick the rows to link first. Rows already linked are left out."));
		return;
	}
	if (rows.length === 1) {
		choose_employee(report, rows[0]);
		return;
	}
	const links = rows
		.filter((row) => row.employee)
		.map((row) => ({ code: row.code, employee: row.employee }));
	if (!links.length) {
		frappe.msgprint(
			__("None of these rows has a suggested employee. Tick one row to choose its employee.")
		);
		return;
	}
	frappe.confirm(
		__("Give {0} suggested employees their row's code as Attendance Device ID?", [
			links.length,
		]),
		() => link_codes(report, links)
	);
}

function choose_employee(report, row) {
	const dialog = new frappe.ui.Dialog({
		title: __("Link {0}", [row.code]),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__("BioTime name: {0}", [
					frappe.utils.escape_html(row.biotime_name || __("not known")),
				])}</p>`,
			},
			{
				fieldname: "employee",
				fieldtype: "Link",
				label: __("Employee"),
				options: "Employee",
				reqd: 1,
				default: row.employee,
				get_query: () => ({
					filters: { attendance_device_id: ["is", "not set"], status: ["!=", "Left"] },
				}),
			},
		],
		primary_action_label: __("Link"),
		primary_action({ employee }) {
			dialog.hide();
			link_codes(report, [{ code: row.code, employee }]);
		},
	});
	dialog.show();
}

function link_codes(report, links) {
	frappe
		.call({
			method: "frappe_biotime.biotime.report.biotime_employee_mapping.biotime_employee_mapping.link_codes",
			args: { links },
			freeze: true,
		})
		.then(({ message }) => {
			const lines = [
				__("Employees linked: {0}. Their waiting punches import with the next import.", [
					message.linked.length,
				]),
				...message.refused.map((reason) => frappe.utils.escape_html(reason)),
			];
			frappe.msgprint({
				title: __("Link Codes"),
				message: lines.join("<br>"),
				indicator: message.refused.length ? "orange" : "green",
			});
			report.refresh();
		});
}
