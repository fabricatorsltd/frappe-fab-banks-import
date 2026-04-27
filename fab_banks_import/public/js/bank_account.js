frappe.ui.form.on("Bank Account", {
	refresh(frm) {
		if (!frm.doc.iban) {
			return;
		}

		frm.add_custom_button(__("Update Bank from IBAN"), async () => {
			const { message } = await frappe.call({
				method: "fab_banks_import.bank_directory.resolve_bank_from_iban",
				args: {
					iban: frm.doc.iban,
				},
				freeze: true,
				freeze_message: __("Resolving bank from IBAN"),
			});

			if (!message?.bank) {
				return;
			}

			if (frm.doc.bank === message.bank) {
				frappe.show_alert({
					message: __("Bank already matches {0}", [message.bank_name]),
					indicator: "blue",
				});
				return;
			}

			await frm.set_value("bank", message.bank);
			frappe.show_alert({
				message: __("Bank set to {0}. Save the document to persist the change.", [message.bank_name]),
				indicator: "green",
			});
		});
	},
});
