from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import frappe

from fab_banks_import import stripe_import

FIXTURES = Path(__file__).parent / "fixtures" / "stripe_balance_transactions.json"
ROME = ZoneInfo("Europe/Rome")


def load_movements():
	raw = json.loads(FIXTURES.read_text())
	movements = [stripe_import.parse_balance_transaction(row, ROME) for row in raw]
	stripe_import.link_reversals(movements)
	return movements


class TestClassification(unittest.TestCase):
	def test_types_map_to_categories(self):
		categories = {m.id: m.category for m in load_movements()}

		self.assertEqual(
			categories,
			{
				"txn_donation_a": "donation",
				"txn_donation_b": "donation",
				"txn_reversal_a": "commission",
				"txn_stripe_fee": "stripe_fee",
				"txn_payout": "payout",
			},
		)

	def test_refund_and_adjustment_are_commissions(self):
		for kind in ("payment_refund", "refund", "adjustment"):
			self.assertEqual(stripe_import.CATEGORY_BY_TYPE[kind], "commission")

	def test_amounts_and_liberapay_ids(self):
		by_id = {m.id: m for m in load_movements()}

		self.assertEqual((by_id["txn_donation_b"].amount, by_id["txn_donation_b"].fee), (10.0, 0.45))
		self.assertEqual(by_id["txn_donation_b"].net, 9.55)
		self.assertEqual(by_id["txn_donation_a"].liberapay_transfer_id, "100001")
		# the reversal inherits the transfer id of the donation it reverses
		self.assertEqual(by_id["txn_reversal_a"].liberapay_transfer_id, "100001")
		self.assertEqual(by_id["txn_payout"].payout_arrival_date, date(2026, 3, 18))
		self.assertEqual(by_id["txn_payout"].source_id, "po_payout_a")

	def test_description_never_carries_stripe_free_text(self):
		raw = json.loads(FIXTURES.read_text())[0] | {"description": "donation from Jane Doe jane@example.com"}
		movement = stripe_import.parse_balance_transaction(raw, ROME)

		text = stripe_import.bank_transaction_description(movement)

		self.assertNotIn("Jane", text)
		self.assertNotIn("@", text)
		self.assertIn("Liberapay transfer 100001", text)

	def test_unknown_type_fails_loudly(self):
		raw = json.loads(FIXTURES.read_text())[0] | {"type": "payout_failure"}

		with self.assertRaises(ValueError):
			stripe_import.parse_balance_transaction(raw, ROME)

	def test_non_eur_fails_loudly(self):
		raw = json.loads(FIXTURES.read_text())[0] | {"currency": "usd"}

		with self.assertRaises(ValueError):
			stripe_import.parse_balance_transaction(raw, ROME)

	def test_inconsistent_net_fails_loudly(self):
		raw = json.loads(FIXTURES.read_text())[1] | {"net": 1000}

		with self.assertRaises(ValueError):
			stripe_import.parse_balance_transaction(raw, ROME)


class TestMonthlyAggregation(unittest.TestCase):
	def test_month_totals_split_income_and_charges(self):
		totals = stripe_import.month_totals(load_movements())

		self.assertEqual(totals.count, 4)
		self.assertEqual(totals.donations, 22.55)
		self.assertEqual(totals.donation_fees, 0.45)
		self.assertEqual(totals.commissions, 1.38)
		self.assertEqual(totals.stripe_fees, 0.07)
		self.assertEqual(totals.charges, 1.9)
		self.assertEqual((totals.deposits, totals.withdrawals), (22.1, 1.45))
		self.assertEqual(totals.net, 20.65)
		self.assertTrue(totals.is_balanced())

	def test_payouts_are_excluded(self):
		payouts = [m for m in load_movements() if m.category == "payout"]

		totals = stripe_import.month_totals(payouts)

		self.assertEqual((totals.count, totals.net), (0, 0))

	def test_statement_rows_running_balance(self):
		rows, totals = stripe_import.statement_rows(100.0, load_movements())

		self.assertEqual([r["balance"] for r in rows], [112.55, 122.1, 120.72, 120.65, 100.0])
		self.assertEqual(totals["donations"], 22.55)
		self.assertEqual(totals["charges"], 1.9)
		self.assertEqual(totals["payouts"], 20.65)
		self.assertEqual((totals["opening"], totals["closing"]), (100.0, 100.0))


