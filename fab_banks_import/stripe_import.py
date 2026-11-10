from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import flt, fmt_money, formatdate, getdate, now_datetime, nowdate
from frappe.utils.data import get_system_timezone

SETTINGS_DOCTYPE = "Stripe Import Settings"
STRIPE_API = "https://api.stripe.com/v1/"
STRIPE_TIMEOUT = 30
PAYOUT_MATCH_DAYS = 5
LEDGER_LANGUAGE = "it"

# Liberapay takes its commission by reversing part of the donation, so
# refunds and adjustments are an expense, never a lost donation
CATEGORY_BY_TYPE = {
	"payment": "donation",
	"charge": "donation",
	"payment_refund": "commission",
	"refund": "commission",
	"adjustment": "commission",
	"stripe_fee": "stripe_fee",
	"payout": "payout",
}


@dataclass(slots=True)
class StripeMovement:
	id: str
	type: str
	category: str
	posting_date: date
	amount: float
	fee: float
	net: float
	source_id: str = ""
	charge_id: str = ""
	liberapay_transfer_id: str = ""
	payout_arrival_date: date | None = None
	payout_status: str = ""


@dataclass(slots=True)
class MonthTotals:
	donations: float = 0.0
	donation_fees: float = 0.0
	commissions: float = 0.0
	stripe_fees: float = 0.0
	deposits: float = 0.0
	withdrawals: float = 0.0
	count: int = 0

	@property
	def charges(self) -> float:
		return round(self.donation_fees + self.commissions + self.stripe_fees, 2)

	@property
	def net(self) -> float:
		return round(self.deposits - self.withdrawals, 2)

	def is_balanced(self) -> bool:
		return abs(round(self.donations - self.charges - self.net, 2)) <= 0.005


def cents(value: int | None) -> float:
	return round((value or 0) / 100, 2)


def parse_balance_transaction(raw: dict[str, Any], tz: ZoneInfo) -> StripeMovement:
	"""Turn one /v1/balance_transactions row (expanded source) into a movement.

	Unknown types and non EUR rows raise: a new kind of money flow must be
	mapped by hand before it reaches the ledger.
	"""
	if (raw.get("currency") or "").lower() != "eur":
		raise ValueError(f"{raw.get('id')}: unsupported currency {raw.get('currency')!r}")
	category = CATEGORY_BY_TYPE.get(raw.get("type"))
	if not category:
		raise ValueError(f"{raw.get('id')}: unmapped balance transaction type {raw.get('type')!r}")
	amount, fee, net = cents(raw.get("amount")), cents(raw.get("fee")), cents(raw.get("net"))
	if abs(round(amount - fee - net, 2)) > 0.005:
		raise ValueError(f"{raw['id']}: amount {amount} - fee {fee} != net {net}")
	source = raw.get("source")
	movement = StripeMovement(
		id=raw["id"],
		type=raw["type"],
		category=category,
		posting_date=datetime.fromtimestamp(raw["created"], tz).date(),
		amount=amount,
		fee=fee,
		net=net,
	)
	if isinstance(source, dict):
		movement.source_id = source.get("id") or ""
		charge = source.get("charge")
		movement.charge_id = (charge.get("id") if isinstance(charge, dict) else charge) or ""
		movement.liberapay_transfer_id = str(
			(source.get("metadata") or {}).get("liberapay_transfer_id") or ""
		)
		if category == "payout":
			movement.payout_status = source.get("status") or ""
		if category == "payout" and source.get("arrival_date"):
			# arrival_date is midnight UTC of the bank value date
			movement.payout_arrival_date = datetime.fromtimestamp(source["arrival_date"], UTC).date()
	elif isinstance(source, str):
		movement.source_id = source
	if category == "payout" and not movement.payout_arrival_date:
		raise ValueError(f"{raw['id']}: payout without arrival date, expand the source")
	return movement


def link_reversals(movements: list[StripeMovement]) -> None:
	"""Copy the Liberapay transfer id of a donation onto its reversals."""
	by_charge = {m.source_id: m.liberapay_transfer_id for m in movements if m.category == "donation"}
	for m in movements:
		if m.category == "commission" and not m.liberapay_transfer_id and m.charge_id:
			m.liberapay_transfer_id = by_charge.get(m.charge_id, "")


