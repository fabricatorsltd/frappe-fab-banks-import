import unittest
from unittest.mock import patch

from fab_banks_import import install


class TestInstall(unittest.TestCase):
	def test_bank_custom_fields_include_abi_code(self):
		with patch.object(install, "_", side_effect=lambda text: text):
			custom_fields = install.get_custom_fields()

		self.assertEqual(set(custom_fields), {"Bank", "Bank Transaction"})
		self.assertGreaterEqual(
			{field["fieldname"] for field in custom_fields["Bank"]},
			{"fab_abi_code"},
		)

	def test_bank_transaction_custom_fields_carry_stripe_details(self):
		with patch.object(install, "_", side_effect=lambda text: text):
			custom_fields = install.get_custom_fields()

		self.assertEqual(
			[field["fieldname"] for field in custom_fields["Bank Transaction"]],
			["fab_liberapay_transfer_id", "fab_stripe_gross", "fab_stripe_fee"],
		)