class TestOpeningBalance(unittest.TestCase):
	def test_opening_walks_back_from_current_balance(self):
		movements = load_movements()

		opening = stripe_import.compute_opening_balance(25.0, movements)

		self.assertEqual(opening, 25.0)
		stripe_import.check_balance_invariant(opening, movements, 25.0)

	def test_opening_without_payout(self):
		movements = [m for m in load_movements() if m.category != "payout"]

		self.assertEqual(stripe_import.compute_opening_balance(30.65, movements), 10.0)

	def test_invariant_mismatch_fails_loudly(self):
		with self.assertRaises(ValueError):
			stripe_import.check_balance_invariant(10.0, load_movements(), 10.01)


class TestPayoutMatching(unittest.TestCase):
	arrival = date(2026, 3, 18)

	def row(self, name, day, amount=95.69, description="BE Stripe Technology Europe Ltd", **extra):
		return {
			"name": name,
			"date": date(2026, 3, day),
			"deposit": amount,
			"description": description,
			"unallocated_amount": amount,
			"status": "Unreconciled",
		} | extra

	def test_picks_closest_stripe_deposit(self):
		rows = [self.row("far", 22), self.row("near", 19)]

		self.assertEqual(stripe_import.pick_payout_match(rows, 95.69, self.arrival)["name"], "near")

	def test_description_match_is_case_insensitive(self):
		rows = [self.row("upper", 18, description="BONIFICO DALL'ESTERO STRIPE TECHNOLOGY EUROPE LTD")]

		self.assertEqual(stripe_import.pick_payout_match(rows, 95.69, self.arrival)["name"], "upper")

	def test_rejects_wrong_amount_description_window_or_reconciled(self):
		rows = [
			self.row("amount", 18, amount=95.68),
			self.row("description", 18, description="BONIFICO ACME SRL"),
			self.row("window", 24),
			self.row("reconciled", 18, unallocated_amount=0, status="Reconciled"),
		]

		self.assertIsNone(stripe_import.pick_payout_match(rows, 95.69, self.arrival))

	def test_window_edges(self):
		self.assertIsNotNone(stripe_import.pick_payout_match([self.row("edge", 23)], 95.69, self.arrival))
		self.assertIsNotNone(stripe_import.pick_payout_match([self.row("edge", 13)], 95.69, self.arrival))

	def test_tie_returns_nothing(self):
		rows = [self.row("before", 17), self.row("after", 19)]

		self.assertIsNone(stripe_import.pick_payout_match(rows, 95.69, self.arrival))


class TestStatementSchedule(unittest.TestCase):
	def test_send_dates_2026(self):
		expected = {
			1: date(2026, 1, 5),  # Saturday 3rd -> Monday
			2: date(2026, 2, 3),
			3: date(2026, 3, 3),
			4: date(2026, 4, 3),
			5: date(2026, 5, 4),  # Sunday 3rd -> Monday
			6: date(2026, 6, 3),
			7: date(2026, 7, 3),
			8: date(2026, 8, 3),
			9: date(2026, 9, 3),
			10: date(2026, 10, 5),  # Saturday 3rd -> Monday
			11: date(2026, 11, 3),
			12: date(2026, 12, 3),
		}

		for month, send_date in expected.items():
			with self.subTest(month=month):
				computed = stripe_import.statement_send_date(2026, month)
				self.assertEqual(computed, send_date)
				self.assertLess(computed.weekday(), 5)
				if date(2026, month, 3).weekday() >= 5:
					self.assertEqual(computed.weekday(), 0)

	def settings(self, **values):
		defaults = {"statement_email_enabled": 1, "last_statement_sent": "", "start_date": date(2025, 1, 1)}
		return SimpleNamespace(**(defaults | values))

	def test_due_from_send_day_until_marked_sent(self):
		settings = self.settings()

		self.assertIsNone(stripe_import.statement_due(settings, date(2026, 10, 4)))
		self.assertEqual(stripe_import.statement_due(settings, date(2026, 10, 5)), (2026, 9))
		self.assertEqual(stripe_import.statement_due(settings, date(2026, 10, 9)), (2026, 9))
		self.assertIsNone(
			stripe_import.statement_due(self.settings(last_statement_sent="2026-09"), date(2026, 10, 5))
		)

	def test_january_sends_december(self):
		self.assertEqual(stripe_import.statement_due(self.settings(), date(2027, 1, 4)), (2026, 12))

	def test_month_before_start_date_never_due(self):
		settings = self.settings(start_date=date(2026, 11, 1))

		self.assertIsNone(stripe_import.statement_due(settings, date(2026, 11, 5)))

	def test_disabled_never_due(self):
		self.assertIsNone(
			stripe_import.statement_due(self.settings(statement_email_enabled=0), date(2026, 11, 3))
		)


