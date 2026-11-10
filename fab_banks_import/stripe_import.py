from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, fmt_money, formatdate, getdate, now_datetime, nowdate
from frappe.utils.data import get_system_timezone

SETTINGS_DOCTYPE = "Stripe Import Settings"
STRIPE_API = "https://api.stripe.com/v1/"
STRIPE_TIMEOUT = 30
PAYOUT_MATCH_DAYS = 5
FETCH_OVERLAP_DAYS = 3
BANK_MATCH_RETRY_DAYS = 60
SYNC_LOCK = "fab_banks_import_stripe_sync"
SYNC_LOCK_TIMEOUT = 900
REFUND_AS_FEE = "Fee"
# sums are rounded to three decimals, enough for every Stripe currency
EPSILON = 0.0005

# https://docs.stripe.com/currencies#zero-decimal and #three-decimal
ZERO_DECIMAL_CURRENCIES = {
	"BIF",
	"CLP",
	"DJF",
	"GNF",
	"JPY",
	"KMF",
	"KRW",
	"MGA",
	"PYG",
	"RWF",
	"UGX",
	"VND",
	"VUV",
	"XAF",
	"XOF",
	"XPF",
}
THREE_DECIMAL_CURRENCIES = {"BHD", "JOD", "KWD", "OMR", "TND"}

# what a balance transaction does to the ledger; anything missing here goes
# to the "other" account and is flagged in the sync log
KIND_BY_TYPE = {
	"charge": "charge",
	"payment": "charge",
	"refund": "refund",
	"payment_refund": "refund",
	"refund_failure": "refund",
	"payment_failure_refund": "refund",
	"stripe_fee": "fee",
	"stripe_fx_fee": "fee",
	"tax_fee": "fee",
	"adjustment": "fee",
	"payout": "payout",
	"payout_failure": "payout",
	"payout_cancel": "payout",
}
PAYOUT_TYPES = [t for t, kind in KIND_BY_TYPE.items() if kind == "payout"]
FAILED_PAYOUT_STATUSES = {"failed", "canceled"}
DISPUTE_CATEGORIES = {"dispute", "dispute_reversal"}
KIND_LABELS = {
	"charge": "Charge",
	"refund": "Refund",
	"dispute": "Dispute",
	"fee": "Stripe fee",
	"payout": "Payout",
	"other": "Other",
}


@dataclass(slots=True)
class StripeMovement:
	id: str
	type: str
	kind: str
	posting_date: date
	amount: float
	fee: float
	net: float
	currency: str = ""
	source_id: str = ""
	charge_id: str = ""
	description: str = ""
	context: dict[str, Any] = field(default_factory=dict)
	reference: str = ""
	category: str = ""
	account: str = ""
	cost_center: str = ""
	matched: bool = False
	payout_arrival_date: date | None = None
	payout_status: str = ""


@dataclass(slots=True)
class Defaults:
	income_account: str
	fee_account: str
	other_account: str = ""
	cost_center: str = ""


@dataclass(slots=True)
class MonthLines:
	lines: dict[tuple[str, str], float] = field(default_factory=dict)
	deposits: float = 0.0
	withdrawals: float = 0.0
	categories: list[str] = field(default_factory=list)
	count: int = 0

	@property
	def net(self) -> float:
		return rnd(self.deposits - self.withdrawals)


def rnd(value: float) -> float:
	return round(value, 3)


def differs(a: float, b: float) -> bool:
	return abs(rnd(flt(a) - flt(b))) > EPSILON


def to_amount(value: int | None, currency: str) -> float:
	"""Stripe amounts are integers in the currency's smallest unit."""
	currency = (currency or "").upper()
	if currency in ZERO_DECIMAL_CURRENCIES:
		return float(value or 0)
	if currency in THREE_DECIMAL_CURRENCIES:
		return round((value or 0) / 1000, 3)
	return round((value or 0) / 100, 2)


def charge_context(source: dict[str, Any] | None) -> dict[str, Any]:
	"""What rules can look at on a charge: metadata, description, customer."""
	if not isinstance(source, dict):
		return {}
	customer = source.get("customer")
	email = (source.get("billing_details") or {}).get("email") or source.get("receipt_email") or ""
	if isinstance(customer, dict):
		email = email or customer.get("email") or ""
		customer = customer.get("id")
	return {
		"metadata": source.get("metadata") or {},
		"description": source.get("description") or "",
		"customer": customer or "",
		"email": email,
	}


def movement_kind(raw: dict[str, Any]) -> str:
	source = raw.get("source")
	if raw.get("reporting_category") in DISPUTE_CATEGORIES or (
		isinstance(source, dict) and source.get("object") == "dispute"
	):
		return "dispute"
	if raw.get("type") == "adjustment" and (raw.get("amount") or 0) > 0:
		# a credit from Stripe is no fee: book it like any other movement
		return "other"
	return KIND_BY_TYPE.get(raw.get("type"), "other")


def payout_details(movement: StripeMovement, source: dict[str, Any]) -> None:
	movement.source_id = source.get("id") or movement.source_id
	movement.payout_status = source.get("status") or ""
	if source.get("arrival_date"):
		# arrival_date is midnight UTC of the bank value date
		movement.payout_arrival_date = datetime.fromtimestamp(source["arrival_date"], UTC).date()


def parse_balance_transaction(raw: dict[str, Any], tz: ZoneInfo) -> StripeMovement:
	"""Turn one /v1/balance_transactions row (expanded source) into a movement."""
	currency = (raw.get("currency") or "").upper()
	amount, fee, net = (to_amount(raw.get(k), currency) for k in ("amount", "fee", "net"))
	if differs(amount - fee, net):
		raise ValueError(f"{raw['id']}: amount {amount} - fee {fee} != net {net}")
	kind = movement_kind(raw)
	source = raw.get("source")
	movement = StripeMovement(
		id=raw["id"],
		type=raw.get("type") or "",
		kind=kind,
		posting_date=datetime.fromtimestamp(raw["created"], tz).date(),
		amount=amount,
		fee=fee,
		net=net,
		currency=currency,
		description=raw.get("description") or "",
	)
	if isinstance(source, dict):
		movement.source_id = source.get("id") or ""
		charge = source.get("charge")
		movement.charge_id = (charge.get("id") if isinstance(charge, dict) else charge) or ""
		if kind == "charge":
			movement.context = charge_context(source)
		elif isinstance(charge, dict):
			movement.context = charge_context(charge)
		if kind == "payout":
			payout_details(movement, source)
	elif isinstance(source, str):
		movement.source_id = source
	if kind == "payout" and not movement.payout_arrival_date:
		raise ValueError(f"{raw['id']}: payout without arrival date, expand the source")
	return movement


