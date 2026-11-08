# PyPI Publishing Runbook

## Prerequisites

1. Create a PyPI account at https://pypi.org
2. In account settings, create an API token with `project:write` scope for `aletheia`.
3. Store the token securely. Never commit it to source or logs.

## Build

```bash
bash scripts/build-release.sh
```

This produces `dist/aletheia-0.1.0a0-py3-none-any.whl` and verifies install + entrypoint in a fresh venv.

## Upload (twine)

```bash
python3 -m pip install twine
python3 -m twine upload dist/*
```

Enter the API token when prompted.

## Verification

```bash
python3 -m pip install aletheia==0.1.0a0
aletheia --help
```

## Rollback

If a bad release is published, yank the file on PyPI and release a new patch version (`0.1.0a1`).

## Notes

- AGPL-3.0 license is declared in pyproject.toml.
- Wheel is platform-independent (`py3-none-any`).
- Optional extras: `aletheia[server]`, `aletheia[local-embedding]`.