def month_totals(movements: list[StripeMovement]) -> MonthTotals:
	"""Aggregate the non payout movements of one month for its Journal Entry."""
	totals = MonthTotals()
	for m in movements:
		if m.category == "payout":
			continue
		totals.count += 1
		if m.category == "donation":
			totals.donations += m.amount
			totals.donation_fees += m.fee
		elif m.category == "commission":
			totals.commissions -= m.net
		elif m.category == "stripe_fee":
			totals.stripe_fees -= m.net
		if m.net > 0:
			totals.deposits += m.net
		else:
			totals.withdrawals -= m.net
	for attr in ("donations", "donation_fees", "commissions", "stripe_fees", "deposits", "withdrawals"):
		setattr(totals, attr, round(getattr(totals, attr), 2))
	if not totals.is_balanced():
		raise ValueError(
			f"month totals do not balance: donations {totals.donations} - charges {totals.charges}"
			f" != net {totals.net}"
		)
	return totals


def compute_opening_balance(current_balance: float, movements: list[StripeMovement]) -> float:
	"""Balance at start_date - 1, walked back from today's Stripe balance."""
	return round(current_balance - sum(m.net for m in movements), 2)


def check_balance_invariant(opening: float, movements: list[StripeMovement], current_balance: float) -> None:
	computed = round(opening + sum(m.net for m in movements), 2)
	if abs(round(computed - current_balance, 2)) > 0.005:
		raise ValueError(
			f"Stripe balance mismatch: opening {opening} + movements {round(computed - opening, 2)}"
			f" = {computed} != Stripe balance {current_balance}"
		)


def pick_payout_match(
	candidates: list[dict[str, Any]], amount: float, arrival_date: date, days: int = PAYOUT_MATCH_DAYS
) -> dict[str, Any] | None:
	"""Pick the bank deposit that received a payout.

	Same amount, description mentioning Stripe, unreconciled and dated within
	the window around arrival_date; the closest date wins and a tie returns
	nothing so a human decides.
	"""
	matches = []
	for row in candidates:
		if abs(round(flt(row.get("deposit")) - amount, 2)) > 0.005:
			continue
		if "stripe" not in (row.get("description") or "").lower():
			continue
		if flt(row.get("unallocated_amount")) <= 0 or row.get("status") == "Reconciled":
			continue
		distance = abs((getdate(row["date"]) - arrival_date).days)
		if distance <= days:
			matches.append((distance, row))
	if not matches:
		return None
	matches.sort(key=lambda item: item[0])
	if len(matches) > 1 and matches[0][0] == matches[1][0]:
		return None
	return matches[0][1]


def statement_rows(
	opening: float, movements: list[StripeMovement]
) -> tuple[list[dict[str, Any]], dict[str, float]]:
	"""Running balance rows and totals for the accountant statement."""
	rows = []
	balance = opening
	totals = {"donations": 0.0, "charges": 0.0, "payouts": 0.0, "net": 0.0}
	for m in sorted(movements, key=lambda m: (m.posting_date, m.id)):
		balance = round(balance + m.net, 2)
		rows.append({"movement": m, "balance": balance})
		if m.category == "donation":
			totals["donations"] += m.amount
			totals["charges"] += m.fee
		elif m.category == "payout":
			totals["payouts"] -= m.net
		else:
			totals["charges"] -= m.net
		totals["net"] += m.net
	totals = {k: round(v, 2) for k, v in totals.items()}
	totals["opening"] = round(opening, 2)
	totals["closing"] = balance
	return rows, totals


def month_bounds(year: int, month: int) -> tuple[date, date]:
	return date(year, month, 1), date(year, month, monthrange(year, month)[1])


# --------------------------------------------------------------------------
# Stripe API


