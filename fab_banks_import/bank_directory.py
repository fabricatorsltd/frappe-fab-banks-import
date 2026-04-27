from __future__ import annotations

from pathlib import Path
from typing import Any

import frappe
from frappe import _
from frappe.utils import is_valid_iban
from frappe.utils.file_manager import get_file_path


def import_abi_cab_file(file_path: str) -> dict[str, int]:
	return import_abi_cab_content(Path(file_path).read_text(errors="ignore"))


def import_abi_cab_content(content: str) -> dict[str, int]:
	rows = parse_abi_cab_file(content)
	created = 0
	updated = 0

	for row in rows.values():
		result = upsert_bank_by_abi_code(row["abi_code"], row["bank_name"])
		if result == "created":
			created += 1
		elif result == "updated":
			updated += 1

	return {
		"imported": len(rows),
		"created": created,
		"updated": updated,
	}


@frappe.whitelist()
def import_abi_cab_upload() -> dict[str, int | str | None]:
	content = get_uploaded_abi_cab_content()
	file_name = frappe.local.uploaded_filename or frappe.local.uploaded_file_url
	result = import_abi_cab_content(content)
	result["file_name"] = file_name
	return result


def get_uploaded_abi_cab_content() -> str:
	if frappe.local.uploaded_file:
		return frappe.local.uploaded_file.decode("utf-8", errors="ignore")

	file_url = frappe.local.uploaded_file_url
	if file_url:
		file_path = get_file_path(file_url)
		return Path(file_path).read_text(errors="ignore")

	frappe.throw(_("Upload an ABI/CAB text file first."))


def parse_abi_cab_file(content: str) -> dict[str, dict[str, str]]:
	rows: dict[str, dict[str, str]] = {}
	for raw_line in content.splitlines():
		line = raw_line.rstrip("\r\n")
		if len(line) < 20 or line[:1] != "1":
			continue

		abi_code = normalize_abi_code(line[2:7])
		bank_name = collapse_whitespace(line[13:83])
		if not abi_code or not bank_name:
			continue

		rows[abi_code] = {
			"abi_code": abi_code,
			"bank_name": bank_name,
		}

	return rows


def upsert_bank_by_abi_code(abi_code: str, bank_name: str) -> str:
	existing_by_abi = frappe.db.get_value("Bank", {"fab_abi_code": abi_code}, "name")
	if existing_by_abi:
		bank = frappe.get_doc("Bank", existing_by_abi)
		changed = False
		if bank.bank_name != bank_name:
			bank.bank_name = bank_name
			changed = True
		if bank.fab_abi_code != abi_code:
			bank.fab_abi_code = abi_code
			changed = True
		if changed:
			bank.save(ignore_permissions=True)
			return "updated"
		return "unchanged"

	existing_by_name = frappe.db.get_value("Bank", {"bank_name": bank_name}, "name")
	if existing_by_name:
		bank = frappe.get_doc("Bank", existing_by_name)
		if bank.fab_abi_code != abi_code:
			bank.fab_abi_code = abi_code
			bank.save(ignore_permissions=True)
			return "updated"
		return "unchanged"

	frappe.get_doc(
		{
			"doctype": "Bank",
			"bank_name": bank_name,
			"fab_abi_code": abi_code,
		}
	).insert(ignore_permissions=True)
	return "created"


def get_bank_record_from_abi(abi_code: str | None) -> dict[str, str] | None:
	abi_code = normalize_abi_code(abi_code)
	if not abi_code:
		return None

	bank = frappe.db.get_value(
		"Bank",
		{"fab_abi_code": abi_code},
		["name", "bank_name", "fab_abi_code"],
		as_dict=True,
	)
	return dict(bank) if bank else None


def resolve_bank_name_from_abi(abi_code: str | None) -> str | None:
	bank = get_bank_record_from_abi(abi_code)
	return bank["bank_name"] if bank else None


@frappe.whitelist()
def resolve_bank_from_iban(iban: Any) -> dict[str, str | None]:
	details = extract_italian_bank_codes_from_iban(iban)
	normalized_iban = details["iban"]
	if not normalized_iban:
		frappe.throw(_("Enter an IBAN first."))

	if not details["abi_code"]:
		if normalized_iban.startswith("IT"):
			frappe.throw(_("Could not extract an ABI code from IBAN {0}.").format(normalized_iban))
		frappe.throw(_("Only Italian IBANs with ABI/CAB codes are supported."))

	bank = get_bank_record_from_abi(details["abi_code"])
	if not bank:
		frappe.throw(_("No imported Bank matches ABI code {0}.").format(details["abi_code"]))

	return {
		"iban": normalized_iban,
		"abi_code": details["abi_code"],
		"cab_code": details["cab_code"],
		"bank": bank["name"],
		"bank_name": bank["bank_name"],
	}


def extract_italian_bank_codes_from_iban(iban: Any) -> dict[str, str | None]:
	normalized_iban = normalize_text(iban)
	if not normalized_iban:
		return {"iban": None, "abi_code": None, "cab_code": None}

	normalized_iban = normalized_iban.replace(" ", "").upper()
	if not is_valid_iban(normalized_iban):
		return {"iban": normalized_iban, "abi_code": None, "cab_code": None}

	details = {"iban": normalized_iban, "abi_code": None, "cab_code": None}
	if normalized_iban.startswith("IT") and len(normalized_iban) == 27:
		details["abi_code"] = normalized_iban[5:10]
		details["cab_code"] = normalized_iban[10:15]

	return details


def normalize_abi_code(value: Any) -> str | None:
	text = normalize_text(value)
	if not text:
		return None
	return text.zfill(5)


def normalize_text(value: Any) -> str | None:
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def collapse_whitespace(value: str) -> str:
	return " ".join((value or "").split()).strip()
