# Synthetic demo

The demo exercises topic selection, two-article packaging, fact cards, deterministic drafting, review, cover selection, manifest freezing and publish-policy evaluation without network access.

## Run

```bash
python scripts/run_synthetic_demo.py --output-dir demo-output
```

For a deterministic date:

```bash
python scripts/run_synthetic_demo.py --output-dir demo-output-ci --date 2026-08-08
```

## Expected properties

- Result status is `ok`.
- `published` is `false`.
- Publishing mode is inherited as `dry_run`.
- The primary article has at least five items.
- The manufacturing article has at least three items.
- External model roles report disabled or unconfigured.
- Covers use the bundled deterministic templates.
- `content-package.manifest.json` verifies the package hashes.

The demo writes only beneath the chosen output directory. `demo-output/` is ignored by Git.

For a screenshot-friendly local wrapper that hides unresolved WeChat image placeholders:

```bash
python scripts/prepare_demo_preview.py --input demo-output/run/article.html --output demo-output/preview.html
```

## Data contract

Every company, publication, event and URL under `examples/synthetic/` is fictional. Reserved `.invalid` domains prevent accidental traffic. The script rewrites dates at runtime so previous-day logic remains realistic without turning the fixture into a historical news claim.
