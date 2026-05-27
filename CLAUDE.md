# CLAUDE.md

## Project Overview
A Python CLI that finds Postmark voicemail-notification emails which were blocked by
hard-bounce suppressions and resends them faithfully (HTML body + audio attachment) to
only the recipients who bounced. It exists because Postmark suppresses an address after a
hard bounce and then logs every later voicemail to it as an error, with no built-in retry.

## Tech Stack
- **Python 3.11+**, managed with **uv**.
- **httpx** — Postmark REST client.
- **typer** + **rich** — CLI and terminal output.
- **InquirerPy** — interactive fuzzy multi-select picker.
- **python-dotenv** — `.env` loading; **tzdata** — timezone DB for `zoneinfo`.
- stdlib `email` for MIME parsing. **pytest** for tests.

## Project Structure
- `retry_bounces/cli.py` — Typer app and all commands (`inspect`, `bounces`,
  `inspect-bounce`, `find`, `export`, `resend`); `_process_resends` orchestrates sending.
- `retry_bounces/api.py` — `PostmarkClient`: messages, suppressions, bounces, `/email`;
  429 retry/backoff lives in `_request`.
- `retry_bounces/discovery.py` — `find_candidates`, Bounces-API based (the source of truth).
- `retry_bounces/resend.py` — `fetch_source` (type-keyed content recovery), `build_payload`,
  `reactivate_recipients`, `friendly_eastern`.
- `retry_bounces/mime.py` — raw MIME → `ParsedMessage`. `models.py` — dataclasses.
- `retry_bounces/config.py` — config + 1Password `op://` secret resolution.
- `retry_bounces/audit.py` — `resent.jsonl` audit log + duplicate checks.
- `tests/test_core.py` — offline unit tests (parsing, payload, discovery, fetch, config, audit).

## Build, Test, and Run
```bash
uv sync                      # install deps
cp .env.example .env         # set POSTMARK_SERVER_TOKEN (+ OP_ACCOUNT if using op://)
uv run pytest                # run tests
uv run retry-bounces --help  # run the CLI
```

## Coding Conventions
- `from __future__ import annotations`; modern type hints (`str | None`, `list[...]`).
- Module-level docstrings explain the "why"; constants (e.g. `RESEND_TYPES`) are commented
  with the reasoning so behavior isn't silently changed.
- Pure logic (parsing, payload building, discovery) is kept out of `cli.py` so it's unit-
  testable without the API or a TTY; tests use small fake clients, no network.
- Postmark API errors raise `PostmarkError`; CLI commands catch it and exit cleanly.

## Important Notes
- **Single Postmark *Server* token** authorizes messages, suppressions, and `/email`.
  `POSTMARK_SERVER_TOKEN` may be a raw token or a 1Password `op://` reference resolved at
  runtime via the `op` CLI; `OP_ACCOUNT` selects the account. `.env` is gitignored.
- **Content recovery is keyed on bounce type** (`fetch_source`): `HardBounce` → outbound
  message dump; `SMTPApiError`/`Blocked` → bounce dump, falling back to the bounce record's
  `Content` field (those sends were rejected before being stored as messages). Never use a
  bounce dump for a HardBounce — it's the remote DSN, not the voicemail.
- Discovery uses the **Bounces API**, not delivery events. `MessageEvents` in message
  details is non-exhaustive and must not be trusted for delivery confirmation.
- A bounce's `DumpAvailable` field is **unreliable** (observed `False` while the dump
  returned content) — don't pre-filter on it; just attempt the fetch.
- **Order matters:** delete a suppression *before* sending, or Postmark rejects with 406.
- **Safety:** `--dry-run` is strictly read-only (no suppression deletes, no sends);
  `--test-recipient` previews to an address and is not logged; already-sent recipients are
  skipped via `resent.jsonl` unless `--resend-anyway`. `--delay` paces bulk sends (default 0.5s).
- Date notes (`--add-date-note`) render DST-aware US Eastern; the `X-Original-Date` header
  keeps the raw value.
- `.gitignore` excludes `.env`, `resent.jsonl`, `*.eml`, and `candidates.*`; **`uv.lock` is
  committed** intentionally for reproducible installs.
```