def stripe_get(api_key: str, path: str, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
	url = STRIPE_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
	token = base64.b64encode(f"{api_key}:".encode()).decode()
	request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
	try:
		with urllib.request.urlopen(request, timeout=STRIPE_TIMEOUT) as response:
			return json.load(response)
	except urllib.error.HTTPError as e:
		try:
			message = json.load(e).get("error", {}).get("message", "")
		except Exception:
			message = ""
		frappe.throw(_("Stripe API {0} failed with HTTP {1}: {2}").format(path, e.code, message))


def fetch_balance(api_key: str) -> float:
	balance = stripe_get(api_key, "balance")
	total = 0.0
	for row in [*balance.get("available", []), *balance.get("pending", [])]:
		if row["currency"].lower() != "eur":
			if row["amount"]:
				frappe.throw(_("Stripe holds a {0} balance: only EUR is supported").format(row["currency"]))
			continue
		total += cents(row["amount"])
	return round(total, 2)


def fetch_balance_transactions(api_key: str, created_from: int) -> list[dict[str, Any]]:
	rows: list[dict[str, Any]] = []
	after = ""
	while True:
		params = [("limit", "100"), ("created[gte]", str(created_from)), ("expand[]", "data.source")]
		if after:
			params.append(("starting_after", after))
		page = stripe_get(api_key, "balance_transactions", params)
		rows += page["data"]
		if not page.get("has_more"):
			return rows
		after = page["data"][-1]["id"]


def fetch_movements(api_key: str, start_date: date, tz: ZoneInfo) -> tuple[list[StripeMovement], float]:
	"""Movements since start_date and the balance they lead to.

	The balance is read around the list; if it moved while paging, read again
	so the invariant is never checked against a half updated pair.
	"""
	created_from = int(datetime.combine(start_date, datetime.min.time(), tz).timestamp())
	for _attempt in range(3):
		before = fetch_balance(api_key)
		raw = fetch_balance_transactions(api_key, created_from)
		after = fetch_balance(api_key)
		if before == after:
			break
	else:
		frappe.throw(_("Stripe balance kept changing during the sync, retry later"))
	movements = sorted((parse_balance_transaction(r, tz) for r in raw), key=lambda m: (m.posting_date, m.id))
	link_reversals(movements)
	return movements, after


# --------------------------------------------------------------------------
# ERPNext side


def get_settings():
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	missing = [
		field
		for field in (
			"company",
			"bank_account",
			"payout_bank_account",
			"income_account",
			"fee_account",
			"clearing_account",
			"start_date",
		)
		if not settings.get(field)
	]
	if missing:
		frappe.throw(_("Stripe Import Settings incomplete: {0}").format(", ".join(missing)))
	return settings


def bank_gl_account(bank_account: str) -> str:
	return frappe.db.get_value("Bank Account", bank_account, "account")


def sync(settings=None) -> dict[str, Any]:
	"""Import Stripe movements, post opening, closed months and payouts.

	Every step is idempotent: running it twice creates nothing new.
	"""
	from frappe.utils.synchronization import filelock

	# the daily job and "Sync now" must not both insert the same rows
	with filelock("stripe_import_sync", timeout=120):
		return _sync(settings or get_settings())


def _sync(settings) -> dict[str, Any]:
	api_key = settings.get_password("api_key")
	tz = ZoneInfo(get_system_timezone())
	start_date = getdate(settings.start_date)
	movements, balance = fetch_movements(api_key, start_date, tz)
	if any(m.posting_date < start_date for m in movements):
		frappe.throw(_("Stripe returned movements before the start date"))
	results: dict[str, Any] = {
		"stripe_balance": balance,
		"movements": len(movements),
		"by_type": dict(Counter(m.type for m in movements)),
		"transactions_created": 0,
		"transactions_skipped": 0,
	}
	results["opening"] = ensure_opening_entry(settings, movements, balance)
	for m in movements:
		results[f"transactions_{upsert_bank_transaction(settings, m)}"] += 1
	check_ledger_side(settings, start_date, balance)
	results["months"] = post_closed_months(settings)
	results["payouts"] = [ensure_payout(settings, m) for m in movements if m.category == "payout"]
	settings.db_set("last_sync", now_datetime())
	settings.db_set("last_sync_log", json.dumps(results, indent=1, default=str))
	return results


def check_ledger_side(settings, start_date: date, balance: float) -> None:
	"""The stored Bank Transactions, not only the API rows, must add up to Stripe."""
	imported = frappe.db.sql(
		"""select coalesce(sum(deposit - withdrawal), 0) from `tabBank Transaction`
		where bank_account = %s and docstatus = 1 and date >= %s""",
		(settings.bank_account, start_date),
	)[0][0]
	computed = round(flt(settings.opening_balance) + flt(imported), 2)
	if abs(round(computed - balance, 2)) > 0.005:
		frappe.throw(
			_("Stripe Bank Transactions add up to {0} but Stripe holds {1}").format(computed, balance)
		)


def ensure_opening_entry(settings, movements: list[StripeMovement], balance: float) -> dict[str, Any]:
	opening = compute_opening_balance(balance, movements)
	check_balance_invariant(opening, movements, balance)
	posted = settings.opening_journal_entry
	if posted and frappe.db.get_value("Journal Entry", posted, "docstatus") == 1:
		if abs(round(flt(settings.opening_balance) - opening, 2)) > 0.005:
			frappe.throw(
				_("Stripe opening balance drifted: posted {0}, computed now {1}").format(
					settings.opening_balance, opening
				)
			)
		return {"balance": opening, "journal_entry": posted, "created": False}
	posting_date = getdate(settings.start_date) - timedelta(days=1)
	journal_entry = None
	if opening:
		stripe_account = bank_gl_account(settings.bank_account)
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Journal Entry",
				"company": settings.company,
				"posting_date": posting_date,
				"user_remark": ledger_text("Stripe opening balance on {0}", formatdate(posting_date)),
				"accounts": [
					journal_line(stripe_account, opening, settings.company),
					journal_line(settings.clearing_account, -opening, settings.company),
				],
			}
		)
		je.flags.ignore_permissions = True
		je.insert()
		je.submit()
		journal_entry = je.name
	settings.db_set("opening_balance", opening)
	settings.db_set("opening_journal_entry", journal_entry)
	return {"balance": opening, "journal_entry": journal_entry, "created": bool(journal_entry)}


