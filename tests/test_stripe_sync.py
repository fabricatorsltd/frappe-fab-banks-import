"""End to end sync against a mocked Stripe API.

Needs a dedicated test site with ERPNext: it creates a company with its own
chart of accounts and rolls everything back afterwards.
"""

from __future__ import annotations

import copy
import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import frappe

from fab_banks_import import stripe_import

FIXTURES = Path(__file__).parent / "fixtures" / "stripe_balance_transactions.json"
COMPANY = "Stripe Sync Test Company"
ABBR = "SSTC"


class FakeStripe:
	def __init__(self, rows, balance_cents):
		self.rows = rows
		self.balance_cents = balance_cents
		self.calls = []

	def __call__(self, api_key, path, params=None):
		self.calls.append(path)
		if path == "balance":
			return {"available": [{"amount": self.balance_cents, "currency": "eur"}], "pending": []}
		if path == "balance_transactions":
			created_from = int(dict(params)["created[gte]"])
			data = [copy.deepcopy(r) for r in self.rows if r["created"] >= created_from]
			return {"data": data, "has_more": False}
		if path.startswith("payouts/"):
			payout_id = path.split("/", 1)[1]
			return next(r["source"] for r in self.rows if (r.get("source") or {}).get("id") == payout_id)
		if path.startswith("charges/"):
			return {"metadata": {}}
		raise AssertionError(f"unexpected Stripe call {path}")


def make_account(name, parent, account_type="", root_type=None, is_group=0):
	existing = frappe.db.get_value("Account", {"account_name": name, "company": COMPANY})
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": name,
			"parent_account": parent,
			"company": COMPANY,
			"account_type": account_type,
			"account_currency": "EUR",
			"is_group": is_group,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def group_of(root_type):
	return frappe.db.get_value(
		"Account",
		{"company": COMPANY, "root_type": root_type, "is_group": 1, "parent_account": ["is", "set"]},
	)