FAKE_TOTALS = {
	"opening": 10.0,
	"donations": 22.55,
	"charges": 1.9,
	"payouts": 20.65,
	"net": 0.0,
	"closing": 10.0,
}


class TestStatementEmail(unittest.TestCase):
	"""Runs on a dedicated test site: needs the database for the Email Queue."""

	def setUp(self):
		frappe.flags.mute_emails = True
		self.settings = frappe.get_single(stripe_import.SETTINGS_DOCTYPE)
		self.settings.set("statement_recipients", [])
		self.settings.append("statement_recipients", {"email": "it.reader@example.com", "language": "it"})
		self.settings.append("statement_recipients", {"email": "en.reader@example.com", "language": "en"})
		self.settings.last_statement_sent = ""
		self.settings.flags.ignore_mandatory = True
		self.settings.save(ignore_permissions=True)
		frappe.db.delete("Email Queue", {"reference_doctype": stripe_import.SETTINGS_DOCTYPE})

	def tearDown(self):
		frappe.db.delete("Email Queue", {"reference_doctype": stripe_import.SETTINGS_DOCTYPE})
		frappe.db.rollback()

	def send(self, force=False):
		with (
			patch.object(stripe_import, "statement_context", return_value={"totals": FAKE_TOTALS}),
			patch.object(stripe_import, "render_statement_html", return_value="<html></html>"),
			patch.object(stripe_import, "render_statement_pdf", return_value=b"%PDF-1.7 fake"),
		):
			return stripe_import.send_statement(2026, 10, settings=self.settings, force=force)

	def queued(self):
		return frappe.get_all(
			"Email Queue",
			filters={"reference_doctype": stripe_import.SETTINGS_DOCTYPE},
			fields=["name", "message"],
		)

	def test_one_mail_per_language(self):
		result = self.send()

		self.assertTrue(result["sent"])
		queue = self.queued()
		self.assertEqual(len(queue), 2)
		by_recipient = {}
		for row in queue:
			recipients = frappe.get_all(
				"Email Queue Recipient", filters={"parent": row.name}, pluck="recipient"
			)
			self.assertEqual(len(recipients), 1)
			by_recipient[recipients[0]] = row
			# inline PDF attachments travel inside the MIME message
			self.assertEqual(row.message.count("Content-Type: application/pdf"), 1)
		self.assertIn("Subject: Estratto conto Stripe 10/2026", by_recipient["it.reader@example.com"].message)
		self.assertIn("Saldo iniziale", by_recipient["it.reader@example.com"].message)
		self.assertIn("estratto-conto-stripe-2026-10.pdf", by_recipient["it.reader@example.com"].message)
		self.assertIn("Subject: Stripe statement 10/2026", by_recipient["en.reader@example.com"].message)
		self.assertIn("Opening balance", by_recipient["en.reader@example.com"].message)
		self.assertIn("stripe-statement-2026-10.pdf", by_recipient["en.reader@example.com"].message)
		self.assertEqual(
			frappe.db.get_single_value(stripe_import.SETTINGS_DOCTYPE, "last_statement_sent"), "2026-10"
		)

	def test_guard_blocks_second_send_unless_forced(self):
		self.send()
		self.settings.reload()

		self.assertFalse(self.send()["sent"])
		self.assertEqual(len(self.queued()), 2)
		self.assertTrue(self.send(force=True)["sent"])
		self.assertEqual(len(self.queued()), 4)

	def test_user_language_wins_over_row_language(self):
		if not frappe.db.exists("User", "stripe.user@example.com"):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": "stripe.user@example.com",
					"first_name": "Stripe",
					"language": "it",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		self.settings.set("statement_recipients", [])
		self.settings.append("statement_recipients", {"email": "stripe.user@example.com", "language": "en"})

		self.assertEqual(
			stripe_import.recipients_by_language(self.settings), {"it": ["stripe.user@example.com"]}
		)
