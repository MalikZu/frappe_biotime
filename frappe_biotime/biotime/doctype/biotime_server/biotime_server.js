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
		}
	},
});