class TestSyncEndToEnd(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		# the test rolls back to its own savepoint instead
		cls.commit_patch = patch.multiple(stripe_import, commit=lambda: None, rollback=lambda: None)
		cls.commit_patch.start()
		frappe.flags.mute_emails = True
		for year in (2024, 2025, 2026):
			if not frappe.db.exists("Fiscal Year", str(year)):
				frappe.get_doc(
					{
						"doctype": "Fiscal Year",
						"year": str(year),
						"year_start_date": date(year, 1, 1),
						"year_end_date": date(year, 12, 31),
					}
				).insert(ignore_permissions=True)
		if not frappe.db.exists("Warehouse Type", "Transit"):
			# normally created by the setup wizard, needed by Company.on_update
			frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Company", COMPANY):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": COMPANY,
					"abbr": ABBR,
					"default_currency": "EUR",
					"country": "Italy",
					"create_chart_of_accounts_based_on": "Standard Template",
					"chart_of_accounts": "Standard",
				}
			).insert(ignore_permissions=True)
		bank_group = frappe.db.get_value(
			"Account", {"company": COMPANY, "account_type": "Bank", "is_group": 1}
		) or group_of("Asset")
		cls.accounts = {
			"income": make_account("Shop Sales", group_of("Income")),
			"default_income": make_account("Other Sales", group_of("Income")),
			"fees": make_account("Processor Fees", group_of("Expense")),
			"other": make_account("Processor Other", group_of("Expense")),
			"counter": make_account("Opening Counter", group_of("Equity") or group_of("Liability")),
			"bank": make_account("Main Bank", bank_group, account_type="Bank"),
		}
		if not frappe.db.exists("Bank", "Test Bank"):
			frappe.get_doc({"doctype": "Bank", "bank_name": "Test Bank"}).insert(ignore_permissions=True)
		cls.payout_bank = frappe.db.get_value("Bank Account", {"account": cls.accounts["bank"]})
		if not cls.payout_bank:
			cls.payout_bank = (
				frappe.get_doc(
					{
						"doctype": "Bank Account",
						"account_name": "Main",
						"bank": "Test Bank",
						"is_company_account": 1,
						"company": COMPANY,
						"account": cls.accounts["bank"],
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		settings = frappe.get_single(stripe_import.SETTINGS_DOCTYPE)
		for fieldname in (
			"company",
			"bank_account",
			"payout_bank_account",
			"payout_description_contains",
			"income_account",
			"fee_account",
			"other_account",
			"clearing_account",
			"default_cost_center",
			"currency",
		):
			settings.set(fieldname, None)
		settings.set("rules", [])
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)
		stripe_import.setup_stripe(
			COMPANY,
			parent_account=bank_group,
			payout_bank_account=cls.payout_bank,
			income_account=cls.accounts["default_income"],
			fee_account=cls.accounts["fees"],
			other_account=cls.accounts["other"],
			opening_account=cls.accounts["counter"],
		)
		settings = frappe.get_single(stripe_import.SETTINGS_DOCTYPE)
		settings.start_date = date(2026, 1, 1)
		settings.api_key = "rk_test_fake"
		settings.opening_balance = 0
		settings.opening_journal_entry = None
		settings.append(
			"rules",
			{
				"priority": 10,
				"label": "Shop order",
				"metadata_key": "order_id",
				"income_account": cls.accounts["income"],
				"reference_key": "order_id",
				"refund_treatment": "Fee",
			},
		)
		settings.save(ignore_permissions=True)
		cls.stripe_account = frappe.db.get_value("Bank Account", settings.bank_account, "account")
		cls.stripe_bank = settings.bank_account
		deposit = frappe.get_doc(
			{
				"doctype": "Bank Transaction",
				"date": date(2026, 3, 18),
				"bank_account": cls.payout_bank,
				"company": COMPANY,
				"currency": "EUR",
				"deposit": 0.15,
				"description": "TRANSFER FROM STRIPE",
				"transaction_id": "bank-deposit-1",
			}
		)
		deposit.insert(ignore_permissions=True)
		deposit.submit()
		cls.deposit = deposit.name

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		cls.commit_patch.stop()

	def setUp(self):
		frappe.db.savepoint("stripe_sync_test")
		self.rows = json.loads(FIXTURES.read_text())

	def tearDown(self):
		frappe.db.rollback(save_point="stripe_sync_test")

	def run_sync(self, rows=None, balance_cents=10000):
		fake = FakeStripe(rows or self.rows, balance_cents)
		with patch.object(stripe_import, "stripe_get", fake):
			return stripe_import.sync(), fake

	def ledger(self, account):
		return frappe.db.sql(
			"select coalesce(sum(debit - credit), 0) from `tabGL Entry` where account = %s and is_cancelled = 0",
			account,
		)[0][0]

	def counts(self):
		return (
			frappe.db.count("Bank Transaction", {"bank_account": self.stripe_bank, "docstatus": 1}),
			frappe.db.count("Journal Entry", {"company": COMPANY, "docstatus": 1}),
			frappe.db.count("Payment Entry", {"company": COMPANY, "docstatus": 1}),
		)

	def test_first_sync_books_everything_and_second_is_a_no_op(self):
		result, _fake = self.run_sync()

		self.assertEqual(result["transactions_created"], 8)
		self.assertEqual(result["opening"]["balance"], 100.0)
		self.assertEqual(result["unclassified"], 3)
		self.assertEqual(len(result["months"]), 1)
		month = result["months"][0]
		self.assertEqual(month["month"], "2026-03")
		self.assertEqual(month["reconciled"], 7)
		self.assertEqual(month["lines"][f"{self.accounts['income']} / Main - {ABBR}"], -12.55)
		self.assertEqual(month["lines"][f"{self.accounts['fees']} / Main - {ABBR}"], 16.9)
		payout = result["payouts"][0]
		self.assertTrue(payout["created"])
		self.assertTrue(payout["stripe_reconciled"])
		self.assertEqual((payout["bank_matched"], payout["bank_transaction"]), ("matched", self.deposit))
		self.assertEqual(self.ledger(self.stripe_account), 100.0)
		self.assertEqual(self.ledger(self.accounts["bank"]), 0.15)
		self.assertEqual(
			frappe.db.count("Bank Transaction", {"bank_account": self.stripe_bank, "status": "Unreconciled"}),
			0,
		)
		before = self.counts()

		again, fake = self.run_sync()

		self.assertEqual(self.counts(), before)
		self.assertEqual((again["transactions_created"], again["transactions_classified"]), (0, 0))
		self.assertFalse(any(m["created"] for m in again["months"]))
		self.assertFalse(again["payouts"][0]["created"])
		self.assertEqual(self.ledger(self.stripe_account), 100.0)
		self.assertNotIn("payouts/po_payout_a", fake.calls)

	def test_balance_mismatch_fails_loudly(self):
		self.run_sync()

		with self.assertRaises(frappe.ValidationError):
			self.run_sync(balance_cents=10001)

	def test_failed_payout_reverses_its_transfer(self):
		self.run_sync()
		payout = self.rows[-1]
		failed_source = payout["source"] | {"status": "failed"}
		failure = {
			"id": "txn_payout_failure",
			"type": "payout_failure",
			"currency": "eur",
			"amount": 15,
			"fee": 0,
			"net": 15,
			"created": payout["created"] + 86400,
			"reporting_category": "payout_reversal",
			"description": "",
			"source": failed_source,
		}
		rows = [*self.rows[:-1], payout | {"source": failed_source}, failure]

		result, _fake = self.run_sync(rows=rows, balance_cents=10015)

		reversed_entry = next(p["reversed"] for p in result["payouts"] if p.get("reversed"))
		self.assertEqual(frappe.db.get_value("Payment Entry", reversed_entry, "docstatus"), 2)
		self.assertEqual(self.ledger(self.accounts["bank"]), 0.0)
		self.assertEqual(self.ledger(self.stripe_account), 100.15)
		self.assertEqual(frappe.db.get_value("Bank Transaction", self.deposit, "status"), "Unreconciled")

	def test_rows_imported_before_classification_are_filled_in_place(self):
		self.run_sync()
		names = frappe.get_all("Bank Transaction", filters={"bank_account": self.stripe_bank}, pluck="name")
		amounts = {
			n: frappe.db.get_value("Bank Transaction", n, ["deposit", "withdrawal", "status"]) for n in names
		}
		for name in names:
			frappe.db.set_value(
				"Bank Transaction",
				name,
				{
					"fab_stripe_kind": None,
					"fab_stripe_account": None,
					"fab_stripe_category": None,
					"fab_stripe_cost_center": None,
				},
				update_modified=False,
			)
		before = self.counts()

		result, _fake = self.run_sync()

		self.assertEqual(result["transactions_classified"], 8)
		self.assertEqual(self.counts(), before)
		for name in names:
			self.assertEqual(
				frappe.db.get_value("Bank Transaction", name, ["deposit", "withdrawal", "status"]),
				amounts[name],
			)
		self.assertEqual(
			frappe.db.count(
				"Bank Transaction", {"bank_account": self.stripe_bank, "fab_stripe_kind": ["is", "not set"]}
			),
			0,
		)
		self.assertEqual(self.ledger(self.stripe_account), 100.0)

	def test_manual_entry_on_the_stripe_account_is_flagged_not_fatal(self):
		self.run_sync()
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": COMPANY,
				"posting_date": date(2026, 4, 2),
				"accounts": [
					{"account": self.stripe_account, "debit_in_account_currency": 1},
					{"account": self.accounts["counter"], "credit_in_account_currency": 1},
				],
			}
		)
		je.insert(ignore_permissions=True)
		je.submit()

		result, _fake = self.run_sync()

		self.assertTrue(any("101" in str(f) for f in result["flagged"]))

	def test_lock_is_reentrant(self):
		with stripe_import.sync_lock():
			with stripe_import.sync_lock():
				self.assertTrue(frappe.local.stripe_sync_lock)
			self.assertTrue(frappe.local.stripe_sync_lock)
		self.assertFalse(frappe.local.stripe_sync_lock)
		self.assertEqual(frappe.db.sql("select is_free_lock(%s)", stripe_import.SYNC_LOCK)[0][0], 1)

	def test_statement_pdf_renders(self):
		self.run_sync()

		pdf = stripe_import.render_statement_pdf(2026, 3)

		self.assertTrue(pdf.startswith(b"%PDF"))


class TestRenamePatch(unittest.TestCase):
	"""Adds and drops a column, so it commits: it only touches its own row."""

	def test_copies_values_and_removes_the_old_field(self):
		from frappe.custom.doctype.custom_field.custom_field import create_custom_field

		from fab_banks_import.patches import rename_stripe_reference_field as patch_module

		old = patch_module.OLD_FIELD
		create_custom_field(
			"Bank Transaction", {"fieldname": old, "label": "Old Reference", "fieldtype": "Data"}
		)
		frappe.db.sql(
			f"insert into `tabBank Transaction` (name, docstatus, `{old}`) values ('patch-test-row', 0, 'REF-1')"
		)
		try:
			patch_module.execute()
			patch_module.execute()

			self.assertFalse(frappe.db.has_column("Bank Transaction", old))
			self.assertFalse(frappe.db.exists("Custom Field", f"Bank Transaction-{old}"))
			self.assertEqual(
				frappe.db.get_value("Bank Transaction", "patch-test-row", "fab_stripe_reference"), "REF-1"
			)
		finally:
			frappe.db.sql("delete from `tabBank Transaction` where name = 'patch-test-row'")
			frappe.db.commit()
