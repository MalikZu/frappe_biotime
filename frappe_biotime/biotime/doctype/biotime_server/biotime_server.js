// Copyright (c) 2026, Malik AlZubaidi and contributors
// For license information, please see license.txt

frappe.ui.form.on("BioTime Server", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		const actions = __("Actions");
		frm.add_custom_button(
			__("Test Connection"),
			() =>
				frm.call("test_connection").then(({ message }) => {
					frappe.msgprint({
						title: __("Connected"),
						indicator: "green",
						message: [
							__("Version: {0}", [message.version || __("not shown by the server")]),
							__("Resign API: {0}", [message.has_resigns ? __("yes") : __("no")]),
						].join("<br>"),
					});
					frm.reload_doc();
				}),
			actions
		);
		frm.add_custom_button(
			__("Sync Terminals"),
			() =>
				frm.call("sync_terminals").then(({ message }) => {
					frappe.show_alert({ message: __("{0} terminals synced", [message]), indicator: "green" });
					frm.reload_doc();
				}),
			actions
		);
		if (frm.doc.mode === "Pull" && frm.doc.import_punches) {
			frm.add_custom_button(
				__("Import Now"),
				() =>
					frm.call("import_now").then(() =>
						frappe.show_alert({ message: __("Import queued"), indicator: "blue" })
					),
				actions
			);
			frm.add_custom_button(__("Start Over"), () => start_over(frm), actions);
		}
		frm.add_custom_button(
			__("Waiting Punches"),
			() => frappe.set_route("List", "BioTime Pending Punch", { server: frm.doc.name }),
			actions
		);
		frm.set_intro(
			frm.doc.waiting_punches
				? __("{0} punches are waiting to become checkins. Each import tries them again.", [
						frm.doc.waiting_punches,
				  ])
				: "",
			"orange"
		);
	},
});

function start_over(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Start Over"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__(
					"Use this after BioTime's database was restored or reinstalled. Imports read BioTime again from this date. Punches already in Frappe are recognized by their time and are not imported twice."
				)}</p>`,
			},
			{
				fieldname: "from_date",
				fieldtype: "Date",
				label: __("Read Again From"),
				reqd: 1,
				default: frm.doc.import_from,
			},
		],
		primary_action_label: __("Start Over"),
		primary_action({ from_date }) {
			frm.call("start_over", { from_date }).then(() => {
				dialog.hide();
				frappe.show_alert({ message: __("Import queued"), indicator: "blue" });
				frm.reload_doc();
			});
		},
	});
	dialog.show();
}
