from __future__ import annotations

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def after_install():
	ensure_custom_fields()


def after_migrate():
	ensure_custom_fields()


def ensure_custom_fields():
	create_custom_fields(get_custom_fields(), update=True)


def get_custom_fields() -> dict[str, list[dict[str, object]]]:
	return {
		"Bank": [
			{
				"fieldname": "fab_abi_code",
				"label": _("ABI Code"),
				"fieldtype": "Data",
				"insert_after": "bank_name",
				"length": 5,
				"in_standard_filter": 1,
				"unique": 1,
			}
		],
		"Bank Transaction": [
			{
				"fieldname": "fab_stripe_reference",
				"label": _("Reference"),
				"fieldtype": "Data",
				"insert_after": "transaction_type",
				"read_only": 1,
				"depends_on": "fab_stripe_reference",
			},
			{
				"fieldname": "fab_stripe_category",
				"label": _("Category"),
				"fieldtype": "Data",
				"insert_after": "fab_stripe_reference",
				"read_only": 1,
				"depends_on": "fab_stripe_category",
			},
			{
				"fieldname": "fab_stripe_kind",
				"label": _("Stripe Movement Kind"),
				"fieldtype": "Data",
				"insert_after": "fab_stripe_category",
				"read_only": 1,
				"hidden": 1,
			},
			{
				"fieldname": "fab_stripe_gross",
				"label": _("Stripe Gross Amount"),
				"fieldtype": "Currency",
				"options": "currency",
				"insert_after": "fab_stripe_kind",
				"read_only": 1,
				"depends_on": "fab_stripe_gross",
			},
			{
				"fieldname": "fab_stripe_fee",
				"label": _("Stripe Fee"),
				"fieldtype": "Currency",
				"options": "currency",
				"insert_after": "fab_stripe_gross",
				"read_only": 1,
				"depends_on": "fab_stripe_fee",
			},
			{
				"fieldname": "fab_stripe_account",
				"label": _("Stripe Booking Account"),
				"fieldtype": "Link",
				"options": "Account",
				"insert_after": "fab_stripe_fee",
				"read_only": 1,
				"depends_on": "fab_stripe_account",
			},
			{
				"fieldname": "fab_stripe_cost_center",
				"label": _("Stripe Cost Center"),
				"fieldtype": "Link",
				"options": "Cost Center",
				"insert_after": "fab_stripe_account",
				"read_only": 1,
				"depends_on": "fab_stripe_cost_center",
			},
		],
	}
