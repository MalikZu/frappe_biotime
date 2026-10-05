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
					frappe.show_alert({
						message: __("{0} terminals synced", [message]),
						indicator: "green",
					});
					frm.reload_doc();
				}),
			actions
		);
		if (frm.doc.mode === "Pull" && frm.doc.import_punches) {
			frm.add_custom_button(
				__("Import Now"),
				() =>
					frm
						.call("import_now")
						.then(() =>
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
		const notes = attendance_notes(frm.doc.__onload?.attendance || {});
		if (frm.doc.waiting_punches) {
			notes.unshift(
				__("{0} punches are waiting to become checkins. Each import tries them again.", [
					frm.doc.waiting_punches,
				])
			);
		}
		frm.set_intro(notes.join("<br>"), "orange");
	},
});

function attendance_notes(attendance) {
	const list = (names) => names.map((name) => frappe.utils.escape_html(name)).join(", ");
	const floor = attendance.floor
		? frappe.datetime.str_to_user(attendance.floor)
		: __("Import From");
	const notes = [];
	if (attendance.disabled_holding) {
		notes.push(
			__(
				"This server is disabled, but attendance stays held at its Imported Up To, because its punches may still come. Untick Import Punches to release it."
			)
		);
	}
	if (attendance.waiting_for?.length) {
		notes.push(
			__("Attendance does not move until these servers have imported once: {0}.", [
				list(attendance.waiting_for),
			])
		);
	}
	if (attendance.self_moving?.length) {
		notes.push(
			__(
				"These shift types move their own Last Sync of Checkin, which can mark people Absent before late punches arrive: {0}. Turn off Auto Update Last Sync on them.",
				[list(attendance.self_moving)]
			)
		);
	}
	if (attendance.not_started?.length) {
		notes.push(
			__(
				"These shift types have no Last Sync of Checkin, so Frappe HR marks no attendance for them yet: {0}. Set Process Attendance After to {1} or later first, then Last Sync of Checkin. The app moves it from there.",
				[list(attendance.not_started), floor]
			)
		);
	}
	if (attendance.behind?.length) {
		notes.push(
			__(
				"The app leaves these shift types alone, because moving them would mark days before {1} Absent: {0}. Set their Process Attendance After to {1} or later.",
				[list(attendance.behind), floor]
			)
		);
	}
	return notes;
}

function start_over(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Start Over"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__(
					"Use this after BioTime's database was restored or reinstalled. Imports read BioTime again from this date: pick the day the restore lost data from, or a little earlier. Punches already in Frappe are recognized by their time and are not imported twice."
				)}</p>`,
			},
			{
				fieldname: "from_date",
				fieldtype: "Date",
				label: __("Read Again From"),
				reqd: 1,
				// Not Import From: reading everything since then can outlast the job's timeout.
				default: frappe.datetime.add_days(
					frappe.datetime.get_today(),
					-(frm.doc.lookback_days || 3)
				),
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
