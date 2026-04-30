# fab Banks Import

Standalone ABI/CAB directory import for Frappe and ERPNext.

## Scope

`fab_banks_import` maintains Italian bank directory data so other apps can
resolve a bank from IBAN-derived ABI codes without embedding their own lookup
tables.

Current responsibilities include:

- importing public ABI/CAB source files into ERPNext `Bank` records
- storing ABI metadata used by downstream fab apps
- exposing operator-facing import actions in Desk
- supporting post-import bank relinking from IBAN data

## Branches

- `develop`: integration branch for testing against Frappe/ERPNext `develop`
- `version-16`: stable branch for Frappe/ERPNext 16

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/fabricatorsltd/frappe-fab-banks-import.git --branch version-16
bench --site [site] install-app fab_banks_import
```

## Usage

Import a directory file from the CLI:

```bash
bench --site [site] execute fab_banks_import.bank_directory.import_abi_cab_file --kwargs "{'file_path': '/absolute/path/to/abicab.txt'}"
```

Or use **Bank -> Menu -> Import ABI/CAB Directory** in Desk.

## Contributing

Follow the official Frappe contribution guidelines:

- <https://github.com/frappe/erpnext/wiki/Contribution-Guidelines>

Use the upstream guidance for proposals, coding standards, pull requests, and
documentation updates when contributing to this app.

## Development

```bash
cd apps/fab_banks_import
pre-commit install
```

Pre-commit is configured for Ruff, ESLint, Prettier, and PyUpgrade.

## License

GNU Affero General Public License v3.0
