from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fab_banks_import import bank_directory


class TestBankDirectory(unittest.TestCase):
	def test_parse_abi_cab_file_extracts_bank_master_rows(self):
		content = "\n".join(
			[
				"1103069     2INTESA SANPAOLO SPA                                                             0",
				"21030690100090100090PIAZZA SAN CARLO, 156",
				"310306901000TORINO - PIAZZA SAN CARLO               110101000",
			]
		)

		rows = bank_directory.parse_abi_cab_file(content)

		self.assertEqual(
			rows,
			{
				"03069": {
					"abi_code": "03069",
					"bank_name": "INTESA SANPAOLO SPA",
				}
			},
		)

	def test_get_bank_record_from_abi_returns_bank_metadata(self):
		with patch.object(
			bank_directory,
			"frappe",
			new=SimpleNamespace(
				db=SimpleNamespace(
					get_value=Mock(
						return_value={
							"name": "INTESA SANPAOLO SPA",
							"bank_name": "INTESA SANPAOLO SPA",
							"fab_abi_code": "03069",
						}
					)
				)
			),
		):
			bank = bank_directory.get_bank_record_from_abi("03069")

		self.assertEqual(
			bank,
			{
				"name": "INTESA SANPAOLO SPA",
				"bank_name": "INTESA SANPAOLO SPA",
				"fab_abi_code": "03069",
			},
		)

	def test_resolve_bank_name_from_abi_uses_imported_directory(self):
		with patch.object(
			bank_directory,
			"frappe",
			new=SimpleNamespace(
				db=SimpleNamespace(
					get_value=Mock(
						return_value={
							"name": "INTESA SANPAOLO SPA",
							"bank_name": "INTESA SANPAOLO SPA",
							"fab_abi_code": "03069",
						}
					)
				)
			),
		):
			bank_name = bank_directory.resolve_bank_name_from_abi("03069")

		self.assertEqual(bank_name, "INTESA SANPAOLO SPA")

	def test_extract_italian_bank_codes_from_iban(self):
		details = bank_directory.extract_italian_bank_codes_from_iban("IT74 G03069 03293 100000018562")

		self.assertEqual(
			details,
			{
				"iban": "IT74G0306903293100000018562",
				"abi_code": "03069",
				"cab_code": "03293",
			},
		)

	def test_import_abi_cab_file_counts_creates_and_updates(self):
		content = "1103069     2INTESA SANPAOLO SPA                                                             0\n"
		with (
			patch.object(bank_directory.Path, "read_text", return_value=content),
			patch.object(bank_directory, "upsert_bank_by_abi_code", return_value="created") as upsert,
		):
			result = bank_directory.import_abi_cab_file("/tmp/abicab.txt")

		self.assertEqual(result, {"imported": 1, "created": 1, "updated": 0})
		upsert.assert_called_once_with("03069", "INTESA SANPAOLO SPA")

	def test_import_abi_cab_upload_uses_uploaded_file_content(self):
		frappe_stub = SimpleNamespace(
			local=SimpleNamespace(
				uploaded_file=b"1103069     2INTESA SANPAOLO SPA                                                             0\n",
				uploaded_file_url=None,
				uploaded_filename="abicab.txt",
			)
		)

		with (
			patch.object(bank_directory, "frappe", new=frappe_stub),
			patch.object(bank_directory, "import_abi_cab_content", return_value={"imported": 1, "created": 1, "updated": 0}) as importer,
		):
			result = bank_directory.import_abi_cab_upload()

		importer.assert_called_once_with(
			"1103069     2INTESA SANPAOLO SPA                                                             0\n"
		)
		self.assertEqual(
			result,
			{
				"imported": 1,
				"created": 1,
				"updated": 0,
				"file_name": "abicab.txt",
			},
		)

	def test_resolve_bank_from_iban_returns_matching_bank(self):
		with patch.object(
			bank_directory,
			"get_bank_record_from_abi",
			return_value={
				"name": "INTESA SANPAOLO SPA",
				"bank_name": "INTESA SANPAOLO SPA",
				"fab_abi_code": "03069",
			},
		):
			result = bank_directory.resolve_bank_from_iban("IT74 G03069 03293 100000018562")

		self.assertEqual(
			result,
			{
				"iban": "IT74G0306903293100000018562",
				"abi_code": "03069",
				"cab_code": "03293",
				"bank": "INTESA SANPAOLO SPA",
				"bank_name": "INTESA SANPAOLO SPA",
			},
		)
