frappe.listview_settings["Bank"] = {
	onload(listview) {
		listview.page.add_menu_item(__("Import ABI/CAB Directory"), () => {
			new frappe.ui.FileUploader({
				method: "fab_banks_import.bank_directory.import_abi_cab_upload",
				allow_toggle_private: false,
				allow_take_photo: false,
				restrictions: {
					allowed_file_types: [".txt"],
				},
				on_success(_file_doc, r) {
					const result = r.message || {};
					listview.refresh();
					frappe.msgprint(
						__(
							"ABI/CAB import completed for {0}. Imported: {1}, created: {2}, updated: {3}.",
							[
								result.file_name || __("uploaded file"),
								result.imported || 0,
								result.created || 0,
								result.updated || 0,
							]
						)
					);
				},
			});
		});
	},
};