def journal_line(account: str, signed_amount: float, company: str) -> dict[str, Any]:
	"""A positive amount debits the account, a negative one credits it."""
	return {
		"account": account,
		"debit_in_account_currency": signed_amount if signed_amount > 0 else 0,
		"credit_in_account_currency": -signed_amount if signed_amount < 0 else 0,
		"cost_center": frappe.get_cached_value("Company", company, "cost_center"),
	}


def bank_transaction_description(m: StripeMovement) -> str:
	parts = [f"Stripe {m.type}"]
	if m.liberapay_transfer_id:
		parts.append(f"Liberapay transfer {m.liberapay_transfer_id}")
	if m.category == "payout" and m.payout_arrival_date:
		parts.append(f"payout {m.source_id} arriving {m.payout_arrival_date.isoformat()}")
	parts.append(f"gross {m.amount:.2f} fee {m.fee:.2f} net {m.net:.2f}")
	return " | ".join(parts)


def upsert_bank_transaction(settings, m: StripeMovement) -> str:
	if frappe.db.exists("Bank Transaction", {"transaction_id": m.id, "docstatus": ["<", 2]}):
		return "skipped"
	if not m.net:
		return "skipped"
	doc = frappe.get_doc(
		{
			"doctype": "Bank Transaction",
			"date": m.posting_date,
			"bank_account": settings.bank_account,
			"company": settings.company,
			"currency": "EUR",
			"deposit": m.net if m.net > 0 else 0,
			"withdrawal": -m.net if m.net < 0 else 0,
			"description": bank_transaction_description(m),
			"reference_number": m.source_id,
			"transaction_id": m.id,
			"transaction_type": m.type,
			"fab_stripe_gross": m.amount,
			"fab_stripe_fee": m.fee,
			"fab_liberapay_transfer_id": m.liberapay_transfer_id,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	return "created"


def bank_transaction_movements(bank_account: str, from_date: date, to_date: date) -> list[StripeMovement]:
	"""Rebuild movements from the imported Bank Transactions (no API call)."""
	rows = frappe.get_all(
		"Bank Transaction",
		filters={"bank_account": bank_account, "docstatus": 1, "date": ["between", [from_date, to_date]]},
		fields=[
			"name",
			"date",
			"deposit",
			"withdrawal",
			"transaction_id",
			"transaction_type",
			"reference_number",
			"fab_stripe_gross",
			"fab_stripe_fee",
			"fab_liberapay_transfer_id",
		],
		order_by="date asc, transaction_id asc",
	)
	movements = []
	for row in rows:
		category = CATEGORY_BY_TYPE.get(row.transaction_type)
		if not category:
			frappe.throw(
				_("Bank Transaction {0} has unmapped Stripe type {1}").format(row.name, row.transaction_type)
			)
		movements.append(
			StripeMovement(
				id=row.transaction_id,
				type=row.transaction_type,
				category=category,
				posting_date=getdate(row.date),
				amount=flt(row.fab_stripe_gross),
				fee=flt(row.fab_stripe_fee),
				net=round(flt(row.deposit) - flt(row.withdrawal), 2),
				source_id=row.reference_number or "",
				liberapay_transfer_id=row.fab_liberapay_transfer_id or "",
			)
		)
	return movements


def post_closed_months(settings) -> list[dict[str, Any]]:
	today = getdate(nowdate())
	current_month = date(today.year, today.month, 1)
	month = date(getdate(settings.start_date).year, getdate(settings.start_date).month, 1)
	results = []
	while month < current_month:
		outcome = ensure_month_entry(settings, month.year, month.month)
		if outcome:
			results.append(outcome)
		month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
	return results


def month_reference(year: int, month: int) -> str:
	return f"STRIPE-{year}-{month:02d}"


def ensure_month_entry(settings, year: int, month: int) -> dict[str, Any] | None:
	"""One Journal Entry per closed month, reconciled against its Bank Transactions."""
	first, last = month_bounds(year, month)
	movements = [
		m for m in bank_transaction_movements(settings.bank_account, first, last) if m.category != "payout"
	]
	if not movements:
		return None
	totals = month_totals(movements)
	stripe_account = bank_gl_account(settings.bank_account)
	reference = month_reference(year, month)
	je_name = frappe.db.get_value(
		"Journal Entry", {"cheque_no": reference, "company": settings.company, "docstatus": 1}
	)
	created = False
	if je_name:
		posted = frappe.db.sql(
			"""select sum(debit_in_account_currency), sum(credit_in_account_currency)
			from `tabJournal Entry Account` where parent = %s and account = %s""",
			(je_name, stripe_account),
		)[0]
		if (
			abs(round(flt(posted[0]) - totals.deposits, 2)) > 0.005
			or abs(round(flt(posted[1]) - totals.withdrawals, 2)) > 0.005
		):
			frappe.throw(
				_("Journal Entry {0} no longer matches the Stripe movements of {1}/{2}").format(
					je_name, f"{month:02d}", year
				)
			)
	else:
		# deposits and withdrawals stay on separate lines: bank reconciliation
		# allocates the sum of the absolute bank GL amounts across the rows
		lines = [
			journal_line(settings.income_account, -totals.donations, settings.company),
			journal_line(settings.fee_account, totals.charges, settings.company),
			journal_line(stripe_account, totals.deposits, settings.company),
			journal_line(stripe_account, -totals.withdrawals, settings.company),
		]
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Bank Entry",
				"company": settings.company,
				"posting_date": last,
				"cheque_no": reference,
				"cheque_date": last,
				"user_remark": ledger_text("Liberapay donations via Stripe {0}", f"{month:02d}/{year}"),
				"accounts": [
					line
					for line in lines
					if line["debit_in_account_currency"] or line["credit_in_account_currency"]
				],
			}
		)
		je.flags.ignore_permissions = True
		je.insert()
		je.submit()
		je_name, created = je.name, True
	reconciled = reconcile_month(settings.bank_account, first, last, je_name)
	return {
		"month": f"{year}-{month:02d}",
		"journal_entry": je_name,
		"created": created,
		"donations": totals.donations,
		"charges": totals.charges,
		"net": totals.net,
		"reconciled": reconciled,
	}


