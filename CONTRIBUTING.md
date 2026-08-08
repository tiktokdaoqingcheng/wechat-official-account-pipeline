# Contributing

Thank you for helping make WeChat publishing automation safer and easier to audit.

## Before opening a change

- Search existing issues and keep each change focused.
- Use synthetic fixtures or material you have the right to redistribute.
- Never include account identifiers, credentials, production URLs, publish receipts, customer data or private incident logs.
- Do not enable external calls or WeChat writes in default configuration.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock -r requirements-dev.lock
```

Run the checks:

```bash
python scripts/check_public_tree.py
python -m compileall -q src scripts
python -m ruff check .
python -m unittest discover -s tests
python scripts/run_synthetic_demo.py --output-dir demo-output-ci --date 2026-08-08
```

## Pull requests

- Explain the user-visible behavior and failure modes.
- Add focused tests proportional to the risk of the change.
- Update documentation when configuration, state or API behavior changes.
- Preserve `dry_run` as the default and keep retries bounded.
- Include provenance for every new image, font, dataset or copied code fragment.
- Confirm that `git status --short --ignored` contains no sensitive untracked file.

By submitting a contribution, you agree that it is licensed under Apache-2.0 and that you have the right to provide it under that license.