def sorted_rules(rows: list[Any]) -> list[Any]:
	return sorted(rows or [], key=lambda r: (cint(r.get("priority")), cint(r.get("idx"))))


def rule_matches(rule: Any, movement_type: str, kind: str, context: dict[str, Any]) -> bool:
	"""Every condition set on the rule must hold; an empty condition is ignored."""
	if rule.get("movement_type") and rule.get("movement_type") not in (movement_type, kind):
		return False
	metadata = context.get("metadata") or {}
	key = rule.get("metadata_key")
	if key:
		if key not in metadata:
			return False
		if rule.get("metadata_value") and str(metadata[key]) != rule.get("metadata_value"):
			return False
	text = rule.get("description_contains")
	if text and text.lower() not in (context.get("description") or "").lower():
		return False
	customer = (rule.get("customer") or "").strip().lower()
	if customer and customer not in (
		str(context.get("customer") or "").lower(),
		str(context.get("email") or "").lower(),
	):
		return False
	return True


def first_rule(rules: list[Any], movement_type: str, kind: str, context: dict[str, Any]) -> Any | None:
	return next((r for r in rules if rule_matches(r, movement_type, kind, context)), None)


def classify(
	movements: list[StripeMovement],
	rules: list[Any],
	defaults: Defaults,
	charges: dict[str, tuple[str, dict[str, Any]]] | None = None,
) -> None:
	"""Set reference, category, account and cost center on each movement.

	Refunds and disputes follow the rule of their original charge, looked up
	by charge id in the movements themselves or in `charges` (id -> (type,
	context)) for charges outside the fetched window.
	"""
	rules = sorted_rules(rules)
	known = dict(charges or {})
	known.update({m.source_id: (m.type, m.context) for m in movements if m.kind == "charge" and m.source_id})
	for m in movements:
		if m.kind == "payout":
			continue
		if m.kind in ("refund", "dispute"):
			charge_type, context = known.get(m.charge_id, ("", m.context))
			rule = first_rule(rules, charge_type or m.type, "charge" if charge_type else m.kind, context)
		else:
			context = m.context or {"description": m.description}
			rule = first_rule(rules, m.type, m.kind, context)
		m.matched = rule is not None
		m.category = (rule.get("label") if rule else "") or ""
		m.cost_center = (rule.get("cost_center") if rule else "") or defaults.cost_center
		key = rule.get("reference_key") if rule else ""
		m.reference = str((context.get("metadata") or {}).get(key) or "") if key else ""
		m.reference = m.reference or context.get("description") or m.description
		rule_account = (rule.get("income_account") if rule else "") or ""
		income = rule_account or defaults.income_account
		if m.kind == "charge":
			m.account = income
		elif m.kind == "refund":
			refund_as_fee = rule and rule.get("refund_treatment") == REFUND_AS_FEE
			m.account = defaults.fee_account if refund_as_fee else income
		elif m.kind == "dispute":
			m.account = income
		elif m.kind == "fee":
			m.account = defaults.fee_account
		else:
			m.account = rule_account or defaults.other_account


def month_lines(movements: list[StripeMovement], fee_account: str) -> MonthLines:
	"""Journal lines of one month: gross per (account, cost center), fees, net."""
	month = MonthLines()
	for m in movements:
		if m.kind == "payout":
			continue
		if not m.account:
			raise ValueError(f"{m.id}: no account to book {m.type} on")
		month.count += 1
		key = (m.account, m.cost_center)
		month.lines[key] = month.lines.get(key, 0.0) - m.amount
		if m.fee:
			fee_key = (fee_account, m.cost_center)
			month.lines[fee_key] = month.lines.get(fee_key, 0.0) + m.fee
		if m.net > 0:
			month.deposits += m.net
		else:
			month.withdrawals -= m.net
		if m.category and m.category not in month.categories:
			month.categories.append(m.category)
	month.lines = {k: rnd(v) for k, v in month.lines.items() if rnd(v)}
	month.deposits, month.withdrawals = rnd(month.deposits), rnd(month.withdrawals)
	if differs(sum(month.lines.values()), -month.net):
		raise ValueError(f"month lines do not balance: {month.lines} against net {month.net}")
	return month


def compute_opening_balance(current_balance: float, movements: list[StripeMovement]) -> float:
	"""Balance at start_date - 1, walked back from today's Stripe balance."""
	return rnd(current_balance - sum(m.net for m in movements))


def check_balance_invariant(opening: float, movements: list[StripeMovement], current_balance: float) -> None:
	computed = rnd(opening + sum(m.net for m in movements))
	if differs(computed, current_balance):
		raise ValueError(
			f"Stripe balance mismatch: opening {opening} + movements {rnd(computed - opening)}"
			f" = {computed} != Stripe balance {current_balance}"
		)


def match_payouts(
	payouts: list[dict[str, Any]],
	candidates: list[dict[str, Any]],
	description_contains: str = "",
	days: int = PAYOUT_MATCH_DAYS,
) -> dict[str, str | None]:
	"""Pair payouts ({key, amount, arrival_date}) with unreconciled bank deposits.

	Nearest date first and one deposit per payout. When a payout has two
	equally close deposits, or a deposit is equally close to two payouts,
	those stay unmatched so a human decides.
	"""
	needle = (description_contains or "").strip().lower()
	pairs = []
	for payout in payouts:
		for row in candidates:
			if differs(row.get("deposit"), payout["amount"]):
				continue
			if needle and needle not in (row.get("description") or "").lower():
				continue
			if flt(row.get("unallocated_amount")) <= 0 or row.get("status") == "Reconciled":
				continue
			distance = abs((getdate(row["date"]) - payout["arrival_date"]).days)
			if distance <= days:
				pairs.append((distance, payout["key"], row["name"]))
	result: dict[str, str | None] = {p["key"]: None for p in payouts}
	taken_payouts: set[str] = set()
	taken_rows: set[str] = set()
	for distance in sorted({d for d, _p, _r in pairs}):
		level = [
			(p, r) for d, p, r in pairs if d == distance and p not in taken_payouts and r not in taken_rows
		]
		per_payout = Counter(p for p, _r in level)
		per_row = Counter(r for _p, r in level)
		for payout_key, row_name in level:
			if per_payout[payout_key] == 1 and per_row[row_name] == 1:
				result[payout_key] = row_name
				taken_rows.add(row_name)
		# ambiguous payouts and deposits must not fall back to a farther match
		taken_payouts.update(p for p, _r in level)
		taken_rows.update(r for _p, r in level if per_row[r] > 1)
	return result