def reconcile_against(bank_transaction: str, doctype: str, name: str) -> bool:
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
		reconcile_vouchers,
	)

	bt = frappe.db.get_value(
		"Bank Transaction", bank_transaction, ["unallocated_amount", "deposit", "withdrawal"], as_dict=True
	)
	if flt(bt.unallocated_amount) <= 0:
		return False
	if frappe.db.exists(
		"Bank Transaction Payments",
		{"parent": bank_transaction, "payment_document": doctype, "payment_entry": name},
	):
		return False
	amount = flt(bt.deposit) or flt(bt.withdrawal)
	reconcile_vouchers(
		bank_transaction,
		json.dumps([{"payment_doctype": doctype, "payment_name": name, "amount": amount}]),
	)
	return True


def reconcile_month(bank_account: str, first: date, last: date, je_name: str) -> int:
	names = frappe.get_all(
		"Bank Transaction",
		filters={
			"bank_account": bank_account,
			"docstatus": 1,
			"date": ["between", [first, last]],
			"transaction_type": ["!=", "payout"],
			"unallocated_amount": [">", 0],
		},
		pluck="name",
		order_by="date asc, transaction_id asc",
	)
	return sum(reconcile_against(name, "Journal Entry", je_name) for name in names)


def ensure_payout(settings, m: StripeMovement) -> dict[str, Any]:
	"""Internal transfer Stripe -> payout bank, reconciled on both sides."""
	amount = -m.net
	result: dict[str, Any] = {"payout": m.source_id, "amount": amount, "arrival_date": m.payout_arrival_date}
	if m.payout_status != "paid" or m.payout_arrival_date > getdate(nowdate()):
		# in transit or failed: the bank side does not exist yet
		result["pending"] = m.payout_status or "unknown"
		return result
	stripe_bt = frappe.db.get_value("Bank Transaction", {"transaction_id": m.id, "docstatus": 1})
	pe_name = frappe.db.get_value(
		"Payment Entry", {"reference_no": m.source_id, "company": settings.company, "docstatus": 1}
	)
	result["created"] = not pe_name
	if not pe_name:
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Internal Transfer",
				"company": settings.company,
				"posting_date": m.payout_arrival_date,
				"paid_from": bank_gl_account(settings.bank_account),
				"paid_to": bank_gl_account(settings.payout_bank_account),
				"paid_amount": amount,
				"received_amount": amount,
				"source_exchange_rate": 1,
				"target_exchange_rate": 1,
				"reference_no": m.source_id,
				"reference_date": m.payout_arrival_date,
				"custom_remarks": 1,
				"remarks": ledger_text("Stripe payout {0} to bank", m.source_id),
			}
		)
		pe.flags.ignore_permissions = True
		pe.insert()
		pe.submit()
		pe_name = pe.name
	result["payment_entry"] = pe_name
	result["stripe_reconciled"] = bool(stripe_bt) and reconcile_against(stripe_bt, "Payment Entry", pe_name)

	already = frappe.db.sql(
		"""select bt.name from `tabBank Transaction` bt
		join `tabBank Transaction Payments` p on p.parent = bt.name
		where bt.bank_account = %s and bt.docstatus = 1
		and p.payment_document = 'Payment Entry' and p.payment_entry = %s""",
		(settings.payout_bank_account, pe_name),
	)
	if already:
		result["bank_transaction"] = already[0][0]
		result["bank_matched"] = "already"
		return result
	candidates = frappe.get_all(
		"Bank Transaction",
		filters={
			"bank_account": settings.payout_bank_account,
			"docstatus": 1,
			"deposit": [">", 0],
			"date": [
				"between",
				[
					m.payout_arrival_date - timedelta(days=PAYOUT_MATCH_DAYS),
					m.payout_arrival_date + timedelta(days=PAYOUT_MATCH_DAYS),
				],
			],
		},
		fields=["name", "date", "deposit", "description", "unallocated_amount", "status"],
	)
	match = pick_payout_match(candidates, amount, m.payout_arrival_date)
	if not match:
		result["bank_matched"] = "no match"
		return result
	reconcile_against(match["name"], "Payment Entry", pe_name)
	result["bank_transaction"] = match["name"]
	result["bank_matched"] = "matched"
	return result


