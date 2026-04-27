import unittest
from unittest.mock import patch

from fab_banks_import import install


class TestInstall(unittest.TestCase):
	def test_bank_custom_fields_include_abi_code(self):
		with patch.object(install, "_", side_effect=lambda text: text):
			custom_fields = install.get_custom_fields()

		self.assertEqual(set(custom_fields), {"Bank"})
		self.assertGreaterEqual(
			{field["fieldname"] for field in custom_fields["Bank"]},
			{"fab_abi_code"},
		)
