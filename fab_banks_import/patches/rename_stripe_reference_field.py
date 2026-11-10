import frappe

from fab_banks_import.install import ensure_custom_fields

# the reference field of the first Stripe import release, named after the
# only metadata key it knew
OLD_FIELD = "fab_liberapay_transfer_id"


def execute():
	ensure_custom_fields()
	if frappe.db.has_column("Bank Transaction", OLD_FIELD):
		frappe.db.sql(
			f"""update `tabBank Transaction` set fab_stripe_reference = `{OLD_FIELD}`
			where coalesce(`{OLD_FIELD}`, '') != '' and coalesce(fab_stripe_reference, '') = ''"""
		)
	custom_field = f"Bank Transaction-{OLD_FIELD}"
	if frappe.db.exists("Custom Field", custom_field):
		frappe.delete_doc("Custom Field", custom_field, ignore_permissions=True)
	if frappe.db.has_column("Bank Transaction", OLD_FIELD):
		frappe.db.sql_ddl(f"alter table `tabBank Transaction` drop column `{OLD_FIELD}`")
	frappe.clear_cache(doctype="Bank Transaction")