# --------------------------------------------------------------------------
# entry points


@frappe.whitelist(methods=["POST"])
def sync_now() -> dict[str, Any]:
	frappe.only_for(("System Manager", "Accounts Manager"))
	return sync()


def daily_sync() -> None:
	"""Sync, then email last month's statement when due; both only when enabled."""
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	if not settings.enabled:
		return
	try:
		sync()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error("Stripe sync failed", "fab_banks_import.stripe_import.daily_sync")
		raise
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	due = statement_due(settings, getdate(nowdate()))
	if not due:
		return
	try:
		send_statement(*due, settings=settings)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error("Stripe statement email failed", "fab_banks_import.stripe_import.daily_sync")
		raise


def statement_send_date(year: int, month: int) -> date:
	"""Day 3 of the month, or the following Monday when it falls on a weekend."""
	day = date(year, month, 3)
	if day.weekday() >= 5:
		day += timedelta(days=7 - day.weekday())
	return day


def previous_month(today: date) -> tuple[int, int]:
	return (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)


def period_key(year: int, month: int) -> str:
	return f"{year}-{month:02d}"


def statement_due(settings, today: date) -> tuple[int, int] | None:
	"""The period to email today, if any.

	From the send day onwards the previous month is due until it is marked as
	sent, so a scheduler that skipped the exact day still sends exactly once.
	"""
	if not settings.statement_email_enabled:
		return None
	if today < statement_send_date(today.year, today.month):
		return None
	year, month = previous_month(today)
	if (settings.last_statement_sent or "") >= period_key(year, month):
		return None
	if settings.start_date and month_bounds(year, month)[1] < getdate(settings.start_date):
		return None
	return year, month


def statement_filename(year: int, month: int | None = None) -> str:
	suffix = f"{year}-{month:02d}" if month else str(year)
	return f"{statement_text('stripe-statement')}-{suffix}.pdf"


def recipients_by_language(settings) -> dict[str, list[str]]:
	"""Group recipients by language: the User's language when the address is a
	user, else the row language, else the system default."""
	default = system_language()
	groups: dict[str, list[str]] = {}
	for row in settings.get("statement_recipients") or []:
		email = (row.email or "").strip()
		if not email:
			continue
		user_lang = frappe.db.get_value("User", {"email": email}, "language")
		lang = user_lang or row.get("language") or default
		if email not in groups.setdefault(lang, []):
			groups[lang].append(email)
	return groups


