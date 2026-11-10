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
DEFAULTS = stripe_import.Defaults(
	income_account="Default Income - T",
	fee_account="Fees - T",
	other_account="Other - T",
	cost_center="Main - T",
)
ORDER_RULE = {
	"priority": 10,
	"label": "Shop order",
	"metadata_key": "order_id",
	"income_account": "Sales - T",
	"cost_center": "Shop - T",
	"reference_key": "order_id",
	"refund_treatment": "Fee",
}


def raw_rows():
	return json.loads(FIXTURES.read_text())


def load_movements(rules=None, charges=None):
	movements = [stripe_import.parse_balance_transaction(row, ROME) for row in raw_rows()]
	stripe_import.classify(movements, [ORDER_RULE] if rules is None else rules, DEFAULTS, charges)
	return movements


def by_id(movements):
	return {m.id: m for m in movements}


class TestParsing(unittest.TestCase):
	def test_kinds(self):
		kinds = {m.id: m.kind for m in load_movements()}

		self.assertEqual(
			kinds,
			{
				"txn_charge_a": "charge",
				"txn_charge_b": "charge",
				"txn_charge_c": "charge",
				"txn_refund_a": "refund",
				"txn_dispute_b": "dispute",
				"txn_stripe_fee": "fee",
				"txn_other": "other",
				"txn_payout": "payout",
			},
		)

	def test_amounts_and_payout(self):
		movements = by_id(load_movements())

		self.assertEqual((movements["txn_charge_b"].amount, movements["txn_charge_b"].fee), (10.0, 0.45))
		self.assertEqual(movements["txn_dispute_b"].net, -25.0)
		self.assertEqual(movements["txn_payout"].payout_arrival_date, date(2026, 3, 18))
		self.assertEqual(movements["txn_payout"].payout_status, "paid")
		self.assertEqual(movements["txn_charge_a"].currency, "EUR")

	def test_inconsistent_net_fails_loudly(self):
		raw = raw_rows()[1] | {"net": 1000}

		with self.assertRaises(ValueError):
			stripe_import.parse_balance_transaction(raw, ROME)

	def test_description_never_carries_customer_email(self):
		movement = by_id(load_movements())["txn_charge_b"]

		text = stripe_import.bank_transaction_description(movement)

		self.assertNotIn("buyer@example.com", text)
		self.assertIn("Shop order", text)
		self.assertIn("B-2", text)


class TestRules(unittest.TestCase):
	def test_rule_sets_category_account_cost_center_and_reference(self):
		charge = by_id(load_movements())["txn_charge_a"]

		self.assertTrue(charge.matched)
		self.assertEqual(
			(charge.category, charge.account, charge.cost_center, charge.reference),
			("Shop order", "Sales - T", "Shop - T", "A-1"),
		)

	def test_no_rule_falls_back_to_defaults(self):
		charge = by_id(load_movements())["txn_charge_c"]

		self.assertFalse(charge.matched)
		self.assertEqual(
			(charge.category, charge.account, charge.cost_center, charge.reference),
			("", "Default Income - T", "Main - T", "Gift card"),
		)

	def test_priority_order_wins(self):
		vip = {"priority": 5, "label": "VIP", "customer": "BUYER@example.com", "income_account": "VIP - T"}

		movements = by_id(load_movements(rules=[ORDER_RULE, vip]))

		self.assertEqual(movements["txn_charge_b"].category, "VIP")
		self.assertEqual(movements["txn_charge_a"].category, "Shop order")

	def test_conditions_combine_with_and(self):
		rule = {
			"label": "Web",
			"metadata_key": "channel",
			"metadata_value": "web",
			"description_contains": "order b",
		}
		wrong_value = rule | {"metadata_value": "store"}
		context = by_id(load_movements())["txn_charge_b"].context

		self.assertTrue(stripe_import.rule_matches(rule, "charge", "charge", context))
		self.assertFalse(stripe_import.rule_matches(wrong_value, "charge", "charge", context))
		self.assertFalse(
			stripe_import.rule_matches(rule | {"description_contains": "gift"}, "charge", "charge", context)
		)

	def test_customer_matches_id_or_email(self):
		context = by_id(load_movements())["txn_charge_b"].context

		self.assertTrue(stripe_import.rule_matches({"customer": "cus_test_1"}, "charge", "charge", context))
		self.assertTrue(
			stripe_import.rule_matches({"customer": "buyer@example.com"}, "charge", "charge", context)
		)
		self.assertFalse(stripe_import.rule_matches({"customer": "cus_other"}, "charge", "charge", context))

	def test_movement_type_matches_type_or_kind(self):
		self.assertTrue(stripe_import.rule_matches({"movement_type": "payment"}, "payment", "charge", {}))
		self.assertTrue(stripe_import.rule_matches({"movement_type": "charge"}, "payment", "charge", {}))
		self.assertFalse(stripe_import.rule_matches({"movement_type": "stripe_fee"}, "payment", "charge", {}))

	def test_fee_and_other_movements(self):
		movements = by_id(load_movements())

		self.assertEqual(movements["txn_stripe_fee"].account, "Fees - T")
		self.assertEqual(movements["txn_other"].account, "Other - T")
		self.assertFalse(movements["txn_other"].matched)


