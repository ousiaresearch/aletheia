# Release 2.0.1

## Automated gates

- `scripts/test.sh` — 1,061 passed on macOS.
- `scripts/build-release.sh` — wheel build, Twine metadata check, fresh-venv install, and CLI entrypoint checks passed.
- Provider transport tests use mocked HTTP responses; no secret values are part of the fixtures.

## Verified by hand

- Provider selection, endpoint/model routing, API-key environment lookup, egress opt-in, and key redaction were exercised locally.
- The plugin manifest, package metadata, source `__version__`, and changelog version are checked for agreement.

## Deliberately not proven

- No live OpenAI, Anthropic, Alibaba, or other LLM endpoint has been called; exact Alibaba endpoint/model settings remain unverified.
- The Hermes memory provider does not initiate background LLM calls in this release; the optional extraction/query-expansion paths are available to the CLI/library and remain opt-in.
- A non-macOS plugin runtime and a clean external Hermes profile install have not been verified here.
