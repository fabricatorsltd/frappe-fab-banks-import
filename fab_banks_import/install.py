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
		]
	}