class TestReversalsFollowCharge(unittest.TestCase):
	def test_refund_follows_charge_rule_as_fee(self):
		refund = by_id(load_movements())["txn_refund_a"]

		self.assertTrue(refund.matched)
		self.assertEqual(
			(refund.category, refund.account, refund.cost_center, refund.reference),
			("Shop order", "Fees - T", "Shop - T", "A-1"),
		)

	def test_refund_reverses_income_by_default(self):
		rule = ORDER_RULE | {"refund_treatment": "Reverse Income"}

		refund = by_id(load_movements(rules=[rule]))["txn_refund_a"]

		self.assertEqual(refund.account, "Sales - T")

	def test_refund_of_charge_before_the_window(self):
		raw = raw_rows()[3] | {"source": {"id": "pyr_old", "object": "refund", "charge": "py_old"}}
		movement = stripe_import.parse_balance_transaction(raw, ROME)
		charges = {"py_old": ("charge", {"metadata": {"order_id": "Z-9"}, "description": "Old order"})}

		stripe_import.classify([movement], [ORDER_RULE], DEFAULTS, charges)

		self.assertEqual((movement.category, movement.reference), ("Shop order", "Z-9"))

	def test_refund_without_known_charge_is_unclassified(self):
		raw = raw_rows()[3] | {"source": {"id": "pyr_old", "object": "refund", "charge": "py_unknown"}}
		movement = stripe_import.parse_balance_transaction(raw, ROME)

		stripe_import.classify([movement], [ORDER_RULE], DEFAULTS)

		self.assertFalse(movement.matched)
		self.assertEqual(movement.account, "Default Income - T")

	def test_dispute_reverses_income_and_fee_goes_to_fee_account(self):
		movements = load_movements()
		dispute = by_id(movements)["txn_dispute_b"]

		self.assertEqual(
			(dispute.category, dispute.account, dispute.reference), ("Shop order", "Sales - T", "B-2")
		)
		lines = stripe_import.month_lines([dispute], DEFAULTS.fee_account).lines
		self.assertEqual(lines, {("Sales - T", "Shop - T"): 10.0, ("Fees - T", "Shop - T"): 15.0})


class TestMonthlyAggregation(unittest.TestCase):
	def test_one_line_per_account_and_cost_center(self):
		month = stripe_import.month_lines(load_movements(), DEFAULTS.fee_account)

		self.assertEqual(
			month.lines,
			{
				("Sales - T", "Shop - T"): -12.55,
				("Fees - T", "Shop - T"): 16.83,
				("Default Income - T", "Main - T"): -5.0,
				("Fees - T", "Main - T"): 0.07,
				("Other - T", "Main - T"): 0.5,
			},
		)
		self.assertEqual((month.deposits, month.withdrawals, month.net), (27.1, 26.95, 0.15))
		self.assertEqual(month.count, 7)
		self.assertEqual(month.categories, ["Shop order"])

	def test_payouts_are_excluded(self):
		payouts = [m for m in load_movements() if m.kind == "payout"]

		month = stripe_import.month_lines(payouts, DEFAULTS.fee_account)

		self.assertEqual((month.count, month.net, month.lines), (0, 0, {}))

	def test_statement_totals(self):
		rows, totals = stripe_import.statement_rows(100.0, load_movements(), DEFAULTS.fee_account)

		self.assertEqual(rows[-1]["balance"], 100.0)
		self.assertEqual(totals["categories"], {"Shop order": 22.55, "Charge": 5.0, "Other": -0.5})
		self.assertEqual((totals["fees"], totals["refunds"], totals["payouts"]), (16.9, 10.0, 0.15))
		self.assertEqual((totals["opening"], totals["closing"]), (100.0, 100.0))