def movement_label(m: StripeMovement) -> str:
	return m.category or statement_text(KIND_LABELS.get(m.kind, "Other"))


def statement_rows(
	opening: float, movements: list[StripeMovement], fee_account: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
	"""Running balance rows and totals for the statement.

	Totals: gross per category, fees (fee column plus anything booked on the
	fee account), refunds (refunds and disputes on an income account), payouts.
	"""
	rows = []
	balance = opening
	categories: dict[str, float] = {}
	fees = refunds = payouts = 0.0
	for m in sorted(movements, key=lambda m: (m.posting_date, m.id)):
		balance = rnd(balance + m.net)
		rows.append({"movement": m, "balance": balance})
		fees += m.fee
		if m.kind == "payout":
			payouts -= m.amount
		elif m.account == fee_account:
			fees -= m.amount
		elif m.kind in ("refund", "dispute"):
			refunds -= m.amount
		else:
			label = movement_label(m)
			categories[label] = categories.get(label, 0.0) + m.amount
	totals = {
		"categories": {k: rnd(v) for k, v in categories.items()},
		"fees": rnd(fees),
		"refunds": rnd(refunds),
		"payouts": rnd(payouts),
		"opening": rnd(opening),
		"closing": balance,
	}
	computed = rnd(opening + sum(categories.values()) - fees - refunds - payouts)
	if differs(computed, balance):
		raise ValueError(f"statement totals {computed} do not add up to the closing balance {balance}")
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


def fetch_balance(api_key: str, currency: str) -> float:
	balance = stripe_get(api_key, "balance")
	rows = [*balance.get("available", []), *balance.get("pending", [])]
	return rnd(sum(to_amount(r["amount"], r["currency"]) for r in rows if r["currency"].upper() == currency))


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


def fetch_movements(
	api_key: str, from_date: date, tz: ZoneInfo, currency: str
) -> tuple[list[StripeMovement], list[str]]:
	"""Movements created from from_date in the account currency, and the ids skipped
	because Stripe holds them in another currency."""
	created_from = int(datetime.combine(from_date, datetime.min.time(), tz).timestamp())
	movements = [parse_balance_transaction(r, tz) for r in fetch_balance_transactions(api_key, created_from)]
	skipped = [f"{m.id} {m.currency}" for m in movements if m.currency != currency]
	movements = sorted((m for m in movements if m.currency == currency), key=lambda m: (m.posting_date, m.id))
	return movements, skipped


def fetch_stable_window(
	api_key: str, from_date: date, tz: ZoneInfo, currency: str
) -> tuple[list[StripeMovement], list[str], float]:
	"""The opening is walked back from the balance, so the list and the balance
	must describe the same moment: read the balance on both sides of the list."""
	for _attempt in range(3):
		before = fetch_balance(api_key, currency)
		movements, skipped = fetch_movements(api_key, from_date, tz, currency)
		after = fetch_balance(api_key, currency)
		if not differs(before, after):
			return movements, skipped, after
	frappe.throw(_("Stripe balance kept changing during the first sync, retry later"))


def fetch_missing_charges(
	api_key: str, movements: list[StripeMovement], pending: set[str], flagged: list[str]
) -> dict[str, tuple[str, dict]]:
	"""Charges outside the window whose reversals still need classifying."""
	known = {m.source_id for m in movements if m.kind == "charge"}
	missing = {
		m.charge_id for m in movements if m.kind in ("refund", "dispute") and m.charge_id and m.id in pending
	} - known
	charges = {}
	for charge_id in sorted(missing):
		try:
			context = charge_context(stripe_get(api_key, f"charges/{charge_id}"))
		except frappe.ValidationError:
			flagged.append(f"{charge_id} charge not readable, reversal left unclassified")
			continue
		charges[charge_id] = ("payment" if charge_id.startswith("py_") else "charge", context)
	return charges


# --------------------------------------------------------------------------
# ERPNext side


def commit() -> None:
	frappe.db.commit()


def rollback() -> None:
	frappe.db.rollback()


@contextmanager
def sync_lock():
	"""Serialise syncs and statement sends across workers and requests.

	A named MariaDB lock is taken first, then a commit starts a fresh snapshot,
	so every read sees what the previous holder committed. The work is
	committed before the lock is released. Nested use joins the outer lock.
	"""
	if getattr(frappe.local, "stripe_sync_lock", False):
		yield
		return
	if not frappe.db.sql("select get_lock(%s, %s)", (SYNC_LOCK, SYNC_LOCK_TIMEOUT))[0][0]:
		frappe.throw(_("Another Stripe sync is still running, retry later"))
	frappe.local.stripe_sync_lock = True
	try:
		commit()
		yield
		commit()
	except Exception:
		rollback()
		raise
	finally:
		frappe.local.stripe_sync_lock = False
		frappe.db.sql("select release_lock(%s)", (SYNC_LOCK,))


def get_settings():
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	required = ("company", "bank_account", "income_account", "fee_account", "clearing_account", "start_date")
	missing = [f for f in required if not settings.get(f)]
	if missing:
		frappe.throw(_("Stripe Import Settings incomplete: {0}").format(", ".join(missing)))
	return settings


def settings_defaults(settings) -> Defaults:
	return Defaults(
		income_account=settings.income_account,
		fee_account=settings.fee_account,
		other_account=settings.other_account or "",
		cost_center=settings.default_cost_center
		or frappe.get_cached_value("Company", settings.company, "cost_center"),
	)


def bank_gl_account(bank_account: str) -> str:
	return frappe.db.get_value("Bank Account", bank_account, "account")


def sync() -> dict[str, Any]:
	"""Import Stripe movements, post opening, closed months and payouts.

	Every step is idempotent: running it twice creates nothing new.
	"""
	with sync_lock():
		return _sync(get_settings())


def _sync(settings) -> dict[str, Any]:
	api_key = settings.get_password("api_key")
	tz = ZoneInfo(get_system_timezone())
	start_date = getdate(settings.start_date)
	stripe_account = bank_gl_account(settings.bank_account)
	currency = frappe.get_cached_value("Account", stripe_account, "account_currency")
	company_currency = frappe.get_cached_value("Company", settings.company, "default_currency")
	payout_currency = settings.payout_bank_account and frappe.get_cached_value(
		"Account", bank_gl_account(settings.payout_bank_account), "account_currency"
	)
	if currency != company_currency or (payout_currency and payout_currency != currency):
		frappe.throw(_("The Stripe account, its payout bank and the company must share one currency"))
	if currency in THREE_DECIMAL_CURRENCIES and cint(frappe.db.get_default("currency_precision") or 2) < 3:
		frappe.throw(_("Set the system Currency Precision to 3 to import {0}").format(currency))
	if settings.currency != currency:
		settings.db_set("currency", currency)
	results: dict[str, Any] = {
		"transactions_created": 0,
		"transactions_classified": 0,
		"transactions_skipped": 0,
		"flagged": [],
		"skipped_other_currency": [],
	}
	first_run = not settings.opening_journal_entry and not imported_count(settings)
	# rows imported before classification existed need the whole window once
	full_window = first_run or bool(unclassified_count(settings))
	movements: list[StripeMovement] = []
	balance = 0.0
	for attempt in range(2):
		# the first run reads everything to walk the opening back; later runs
		# read from the last imported day minus an overlap
		from_date = start_date
		if not full_window:
			from_date = max(start_date, last_imported_date(settings) - timedelta(days=FETCH_OVERLAP_DAYS))
		if first_run:
			batch, skipped, balance = fetch_stable_window(api_key, from_date, tz, currency)
		else:
			batch, skipped = fetch_movements(api_key, from_date, tz, currency)
			balance = fetch_balance(api_key, currency)
		if any(m.posting_date < start_date for m in batch):
			frappe.throw(_("Stripe returned movements before the start date"))
		import_batch(settings, batch, results)
		movements = merge_movements(movements, batch)
		results["skipped_other_currency"] = sorted(set(results["skipped_other_currency"]) | set(skipped))
		if first_run:
			results["opening"] = ensure_opening_entry(settings, movements, balance)
			first_run = False
		full_window = False
		if not differs(stored_balance(settings, start_date), balance):
			break
		if attempt:
			frappe.throw(
				_("Stripe Bank Transactions add up to {0} but Stripe holds {1}").format(
					stored_balance(settings, start_date), balance
				)
			)
	results.setdefault("opening", {"balance": flt(settings.opening_balance), "created": False})
	results["stripe_balance"] = balance
	results["movements"] = len(movements)
	results["by_type"] = dict(Counter(m.type for m in movements))
	results["unclassified"] = sum(1 for m in movements if m.kind != "payout" and not m.matched)
	results["months"] = post_closed_months(settings)
	results["payouts"] = settle_payouts(settings, api_key, movements)
	ledger_problem = check_ledger_balance(settings, start_date)
	if ledger_problem:
		results["flagged"].append(ledger_problem)
		frappe.log_error(ledger_problem, "fab_banks_import.stripe_import.sync")
		frappe.msgprint(ledger_problem, indicator="orange", alert=True)
	if results["skipped_other_currency"]:
		frappe.msgprint(
			_("Stripe movements in other currencies were not imported: {0}").format(
				", ".join(results["skipped_other_currency"])
			),
			indicator="orange",
			alert=True,
		)
	settings.db_set("last_sync", now_datetime())
	settings.db_set("last_sync_log", json.dumps(results, indent=1, default=str))
	return results


def merge_movements(known: list[StripeMovement], batch: list[StripeMovement]) -> list[StripeMovement]:
	by_id = {m.id: m for m in known}
	by_id.update({m.id: m for m in batch})
	return sorted(by_id.values(), key=lambda m: (m.posting_date, m.id))


def import_batch(settings, batch: list[StripeMovement], results: dict[str, Any]) -> None:
	pending = movements_to_classify(batch)
	charges = fetch_missing_charges(settings.get_password("api_key"), batch, pending, results["flagged"])
	classify(batch, settings.get("rules"), settings_defaults(settings), charges)
	other = [m for m in batch if m.kind == "other"]
	if any(not m.account for m in other):
		frappe.throw(_("Set the Other Movements Account: Stripe returned {0}").format(other[0].type))
	for m in other:
		if m.id in pending:
			results["flagged"].append(f"{m.id} {m.type} booked on {m.account}")
	for m in batch:
		results[f"transactions_{upsert_bank_transaction(settings, m)}"] += 1


def imported_count(settings) -> int:
	return frappe.db.count("Bank Transaction", {"bank_account": settings.bank_account, "docstatus": 1})


def unclassified_count(settings) -> int:
	return frappe.db.count(
		"Bank Transaction",
		{"bank_account": settings.bank_account, "docstatus": 1, "fab_stripe_kind": ["is", "not set"]},
	)


def last_imported_date(settings) -> date:
	last = frappe.db.sql(
		"select max(date) from `tabBank Transaction` where bank_account = %s and docstatus = 1",
		(settings.bank_account,),
	)[0][0]
	return getdate(last) if last else getdate(settings.start_date)


def movements_to_classify(movements: list[StripeMovement]) -> set[str]:
	"""Ids not imported yet or imported before classification existed."""
	done = set(
		frappe.get_all(
			"Bank Transaction",
			filters={
				"transaction_id": ["in", [m.id for m in movements] or [""]],
				"docstatus": ["<", 2],
				"fab_stripe_kind": ["is", "set"],
			},
			pluck="transaction_id",
		)
	)
	return {m.id for m in movements} - done


def stored_balance(settings, start_date: date) -> float:
	"""Opening plus every saved Stripe Bank Transaction: what Stripe should hold."""
	imported = frappe.db.sql(
		"""select coalesce(sum(deposit - withdrawal), 0) from `tabBank Transaction`
		where bank_account = %s and docstatus = 1 and date >= %s""",
		(settings.bank_account, start_date),
	)[0][0]
	return rnd(flt(settings.opening_balance) + flt(imported))


def check_ledger_balance(settings, start_date: date) -> str | None:
	"""The Stripe ledger account must equal what this module posted.

	Expected: opening + net of the non payout movements of closed months +
	internal transfers, so a manual entry or a lost document shows up in the sync log.
	"""
	stripe_account = bank_gl_account(settings.bank_account)
	today = getdate(nowdate())
	last_closed = date(today.year, today.month, 1) - timedelta(days=1)
	movements = frappe.db.sql(
		"""select coalesce(sum(deposit - withdrawal), 0) from `tabBank Transaction`
		where bank_account = %s and docstatus = 1 and date between %s and %s
		and transaction_type not in %s""",
		(settings.bank_account, start_date, last_closed, tuple(PAYOUT_TYPES)),
	)[0][0]
	transfers = frappe.db.sql(
		"""select coalesce(sum(case when paid_to = %(account)s then received_amount else 0 end)
			- sum(case when paid_from = %(account)s then paid_amount else 0 end), 0)
		from `tabPayment Entry` where docstatus = 1 and payment_type = 'Internal Transfer'
		and (paid_from = %(account)s or paid_to = %(account)s)""",
		{"account": stripe_account},
	)[0][0]
	expected = rnd(flt(settings.opening_balance) + flt(movements) + flt(transfers))
	ledger = frappe.db.sql(
		"select coalesce(sum(debit - credit), 0) from `tabGL Entry` where account = %s and is_cancelled = 0",
		(stripe_account,),
	)[0][0]
	if differs(ledger, expected):
		return _("Ledger account {0} holds {1}, the Stripe import expects {2}").format(
			stripe_account, rnd(flt(ledger)), expected
		)
	return None


def ensure_opening_entry(settings, movements: list[StripeMovement], balance: float) -> dict[str, Any]:
	opening = compute_opening_balance(balance, movements)
	check_balance_invariant(opening, movements, balance)
	posting_date = getdate(settings.start_date) - timedelta(days=1)
	journal_entry = None
	if opening:
		cost_center = frappe.get_cached_value("Company", settings.company, "cost_center")
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Journal Entry",
				"company": settings.company,
				"posting_date": posting_date,
				"cheque_no": "STRIPE-OPENING",
				"cheque_date": posting_date,
				"user_remark": ledger_text("Stripe opening balance on {0}", formatdate(posting_date)),
				"accounts": [
					journal_line(bank_gl_account(settings.bank_account), opening, cost_center),
					journal_line(settings.clearing_account, -opening, cost_center),
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


def journal_line(account: str, signed_amount: float, cost_center: str) -> dict[str, Any]:
	"""A positive amount debits the account, a negative one credits it."""
	return {
		"account": account,
		"debit_in_account_currency": signed_amount if signed_amount > 0 else 0,
		"credit_in_account_currency": -signed_amount if signed_amount < 0 else 0,
		"cost_center": cost_center,
	}


def bank_transaction_description(m: StripeMovement) -> str:
	parts = [f"Stripe {m.type}"]
	if m.category:
		parts.append(m.category)
	if m.reference:
		parts.append(m.reference)
	if m.kind == "payout" and m.payout_arrival_date:
		parts.append(f"payout {m.source_id} arriving {m.payout_arrival_date.isoformat()}")
	parts.append(f"gross {m.amount} fee {m.fee} net {m.net}")
	return " | ".join(parts)


def classification_fields(m: StripeMovement) -> dict[str, Any]:
	return {
		"fab_stripe_reference": m.reference[:140],
		"fab_stripe_category": m.category,
		"fab_stripe_kind": m.kind,
		"fab_stripe_account": m.account or None,
		"fab_stripe_cost_center": m.cost_center or None,
	}


def upsert_bank_transaction(settings, m: StripeMovement) -> str:
	existing = frappe.db.get_value(
		"Bank Transaction",
		{"transaction_id": m.id, "docstatus": ["<", 2]},
		["name", "fab_stripe_kind", "fab_stripe_reference", "fab_stripe_account"],
		as_dict=True,
	)
	if existing:
		if existing.fab_stripe_kind:
			return "skipped"
		# rows imported before classification existed: fill the fields only,
		# amounts and their reconciliation stay as they are
		fields = classification_fields(m)
		if existing.fab_stripe_reference:
			fields.pop("fab_stripe_reference")
		if existing.fab_stripe_account:
			# classified by an earlier sync: keep what was booked
			for fieldname in ("fab_stripe_category", "fab_stripe_account", "fab_stripe_cost_center"):
				fields.pop(fieldname)
		frappe.db.set_value("Bank Transaction", existing.name, fields, update_modified=False)
		return "classified"
	if not (m.net or m.amount or m.fee):
		return "skipped"
	doc = frappe.get_doc(
		{
			"doctype": "Bank Transaction",
			"date": m.posting_date,
			"bank_account": settings.bank_account,
			"company": settings.company,
			"currency": m.currency,
			"deposit": m.net if m.net > 0 else 0,
			"withdrawal": -m.net if m.net < 0 else 0,
			"description": bank_transaction_description(m)[:500],
			"reference_number": m.source_id,
			"transaction_id": m.id,
			"transaction_type": m.type,
			"fab_stripe_gross": m.amount,
			"fab_stripe_fee": m.fee,
			**classification_fields(m),
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	return "created"


def bank_transaction_movements(
	bank_account: str, from_date: date, to_date: date, defaults: Defaults
) -> list[StripeMovement]:
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
			"fab_stripe_reference",
			"fab_stripe_category",
			"fab_stripe_account",
			"fab_stripe_cost_center",
			"fab_stripe_kind",
		],
		order_by="date asc, transaction_id asc",
	)
	movements = []
	for row in rows:
		kind = row.fab_stripe_kind or KIND_BY_TYPE.get(row.transaction_type, "other")
		if kind != "payout" and not row.fab_stripe_account:
			frappe.throw(
				_("Bank Transaction {0} is not classified yet: run the Stripe sync").format(row.name)
			)
		movements.append(
			StripeMovement(
				id=row.transaction_id,
				type=row.transaction_type,
				kind=kind,
				posting_date=getdate(row.date),
				amount=flt(row.fab_stripe_gross),
				fee=flt(row.fab_stripe_fee),
				net=rnd(flt(row.deposit) - flt(row.withdrawal)),
				source_id=row.reference_number or "",
				reference=row.fab_stripe_reference or "",
				category=row.fab_stripe_category or "",
				account=row.fab_stripe_account or "",
				cost_center=row.fab_stripe_cost_center or defaults.cost_center,
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
	defaults = settings_defaults(settings)
	movements = [
		m
		for m in bank_transaction_movements(settings.bank_account, first, last, defaults)
		if m.kind != "payout"
	]
	if not movements:
		return None
	totals = month_lines(movements, settings.fee_account)
	stripe_account = bank_gl_account(settings.bank_account)
	reference = month_reference(year, month)
	posted = frappe.get_all(
		"Journal Entry",
		filters={"cheque_no": reference, "company": settings.company, "docstatus": 1},
		pluck="name",
	)
	if len(posted) > 1:
		frappe.throw(_("Several Journal Entries carry the reference {0}: {1}").format(reference, posted))
	je_name = posted[0] if posted else None
	created = False
	if je_name:
		stripe_lines = frappe.db.sql(
			"""select sum(debit_in_account_currency), sum(credit_in_account_currency)
			from `tabJournal Entry Account` where parent = %s and account = %s""",
			(je_name, stripe_account),
		)[0]
		if differs(stripe_lines[0], totals.deposits) or differs(stripe_lines[1], totals.withdrawals):
			frappe.throw(
				_("Journal Entry {0} no longer matches the Stripe movements of {1}/{2}").format(
					je_name, f"{month:02d}", year
				)
			)
	else:
		company_cc = frappe.get_cached_value("Company", settings.company, "cost_center")
		lines = [journal_line(account, amount, cc) for (account, cc), amount in sorted(totals.lines.items())]
		# deposits and withdrawals stay on separate lines: bank reconciliation
		# allocates the sum of the absolute bank GL amounts across the rows
		if totals.deposits:
			lines.append(journal_line(stripe_account, totals.deposits, company_cc))
		if totals.withdrawals:
			lines.append(journal_line(stripe_account, -totals.withdrawals, company_cc))
		labels = ", ".join(totals.categories) or statement_text("unclassified movements")
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Bank Entry",
				"company": settings.company,
				"posting_date": last,
				"cheque_no": reference,
				"cheque_date": last,
				"user_remark": ledger_text("Stripe {0}: {1}", f"{month:02d}/{year}", labels),
				"accounts": lines,
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
		"lines": {f"{a} / {c}": v for (a, c), v in sorted(totals.lines.items())},
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
			"transaction_type": ["not in", PAYOUT_TYPES],
			"unallocated_amount": [">", 0],
		},
		pluck="name",
		order_by="date asc, transaction_id asc",
	)
	return sum(reconcile_against(name, "Journal Entry", je_name) for name in names)


def payout_entry(settings, payout_id: str) -> str | None:
	names = frappe.get_all(
		"Payment Entry",
		filters={"reference_no": payout_id, "company": settings.company, "docstatus": 1},
		pluck="name",
	)
	if len(names) > 1:
		frappe.throw(_("Several Payment Entries carry the payout {0}: {1}").format(payout_id, names))
	return names[0] if names else None


def unsettled_payouts(settings, api_key: str, movements: list[StripeMovement]) -> list[StripeMovement]:
	"""Payouts of this window plus older ones still waiting for their transfer."""
	payouts = {m.id: m for m in movements if m.kind == "payout"}
	waiting = frappe.get_all(
		"Bank Transaction",
		filters={
			"bank_account": settings.bank_account,
			"docstatus": 1,
			"transaction_type": "payout",
			"unallocated_amount": [">", 0],
			"transaction_id": ["not in", list(payouts) or [""]],
		},
		fields=["transaction_id", "reference_number", "date", "withdrawal"],
	)
	for row in waiting:
		if frappe.db.exists("Payment Entry", {"reference_no": row.reference_number, "docstatus": 2}):
			# its transfer was reversed after the payout failed
			continue
		movement = StripeMovement(
			id=row.transaction_id,
			type="payout",
			kind="payout",
			posting_date=getdate(row.date),
			amount=-flt(row.withdrawal),
			fee=0.0,
			net=-flt(row.withdrawal),
		)
		payout_details(movement, stripe_get(api_key, f"payouts/{row.reference_number}"))
		payouts[movement.id] = movement
	return sorted(payouts.values(), key=lambda m: (m.posting_date, m.id))


def settle_payouts(settings, api_key: str, movements: list[StripeMovement]) -> list[dict[str, Any]]:
	"""Transfer Stripe -> payout bank for every arrived payout, then match all
	still unmatched transfers against the bank deposits in one pass."""
	results = []
	to_match = []
	for m in unsettled_payouts(settings, api_key, movements):
		result: dict[str, Any] = {"payout": m.source_id, "type": m.type, "amount": -m.net}
		results.append(result)
		if m.type != "payout" or m.payout_status in FAILED_PAYOUT_STATUSES:
			# the money came back: a transfer booked for it must go
			result["reversed"] = reverse_payout_entry(settings, m.source_id)
			continue
		if not settings.payout_bank_account:
			result["pending"] = "no payout bank account configured"
			continue
		if m.payout_status != "paid" or m.payout_arrival_date > getdate(nowdate()):
			result["pending"] = m.payout_status or "unknown"
			continue
		pe_name, result["created"] = ensure_payout_entry(settings, m)
		result["payment_entry"] = pe_name
		stripe_bt = frappe.db.get_value("Bank Transaction", {"transaction_id": m.id, "docstatus": 1})
		result["stripe_reconciled"] = bool(stripe_bt) and reconcile_against(
			stripe_bt, "Payment Entry", pe_name
		)
		matched = bank_side_of(settings, pe_name)
		if matched:
			result["bank_transaction"], result["bank_matched"] = matched, "already"
		else:
			to_match.append((result, m, pe_name))
	to_match += stale_transfers(settings, {pe for _r, _m, pe in to_match}, results)
	if to_match:
		match_bank_side(settings, to_match)
	return results


def stale_transfers(settings, queued: set[str], results: list[dict[str, Any]]) -> list[tuple]:
	"""Recent payout transfers whose bank deposit was not imported yet last time."""
	if not settings.payout_bank_account:
		return []
	transfers = frappe.get_all(
		"Payment Entry",
		filters={
			"company": settings.company,
			"docstatus": 1,
			"payment_type": "Internal Transfer",
			"paid_from": bank_gl_account(settings.bank_account),
			"paid_to": bank_gl_account(settings.payout_bank_account),
			"reference_no": ["like", "po_%"],
			"posting_date": [">=", getdate(nowdate()) - timedelta(days=BANK_MATCH_RETRY_DAYS)],
		},
		fields=["name", "reference_no", "posting_date", "paid_amount"],
	)
	pending = []
	for pe in transfers:
		if pe.name in queued or bank_side_of(settings, pe.name):
			continue
		movement = StripeMovement(
			id=pe.reference_no,
			type="payout",
			kind="payout",
			posting_date=getdate(pe.posting_date),
			amount=-flt(pe.paid_amount),
			fee=0.0,
			net=-flt(pe.paid_amount),
			source_id=pe.reference_no,
			payout_arrival_date=getdate(pe.posting_date),
		)
		result = {
			"payout": pe.reference_no,
			"type": "payout",
			"amount": flt(pe.paid_amount),
			"payment_entry": pe.name,
		}
		results.append(result)
		pending.append((result, movement, pe.name))
	return pending


def ensure_payout_entry(settings, m: StripeMovement) -> tuple[str, bool]:
	pe_name = payout_entry(settings, m.source_id)
	if pe_name:
		return pe_name, False
	amount = -m.net
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
	return pe.name, True


def bank_side_of(settings, pe_name: str) -> str | None:
	rows = frappe.db.sql(
		"""select bt.name from `tabBank Transaction` bt
		join `tabBank Transaction Payments` p on p.parent = bt.name
		where bt.bank_account = %s and bt.docstatus = 1
		and p.payment_document = 'Payment Entry' and p.payment_entry = %s""",
		(settings.payout_bank_account, pe_name),
	)
	return rows[0][0] if rows else None


def match_bank_side(settings, to_match: list[tuple[dict, StripeMovement, str]]) -> None:
	dates = [m.payout_arrival_date for _r, m, _p in to_match]
	candidates = frappe.get_all(
		"Bank Transaction",
		filters={
			"bank_account": settings.payout_bank_account,
			"docstatus": 1,
			"deposit": [">", 0],
			"date": [
				"between",
				[
					min(dates) - timedelta(days=PAYOUT_MATCH_DAYS),
					max(dates) + timedelta(days=PAYOUT_MATCH_DAYS),
				],
			],
		},
		fields=["name", "date", "deposit", "description", "unallocated_amount", "status"],
	)
	pairs = match_payouts(
		[{"key": pe, "amount": -m.net, "arrival_date": m.payout_arrival_date} for _r, m, pe in to_match],
		candidates,
		settings.payout_description_contains,
	)
	for result, _m, pe_name in to_match:
		row = pairs.get(pe_name)
		if not row:
			result["bank_matched"] = "no match"
			continue
		reconcile_against(row, "Payment Entry", pe_name)
		result["bank_transaction"], result["bank_matched"] = row, "matched"


def reverse_payout_entry(settings, payout_id: str) -> str | None:
	"""Cancel the transfer of a payout that failed or was canceled after it;
	ERPNext unlinks it from its Bank Transactions on cancel."""
	pe_name = payout_entry(settings, payout_id)
	if not pe_name:
		return None
	pe = frappe.get_doc("Payment Entry", pe_name)
	pe.flags.ignore_permissions = True
	frappe.db.savepoint("stripe_reverse_payout")
	try:
		pe.cancel()
	except frappe.ValidationError:
		frappe.db.rollback(save_point="stripe_reverse_payout")
		frappe.log_error(f"Could not cancel {pe_name} for failed payout {payout_id}", "fab_banks_import")
		return f"cancel failed: {pe_name}"
	return pe_name


# --------------------------------------------------------------------------
# entry points


@frappe.whitelist(methods=["POST"])
def sync_now() -> dict[str, Any]:
	frappe.only_for(("System Manager", "Accounts Manager"))
	return sync()


def daily_sync() -> None:
	"""Sync, then email last month's statement when due; both only when enabled."""
	if not frappe.db.get_single_value(SETTINGS_DOCTYPE, "enabled"):
		return
	try:
		sync()
	except Exception:
		frappe.log_error("Stripe sync failed", "fab_banks_import.stripe_import.daily_sync")
		raise
	try:
		send_due_statement()
	except Exception:
		frappe.log_error("Stripe statement email failed", "fab_banks_import.stripe_import.daily_sync")
		raise


def send_due_statement() -> dict[str, Any] | None:
	with sync_lock():
		settings = frappe.get_single(SETTINGS_DOCTYPE)
		due = statement_due(settings, getdate(nowdate()))
		return _send_statement(*due, settings=settings) if due else None


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


def statement_email_body(label: str, totals: dict[str, Any], currency: str) -> str:
	rows = [
		(statement_text("Opening balance"), totals["opening"]),
		*totals["categories"].items(),
		(statement_text("Fees"), -totals["fees"]),
		(statement_text("Refunds"), -totals["refunds"]),
		(statement_text("Payouts"), -totals["payouts"]),
	]
	lines = "".join(
		f"<tr><td>{escape_html(text)}</td>"
		f"<td align='right'>{format_amount(value, currency)} {currency}</td></tr>"
		for text, value in rows
	)
	closing = statement_text("Closing balance")
	return (
		f"<p>{statement_text('Attached is the Stripe statement for {0}.').format(label)}</p>"
		f"<table>{lines}<tr><td><b>{closing}</b></td>"
		f"<td align='right'><b>{format_amount(totals['closing'], currency)} {currency}</b></td></tr></table>"
	)


def send_statement(year: int, month: int, force: bool = False) -> dict[str, Any]:
	"""Email the statement of one month under the sync lock."""
	with sync_lock():
		return _send_statement(year, month, frappe.get_single(SETTINGS_DOCTYPE), force=force)


def _send_statement(year: int, month: int, settings, force: bool = False) -> dict[str, Any]:
	"""One mail per recipient language; last_statement_sent guards repeats."""
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
				message=statement_email_body(label, context["totals"], context["currency"]),
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
	"""Ledger remarks use the system language, whoever runs the sync."""
	with use_language(system_language()):
		return statement_text(message).format(*args)


def statement_context(year: int, month: int | None = None) -> dict[str, Any]:
	settings = get_settings()
	defaults = settings_defaults(settings)
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
	before = bank_transaction_movements(
		settings.bank_account, start_date, first - timedelta(days=1), defaults
	)
	opening = rnd(flt(settings.opening_balance) + sum(m.net for m in before))
	movements = bank_transaction_movements(settings.bank_account, first, last, defaults)
	rows, totals = statement_rows(opening, movements, settings.fee_account)
	stripe_account = bank_gl_account(settings.bank_account)
	currency = frappe.get_cached_value("Account", stripe_account, "account_currency")
	today = getdate(nowdate())
	if last >= today:
		reference_label = statement_text("current Stripe balance (API /v1/balance)")
		reference = fetch_balance(settings.get_password("api_key"), currency)
	else:
		reference_label = statement_text("computed running balance")
		reference = totals["closing"]
	ledger = frappe.db.sql(
		"""select coalesce(sum(debit_in_account_currency - credit_in_account_currency), 0)
		from `tabGL Entry` where account = %s and is_cancelled = 0 and posting_date <= %s""",
		(stripe_account, min(last, today)),
	)[0][0]
	return {
		"company": settings.company,
		"bank_account": settings.bank_account,
		"gl_account": stripe_account,
		"currency": currency,
		"period": period,
		"first": first,
		"last": last,
		"rows": [
			{
				"date": formatdate(r["movement"].posting_date),
				"category": movement_label(r["movement"]),
				"reference": r["movement"].reference,
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
		"difference": rnd(totals["closing"] - reference),
		"ledger": rnd(flt(ledger)),
		"generated": formatdate(now_datetime()) + now_datetime().strftime(" %H:%M"),
		"title": statement_text("Stripe statement {0}").format(period),
		"lang": frappe.local.lang or "en",
	}


def format_amount(value: float, currency: str | None = None) -> str:
	precision = None
	if currency in ZERO_DECIMAL_CURRENCIES:
		precision = 0
	elif currency in THREE_DECIMAL_CURRENCIES:
		precision = 3
	return fmt_money(flt(value), currency=None, precision=precision)


def render_statement_html(year: int, month: int | None = None, context: dict[str, Any] | None = None) -> str:
	context = dict(context or statement_context(year, month))
	currency = context.get("currency")
	context["amount"] = lambda value: format_amount(value, currency)
	context["fdate"] = formatdate
	return frappe.render_template("fab_banks_import/templates/stripe_statement.html", context)


def statement_pdf_generator() -> str:
	"""The site's choice: site config `stripe_statement_pdf_generator`, else Print Settings."""
	return frappe.conf.get("stripe_statement_pdf_generator") or (
		frappe.db.get_single_value("Print Settings", "pdf_generator") or "wkhtmltopdf"
	)


def render_statement_pdf(year: int, month: int | None = None, html: str | None = None) -> bytes:
	html = html or render_statement_html(year, month)
	generator = statement_pdf_generator()
	if generator != "wkhtmltopdf":
		# the same hook chain Frappe uses for print formats
		for method in frappe.get_hooks("pdf_generator"):
			pdf = frappe.get_attr(method)("Standard", html, {}, None, pdf_generator=generator)
			if pdf:
				return pdf
	from frappe.utils.pdf import get_pdf

	return get_pdf(html)


@frappe.whitelist()
def download_statement(year: int | str, month: int | str | None = None) -> None:
	settings = get_settings()
	if not (
		frappe.has_permission("Bank Account", "read", doc=settings.bank_account)
		and frappe.has_permission("Company", "read", doc=settings.company)
	):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	year = int(year)
	month = int(month) if month else None
	if month is not None and not 1 <= month <= 12:
		frappe.throw(_("Invalid month {0}").format(month))
	frappe.local.response.filename = statement_filename(year, month)
	frappe.local.response.filecontent = render_statement_pdf(year, month)
	frappe.local.response.type = "pdf"


# --------------------------------------------------------------------------
# setup


def setup_stripe(
	company: str,
	parent_account: str,
	account_name: str = "Stripe",
	account_number: str | None = None,
	currency: str | None = None,
	bank: str = "Stripe",
	payout_bank_account: str | None = None,
	income_account: str | None = None,
	fee_account: str | None = None,
	other_account: str | None = None,
	opening_account: str | None = None,
) -> dict[str, Any]:
	"""Idempotent: ledger account, Bank Account and settings for one Stripe balance.

	Only fills settings fields that are still empty; never enables the sync and
	never stores the API key (set it from the form).
	"""
	filters = {"company": company, "account_name": account_name, "is_group": 0}
	if account_number:
		filters["account_number"] = account_number
	stripe_account = frappe.db.get_value("Account", filters)
	if not stripe_account:
		doc = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"account_number": account_number,
				"parent_account": parent_account,
				"company": company,
				"account_type": "Bank",
				"account_currency": currency
				or frappe.get_cached_value("Company", company, "default_currency"),
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		stripe_account = doc.name
	if not frappe.db.exists("Bank", bank):
		frappe.get_doc({"doctype": "Bank", "bank_name": bank}).insert(ignore_permissions=True)
	bank_account = frappe.db.get_value("Bank Account", {"account": stripe_account, "company": company})
	if not bank_account:
		doc = frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": account_name,
				"bank": bank,
				"is_company_account": 1,
				"company": company,
				"account": stripe_account,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		bank_account = doc.name
	values = {
		"company": company,
		"bank_account": bank_account,
		"payout_bank_account": payout_bank_account,
		"income_account": income_account,
		"fee_account": fee_account,
		"other_account": other_account,
		"clearing_account": opening_account,
		"currency": frappe.get_cached_value("Account", stripe_account, "account_currency"),
	}
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	for fieldname, value in values.items():
		if value and not settings.get(fieldname):
			settings.set(fieldname, value)
	settings.flags.ignore_permissions = True
	settings.flags.ignore_mandatory = True
	settings.save()
	commit()
	return {fieldname: settings.get(fieldname) for fieldname in values} | {"stripe_account": stripe_account}