def statement_email_body(label: str, totals: dict[str, float]) -> str:
	rows = [
		(statement_text("Opening balance"), totals["opening"]),
		(statement_text("Donations (gross)"), totals["donations"]),
		(statement_text("Fees (Stripe and Liberapay)"), -totals["charges"]),
		(statement_text("Payouts to bank"), -totals["payouts"]),
	]
	lines = "".join(
		f"<tr><td>{text}</td><td align='right'>{format_eur(value)} EUR</td></tr>" for text, value in rows
	)
	closing = statement_text("Closing balance")
	return (
		f"<p>{statement_text('Attached is the Stripe statement for {0}.').format(label)}</p>"
		f"<table>{lines}<tr><td><b>{closing}</b></td>"
		f"<td align='right'><b>{format_eur(totals['closing'])} EUR</b></td></tr></table>"
	)


def send_statement(year: int, month: int, settings=None, force: bool = False) -> dict[str, Any]:
	"""Email the statement of one month, one mail per recipient language."""
	settings = settings or frappe.get_single(SETTINGS_DOCTYPE)
	period = period_key(year, month)
	if not force and (settings.last_statement_sent or "") >= period:
		return {
			"period": period,
			"sent": False,
			"reason": f"already sent up to {settings.last_statement_sent}",
		}
	groups = recipients_by_language(settings)
	if not groups:
		frappe.throw(_("No recipients configured for the Stripe statement"))
	label = f"{month:02d}/{year}"
	sent = {}
	for lang, recipients in groups.items():
		with use_language(lang):
			context = statement_context(year, month)
			pdf = render_statement_pdf(year, month, html=render_statement_html(year, month, context))
			frappe.sendmail(
				recipients=recipients,
				subject=statement_text("Stripe statement {0}").format(label),
				message=statement_email_body(label, context["totals"]),
				attachments=[{"fname": statement_filename(year, month), "fcontent": pdf}],
				reference_doctype=SETTINGS_DOCTYPE,
				reference_name=SETTINGS_DOCTYPE,
			)
		sent[lang] = recipients
	if period > (settings.last_statement_sent or ""):
		settings.db_set("last_statement_sent", period)
	return {
		"period": period,
		"sent": True,
		"recipients": [e for group in sent.values() for e in group],
		"languages": sent,
	}


@frappe.whitelist(methods=["POST"])
def send_last_month_statement(force: int | str | bool = 0) -> dict[str, Any]:
	frappe.only_for(("System Manager", "Accounts Manager"))
	sync()
	year, month = previous_month(getdate(nowdate()))
	return send_statement(year, month, force=bool(int(force or 0)))


TYPE_LABELS = {
	"donation": "Donation",
	"commission": "Liberapay commission",
	"stripe_fee": "Stripe fee",
	"payout": "Payout to bank",
}


@contextmanager
def use_language(lang: str | None):
	"""Render translated content in lang, then restore the request language."""
	previous = getattr(frappe.local, "lang", None)
	if lang:
		frappe.local.lang = lang
	try:
		yield
	finally:
		frappe.local.lang = previous


def system_language() -> str:
	return frappe.db.get_single_value("System Settings", "language") or "en"


def statement_text(message: str) -> str:
	return _(message, context="stripe_statement")


def ledger_text(message: str, *args: Any) -> str:
	"""Ledger remarks are for the Italian accountant, whoever runs the sync."""
	with use_language(LEDGER_LANGUAGE):
		return statement_text(message).format(*args)


def statement_context(year: int, month: int | None = None) -> dict[str, Any]:
	settings = get_settings()
	if month:
		first, last = month_bounds(year, month)
		period = f"{month:02d}/{year}"
	else:
		first, last = date(year, 1, 1), date(year, 12, 31)
		period = str(year)
	start_date = getdate(settings.start_date)
	if last < start_date:
		frappe.throw(_("The period ends before the Stripe start date {0}").format(start_date))
	first = max(first, start_date)
	before = bank_transaction_movements(settings.bank_account, start_date, first - timedelta(days=1))
	opening = round(flt(settings.opening_balance) + sum(m.net for m in before), 2)
	movements = bank_transaction_movements(settings.bank_account, first, last)
	rows, totals = statement_rows(opening, movements)
	today = getdate(nowdate())
	if last >= today:
		reference_label = statement_text("current Stripe balance (API /v1/balance)")
		reference = fetch_balance(settings.get_password("api_key"))
	else:
		reference_label = statement_text("computed running balance")
		reference = totals["closing"]
	stripe_account = bank_gl_account(settings.bank_account)
	ledger = frappe.db.sql(
		"""select coalesce(sum(debit_in_account_currency - credit_in_account_currency), 0)
		from `tabGL Entry` where account = %s and is_cancelled = 0 and posting_date <= %s""",
		(stripe_account, min(last, today)),
	)[0][0]
	return {
		"company": settings.company,
		"bank_account": settings.bank_account,
		"gl_account": stripe_account,
		"period": period,
		"first": first,
		"last": last,
		"rows": [
			{
				"date": r["movement"].posting_date.strftime("%d/%m/%Y"),
				"type": statement_text(TYPE_LABELS[r["movement"].category]),
				"liberapay": r["movement"].liberapay_transfer_id,
				"stripe_id": r["movement"].id,
				"gross": r["movement"].amount,
				"fee": r["movement"].fee,
				"net": r["movement"].net,
				"balance": r["balance"],
			}
			for r in rows
		],
		"totals": totals,
		"reference_label": reference_label,
		"reference": reference,
		"difference": round(totals["closing"] - reference, 2),
		"ledger": round(flt(ledger), 2),
		"generated": formatdate(now_datetime()) + now_datetime().strftime(" %H:%M"),
		"title": statement_text("Stripe statement {0}").format(period),
		"lang": frappe.local.lang or "en",
	}


