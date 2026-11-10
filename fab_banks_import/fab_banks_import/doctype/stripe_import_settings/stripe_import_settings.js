frappe.ui.form.on("Stripe Import Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Sync now"), async () => {
			const { message } = await frappe.call({
				method: "fab_banks_import.stripe_import.sync_now",
				freeze: true,
				freeze_message: __("Syncing Stripe"),
			});
			frappe.msgprint({
				title: __("Stripe sync"),
				message: `<pre>${frappe.utils.escape_html(JSON.stringify(message, null, 1))}</pre>`,
			});
			frm.reload_doc();
		});

		frm.add_custom_button(__("Statement PDF"), () => {
			const today = frappe.datetime.str_to_obj(frappe.datetime.get_today());
			const dialog = new frappe.ui.Dialog({
				title: __("Stripe statement"),
				fields: [
					{
						fieldname: "year",
						fieldtype: "Int",
						label: __("Year"),
						reqd: 1,
						// default to last month, which in January is December of last year
						default: today.getFullYear() - (today.getMonth() ? 0 : 1),
					},
					{
						fieldname: "month",
						fieldtype: "Select",
						label: __("Month"),
						options: ["", ...Array.from({ length: 12 }, (_, i) => String(i + 1))].join("\n"),
						default: String(today.getMonth() || 12),
						description: __("Leave empty for the whole year"),
					},
				],
				primary_action_label: __("Download"),
				primary_action(values) {
					const params = new URLSearchParams({ year: values.year });
					if (values.month) {
						params.set("month", values.month);
					}
					window.open(
						`/api/method/fab_banks_import.stripe_import.download_statement?${params.toString()}`
					);
					dialog.hide();
				},
			});
			dialog.show();
		});

		frm.add_custom_button(__("Send last month's statement now"), async () => {
			const { message } = await frappe.call({
				method: "fab_banks_import.stripe_import.send_last_month_statement",
				freeze: true,
				freeze_message: __("Sending statement"),
			});
			frappe.msgprint(
				message.sent
					? __("Statement {0} queued for {1}", [message.period, message.recipients.join(", ")])
					: __("Statement {0} not sent: {1}", [message.period, message.reason])
			);
			frm.reload_doc();
		});
	},
});
