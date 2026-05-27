# retry-hard-bounces-postmark

**Version 0.1.1** · MIT licensed · check `uv run retry-bounces --version`

Discover [Postmark](https://www.postmarkapp.com) voicemail emails that were **blocked by hard-bounce suppressions**
and **resend** them — faithfully, including the original HTML body and audio
attachment — to only the recipient(s) who actually bounced.

## Why

Voicemail notifications are sent from the PBX through Postmark. When a recipient
hard-bounces, Postmark suppresses that address and then **blocks** every later
voicemail to it (logged as an SMTP API error / inactive recipient). Postmark keeps
the blocked messages' raw content for **45 days** (varies depending on plan) but 
offers no built-in retry. This tool finds those messages, optionally reactivates 
the address, and resends.

Because some emails may have been sent to multiple recipients, the tool is careful
to resend **only** to the bounced address(es) — never to recipients who already
got the message.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- A Postmark **Server** API token for the server hosting the voicemail stream.
  One token authorizes search, suppressions, and sending — no separate send key.
- (Optional) the [1Password CLI](https://developer.1password.com/docs/cli/) (`op`)
  if you store the token as an `op://` reference.
- The Postmark server must have **"Save outbound message data, including all raw
  content"** enabled (it is) so the raw dump includes the audio attachment.

## Setup

```bash
uv sync
cp .env.example .env   # then edit .env
```

`.env` is gitignored. Set `POSTMARK_SERVER_TOKEN` to either a raw token or a
1Password reference (resolved at runtime via `op read`, never written to disk):

```dotenv
POSTMARK_SERVER_TOKEN=op://Vault/Postmark/server-token
# POSTMARK_MESSAGE_STREAM=outbound   # optional, default "outbound"
```

Run commands with `uv run retry-bounces <command>`.

## Commands

### `inspect` — run this first
Prints a message's details, per-recipient delivery events, and whether the raw
dump is present and contains the attachment. Use it to confirm the API field
shapes the discovery filter relies on, and that the voicemail audio is in the dump.

The argument is **Postmark's MessageID** (the UUID shown by `export`/`find`), not
the email's `Message-ID` header. To find a UUID, list a recipient's messages:

```bash
uv run retry-bounces inspect --recipient bob@example.com   # list MessageIDs
uv run retry-bounces inspect 8ad0e8b1-1c2d-3e4f-5a6b-7c8d9e0f1a2b
uv run retry-bounces inspect <id> --json                       # also dump full details JSON (large)
uv run retry-bounces inspect <id> --save-eml message.eml       # write raw MIME to a file
```

### `find` — interactive discover + resend
Discovers blocked voicemails, shows a filterable multi-select list (type to filter
by email/domain, **Tab** to toggle, Enter to confirm, Esc to cancel), then
reactivates and resends the chosen ones.

```bash
# Always dry-run first:
uv run retry-bounces find --domain example.com --dry-run

# Preview a real message to yourself before sending to the customer:
uv run retry-bounces find --domain example.com --test-recipient you@example.com

# Real send (prompts before removing each suppression and before sending):
uv run retry-bounces find --domain example.com --add-date-note
```

### `export` / `resend` — bulk path
Export candidates to a JSON or CSV file, review/edit which to keep, then resend.

```bash
uv run retry-bounces export --domain example.com -o candidates.json
# review/trim candidates.json, then:
uv run retry-bounces resend --from-file candidates.json --add-date-note
```

## Key options

| Option | Meaning |
|---|---|
| `--domain` | Limit to recipients at a domain (client-side filter of the suppression list). |
| `--recipient/-r` | Target specific address(es); repeatable. Overrides the domain scan. |
| `--days` | Retention window to search (default 45). |
| `--test-recipient` | Send a preview copy here instead of the real recipients. Skips suppression removal; not logged as a real resend. |
| `--add-date-note` | Inject a visible re-delivery banner with the original send date (DST-aware US Eastern, e.g. "May 22, 2026 at 1:28 PM EDT") + an `X-Original-Date` header with the precise original value. The banner is a centered, Outlook-safe table. |
| `--note-text` | Custom banner wording for `--add-date-note`. Use `{date}` where the original date should appear (e.g. `"Recorded {date}; re-sending."`). |
| `--delay SECONDS` | Pause between sends to avoid rate limiting (default 0.5; `0` disables). The client also auto-retries on HTTP 429. |
| `--dry-run` | Show what would happen; no suppression changes, no sends. |
| `--yes/-y` | Skip all confirmation prompts (still honors `--dry-run`). |
| `--resend-anyway` | Resend even if already logged in `resent.jsonl` (default skips already-sent recipients). |

At each confirmation you can answer **y** (yes), **n** (no/skip), **a** (yes to
all remaining — confirm once, then send the rest without prompting), or **q**
(quit the run). By default, recipients already recorded in `resent.jsonl` are
skipped; pass `--resend-anyway` to force them.

## How it works

1. **Discover** — query the **Bounces API** (authoritative per-recipient failure
   records), filtered to your `--domain` (client-side) or specific `--recipient`s.
   Keep resend-worthy types — `HardBounce`, `SMTPApiError` (voicemails blocked
   while the recipient was inactive), and `Blocked` — and exclude `AutoResponder`,
   `SpamComplaint`, `Transient`, and `ManuallyDeactivated`. Group by message,
   carrying the bounced recipients and their bounce IDs.
2. **Reactivate** — before sending, check each target's suppression status and,
   with confirmation, delete it (`suppressions/delete`). Order matters: delete
   **then** send, or Postmark rejects with error 406. `SpamComplaint` suppressions
   can't be deleted and are skipped.
3. **Recover content + resend** — fetch the original MIME, keyed on bounce type:
   `HardBounce` comes from the outbound **message** dump; `SMTPApiError`/`Blocked`
   come from the **bounce** dump (falling back to the bounce record's `Content`
   field), because Postmark rejected those sends before storing them as outbound
   messages. Parse out From/Subject/bodies/attachments, set `To` to the surviving
   bounced recipients (or the test address), optionally add the date note, and
   POST to `/email`. Real sends are recorded in `resent.jsonl` so re-runs warn
   about duplicates. If the content can't be recovered (e.g. an aged-out bounce
   dump), that message is skipped with a clear message rather than sending wrong
   content.

### Diagnostics

- `bounces [--recipient … | --domain …] [--type HardBounce]` — list bounce records
  (BouncedAt, Type, Email, BounceID, MessageID, Inactive, dump flag, Subject).
- `inspect-bounce <BounceID>` — show a single bounce and whether its dump/`Content`
  holds the recoverable original message + attachment.

## Safety notes

- Always `--dry-run` first, then `--test-recipient` to yourself, then the real send.
- The tool never re-delivers to recipients who already received a shared-mailbox
  voicemail — only to addresses that bounced.
- `resent.jsonl` (gitignored) is the local audit trail of real resends; already-sent
  recipients are skipped on later runs unless you pass `--resend-anyway`.

## Tests

```bash
uv run pytest
```

Tests cover MIME parsing (incl. attachments), payload building, date-note
injection, the discovery classifier, secret passthrough, and the audit log. The
end-to-end Postmark paths require a live token and are exercised via `inspect`
and `--dry-run`.

## Versioning & Changelog

The version lives in `retry_bounces/__init__.py` (`__version__`) and is mirrored
in `pyproject.toml`; `uv run retry-bounces --version` prints it. Bump it on every
change — PATCH for fixes/tweaks, MINOR for new features, MAJOR for breaking
changes ([semver](https://semver.org/)) — and add an entry below.

### 0.1.1
- Re-delivery note is now a centered, ~600px, **Outlook-safe table banner**
  (renders reliably across clients, not just modern webmail).
- Generic default wording ("This message was originally sent on … and is being
  re-delivered after a mailbox delivery issue was resolved"); new **`--note-text`**
  option to customize it (use `{date}` for the original send date).

### 0.1.0
- Initial release.
- Discover bounced emails, including attachments (for example, voicemail messages) 
- via the Postmark Bounces API; resend faithfully to only the recipients who bounced.
- Type-keyed content recovery (message dump vs. bounce dump/`Content`).
- Commands: `inspect`, `bounces`, `inspect-bounce`, `find`, `export`, `resend`.
- Safety: `--dry-run`, `--test-recipient`, skip-already-sent audit log
  (`--resend-anyway`), `y/n/all/quit` confirmations, `--delay`, HTTP 429 retry.
- DST-aware US Eastern date notes; 1Password `op://` secret resolution.

## License

[MIT](LICENSE) © 2026 [David Szpunar](https://david.szpunar.com)