def format_eur(value: float) -> str:
	return fmt_money(round(flt(value), 2), precision=2)


def render_statement_html(year: int, month: int | None = None, context: dict[str, Any] | None = None) -> str:
	context = dict(context or statement_context(year, month))
	context["eur"] = format_eur
	context["fdate"] = formatdate
	return frappe.render_template("fab_banks_import/templates/stripe_statement.html", context)


def render_statement_pdf(year: int, month: int | None = None, html: str | None = None) -> bytes:
	from fab.print_format import get_pdf as fab_get_pdf

	html = html or render_statement_html(year, month)
	pdf = fab_get_pdf("Standard", html, {}, None, pdf_generator="chrome_pdfa")
	if pdf is None:
		from frappe.utils.pdf import get_pdf

		pdf = get_pdf(html)
	return pdf


@frappe.whitelist()
def download_statement(year: int | str, month: int | str | None = None) -> None:
	frappe.only_for(("System Manager", "Accounts Manager", "Accounts User"))
	year = int(year)
	month = int(month) if month else None
	if month is not None and not 1 <= month <= 12:
		frappe.throw(_("Invalid month {0}").format(month))
	frappe.local.response.filename = statement_filename(year, month)
	frappe.local.response.filecontent = render_statement_pdf(year, month)
	frappe.local.response.type = "pdf"


# --------------------------------------------------------------------------
# setup


def find_account(company: str, number: str | None, name: str | None = None) -> str | None:
	account = None
	if number:
		account = frappe.db.get_value(
			"Account", {"company": company, "account_number": number, "is_group": 0}
		)
	if not account and name:
		account = frappe.db.get_value("Account", {"company": company, "account_name": name, "is_group": 0})
	return account


def setup_stripe(company: str, api_key: str | None = None) -> dict[str, Any]:
	"""Idempotent: ledger account 182007 Stripe, Bank Account "Stripe" and settings.

	Only fills settings fields that are still empty; never enables the sync.
	"""
	paypal = find_account(company, "182006", "PayPal")
	if not paypal:
		frappe.throw(_("Account 182006 PayPal not found in {0}").format(company))
	stripe_account = find_account(company, "182007")
	if not stripe_account:
		doc = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Stripe",
				"account_number": "182007",
				"parent_account": frappe.db.get_value("Account", paypal, "parent_account"),
				"company": company,
				"account_type": "Bank",
				"account_currency": "EUR",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		stripe_account = doc.name
	if not frappe.db.exists("Bank", "Stripe"):
		frappe.get_doc({"doctype": "Bank", "bank_name": "Stripe"}).insert(ignore_permissions=True)
	bank_account = frappe.db.get_value("Bank Account", {"account": stripe_account, "company": company})
	if not bank_account:
		doc = frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": "Stripe",
				"bank": "Stripe",
				"is_company_account": 1,
				"company": company,
				"account": stripe_account,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		bank_account = doc.name
	sella_account = find_account(company, "182001")
	defaults = {
		"company": company,
		"bank_account": bank_account,
		"payout_bank_account": frappe.db.get_value(
			"Bank Account", {"account": sella_account, "company": company, "is_company_account": 1}
		),
		"income_account": find_account(company, "320200", "Miscellaneous income"),
		"fee_account": find_account(company, "5221", "Bank Charges"),
		"clearing_account": find_account(company, None, "Odoo Migration Clearing"),
	}
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	for field, value in defaults.items():
		if not settings.get(field) and value:
			settings.set(field, value)
	if api_key:
		settings.api_key = api_key
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()
	return {field: settings.get(field) for field in defaults} | {"stripe_account": stripe_account}
