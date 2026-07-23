from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frappe
from frappe import _

from fab_banks_import.bank_directory import resolve_bank_name_from_abi


@dataclass(slots=True)
class CbiAccountDay:
	abi: str
	cab: str
	account_number: str
	currency: str
	opening_date: date | None
	opening_balance: float
	closing_balance: float
	transactions: list[dict[str, Any]] = field(default_factory=list)

	def movement_total(self) -> float:
		return round(sum(t["amount"] for t in self.transactions), 2)

	def is_balanced(self) -> bool:
		return abs(round(self.opening_balance + self.movement_total() - self.closing_balance, 2)) <= 0.005


def parse_cbi_statement_file(file_path: str) -> list[CbiAccountDay]:
	"""Parse one CBI positional rendicontazione flusso (records 61/62/63/64).

	Offsets follow the CBI fixed width layout as shipped by Smart Business
	Sella; every account day is later checked against the opening plus
	movements equals closing invariant, so a layout drift fails loudly
	instead of importing garbage.
	"""
	days: list[CbiAccountDay] = []
	current: CbiAccountDay | None = None
	content = Path(file_path).read_bytes().decode("latin-1")
	for line_no, raw in enumerate(content.splitlines(), 1):
		record_type = raw[1:3]
		if record_type in ("61", "62", "64") and len(raw) < 99:
			raise ValueError(f"{file_path}:{line_no}: record {record_type} shorter than the CBI layout")
		if record_type == "61":
			current = CbiAccountDay(
				abi=raw[52:57],
				cab=raw[57:62],
				account_number=raw[62:74].strip(),
				currency=raw[74:77],
				opening_date=parse_cbi_date(raw[77:83]),
				opening_balance=parse_cbi_amount(raw[83:99]),
				closing_balance=0.0,
			)
			days.append(current)
		elif record_type == "62" and current is not None:
			sign = -1.0 if raw[25:26] == "D" else 1.0
			current.transactions.append(
				{
					"sequence": raw[10:13],
					"posting_date": parse_cbi_date(raw[13:19]),
					"value_date": parse_cbi_date(raw[19:25]),
					"amount": sign * parse_cbi_number(raw[26:41]),
					"causale_abi": raw[41:43],
					"internal_code": raw[43:59].strip(),
					"bank_reference": raw[59:75].strip(),
					"description": raw[86:].strip(),
					"description_full": len(raw[86:].strip()) >= 34,
					"extra_descriptions": [],
				}
			)
		elif record_type == "63" and current is not None and current.transactions:
			text = raw[13:].strip()
			if text:
				last = current.transactions[-1]
				if last["description_full"] and not last["extra_descriptions"]:
					# a full width 62 description continues character by
					# character into the first 63 record
					last["description"] += text
					last["description_full"] = len(text) >= 100
				else:
					last["extra_descriptions"].append(text)
		elif record_type == "64" and current is not None:
			current.closing_balance = parse_cbi_amount(raw[19:35])
	return days


def parse_cbi_amount(chunk: str) -> float:
	chunk = chunk.strip()
	if not chunk or chunk[0] not in ("C", "D"):
		raise ValueError(f"malformed CBI balance field: {chunk!r}")
	sign = -1.0 if chunk[0] == "D" else 1.0
	return sign * parse_cbi_number(chunk[1:])


def parse_cbi_number(chunk: str) -> float:
	return float(chunk.strip().replace(".", "").replace(",", "."))


def parse_cbi_date(chunk: str) -> date | None:
	chunk = chunk.strip()
	if len(chunk) != 6:
		return None
	try:
		return datetime.strptime(chunk, "%d%m%y").date()
	except ValueError:
		return None


def import_cbi_statement_dir(
	source_dir: str,
	company: str,
	gl_account_map: dict[str, str] | None = None,
) -> dict[str, Any]:
	"""Import every CBI flusso in a directory into Bank Transactions.

	Idempotent: each movement carries a transaction id derived from account,
	date, sequence, bank reference, causale and amount. Account days that
	fail the balance
	invariant are reported and skipped entirely. gl_account_map ties each
	CBI account number to its ledger account, required the first time a
	company bank account is created.
	"""
	results: dict[str, Any] = {
		"files": 0,
		"account_days": 0,
		"transactions_created": 0,
		"transactions_skipped": 0,
		"unbalanced": [],
		"accounts": {},
	}
	for path in sorted(Path(source_dir).glob("*.txt")):
		results["files"] += 1
		for day in parse_cbi_statement_file(str(path)):
			if not day.transactions and not round(day.opening_balance - day.closing_balance, 2):
				continue
			results["account_days"] += 1
			if not day.is_balanced():
				results["unbalanced"].append(
					f"{path.name} {day.account_number} {day.opening_date}:"
					f" {day.opening_balance} + {day.movement_total()} != {day.closing_balance}"
				)
				continue
			bank_account = ensure_bank_account(day, company, gl_account_map or {})
			results["accounts"][day.account_number] = bank_account
			for txn in day.transactions:
				if not txn["amount"]:
					# informational zero movements would land as already
					# reconciled: not worth a ledger row
					results["transactions_skipped"] += 1
					continue
				outcome = upsert_bank_transaction(day, txn, bank_account, company)
				results[f"transactions_{outcome}"] += 1
	return results


def ensure_bank_account(day: CbiAccountDay, company: str, gl_account_map: dict[str, str]) -> str:
	existing = frappe.db.get_value("Bank Account", {"bank_account_no": day.account_number, "company": company})
	if existing:
		return existing
	bank_name = resolve_bank_name_from_abi(day.abi)
	if not bank_name:
		frappe.throw(_("No Bank record for ABI {0}: import the ABI/CAB directory first.").format(day.abi))
	gl_account = gl_account_map.get(day.account_number)
	if not gl_account:
		frappe.throw(
			_("No ledger account mapped for bank account {0}: pass it in gl_account_map.").format(
				day.account_number
			)
		)
	doc = frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": f"{bank_name} {day.account_number}",
			"bank": bank_name,
			"bank_account_no": day.account_number,
			"branch_code": day.cab,
			"is_company_account": 1,
			"company": company,
			"account": gl_account,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def upsert_bank_transaction(day: CbiAccountDay, txn: dict[str, Any], bank_account: str, company: str) -> str:
	# the sequence restarts per flusso and a fee can share its parent
	# movement's bank reference: amount and causale keep the key unique
	# across flussi that split the same day
	transaction_id = "-".join(
		(
			day.account_number,
			txn["posting_date"].isoformat() if txn["posting_date"] else "?",
			txn["sequence"],
			txn["bank_reference"] or "?",
			txn["causale_abi"],
			f"{txn['amount']:.2f}",
		)
	)
	if frappe.db.exists("Bank Transaction", {"transaction_id": transaction_id, "docstatus": ["<", 2]}):
		return "skipped"
	description = " | ".join([txn["description"], *txn["extra_descriptions"]]).strip(" |")
	doc = frappe.get_doc(
		{
			"doctype": "Bank Transaction",
			"date": txn["posting_date"],
			"bank_account": bank_account,
			"company": company,
			"currency": day.currency or "EUR",
			"deposit": txn["amount"] if txn["amount"] > 0 else 0,
			"withdrawal": -txn["amount"] if txn["amount"] < 0 else 0,
			"description": description[:500],
			"reference_number": txn["bank_reference"],
			"transaction_id": transaction_id,
			"transaction_type": txn["causale_abi"],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	return "created"