class TestEdgeCases(unittest.TestCase):
	def test_payout_failure_offsets_its_payout(self):
		payout = raw_rows()[-1]
		failure = payout | {"id": "txn_payout_failure", "type": "payout_failure", "amount": 15, "net": 15}
		movements = [stripe_import.parse_balance_transaction(row, ROME) for row in (payout, failure)]

		self.assertEqual([m.kind for m in movements], ["payout", "payout"])
		self.assertEqual(stripe_import.month_lines(movements, DEFAULTS.fee_account).count, 0)
		_rows, totals = stripe_import.statement_rows(0.0, movements, DEFAULTS.fee_account)
		self.assertEqual((totals["payouts"], totals["closing"]), (0.0, 0.0))

	def test_refund_failure_nets_against_refunds(self):
		rows = raw_rows()
		failure = rows[3] | {"id": "txn_refund_failure", "type": "refund_failure", "amount": 138, "net": 138}
		movements = [
			stripe_import.parse_balance_transaction(row, ROME) for row in (rows[0], rows[3], failure)
		]
		rule = ORDER_RULE | {"refund_treatment": "Reverse Income"}
		stripe_import.classify(movements, [rule], DEFAULTS)

		_rows, totals = stripe_import.statement_rows(0.0, movements, DEFAULTS.fee_account)

		self.assertEqual(totals["refunds"], 0.0)
		self.assertEqual(totals["categories"], {"Shop order": 12.55})

	def test_unreadable_old_charge_is_flagged_not_fatal(self):
		raw = raw_rows()[3] | {"source": {"id": "pyr_old", "object": "refund", "charge": "py_gone"}}
		movement = stripe_import.parse_balance_transaction(raw, ROME)
		flagged = []

		with patch.object(stripe_import, "stripe_get", side_effect=frappe.ValidationError("404")):
			charges = stripe_import.fetch_missing_charges("key", [movement], {movement.id}, flagged)

		self.assertEqual(charges, {})
		self.assertEqual(len(flagged), 1)

	def test_old_charge_fetched_only_while_unclassified(self):
		raw = raw_rows()[3] | {"source": {"id": "pyr_old", "object": "refund", "charge": "py_old"}}
		movement = stripe_import.parse_balance_transaction(raw, ROME)

		with patch.object(stripe_import, "stripe_get", return_value={"metadata": {"order_id": "Z"}}) as get:
			self.assertEqual(stripe_import.fetch_missing_charges("key", [movement], set(), []), {})
			charges = stripe_import.fetch_missing_charges("key", [movement], {movement.id}, [])

		get.assert_called_once()
		self.assertEqual(charges["py_old"][0], "payment")


class TestOpeningBalance(unittest.TestCase):
	def test_opening_walks_back_from_current_balance(self):
		movements = load_movements()

		opening = stripe_import.compute_opening_balance(25.0, movements)

		self.assertEqual(opening, 25.0)
		stripe_import.check_balance_invariant(opening, movements, 25.0)

	def test_opening_without_payout(self):
		movements = [m for m in load_movements() if m.kind != "payout"]

		self.assertEqual(stripe_import.compute_opening_balance(10.15, movements), 10.0)

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
		rows = [self.row("upper", 18, description="TRANSFER FROM STRIPE TECHNOLOGY EUROPE LTD")]

		self.assertEqual(stripe_import.pick_payout_match(rows, 95.69, self.arrival)["name"], "upper")

	def test_rejects_wrong_amount_description_window_or_reconciled(self):
		rows = [
			self.row("amount", 18, amount=95.68),
			self.row("description", 18, description="TRANSFER FROM ACME LTD"),
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


FAKE_CONTEXT = {
	"currency": "EUR",
	"totals": {
		"opening": 10.0,
		"categories": {"Shop order": 22.55},
		"fees": 1.9,
		"refunds": 0.0,
		"payouts": 20.65,
		"closing": 10.0,
	},
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
			patch.object(stripe_import, "statement_context", return_value=FAKE_CONTEXT),
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
		italian = by_recipient["it.reader@example.com"].message
		english = by_recipient["en.reader@example.com"].message
		self.assertIn("Subject: Estratto conto Stripe 10/2026", italian)
		self.assertIn("Saldo iniziale", italian)
		self.assertIn("estratto-conto-stripe-2026-10.pdf", italian)
		self.assertIn("Subject: Stripe statement 10/2026", english)
		self.assertIn("Opening balance", english)
		self.assertIn("Shop order", english)
		self.assertIn("stripe-statement-2026-10.pdf", english)
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
