"""Command-line interface: inspect, find, export, resend."""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__
from .api import PostmarkClient, PostmarkError
from .audit import previously_resent, record_resend
from .config import Config, ConfigError
from .discovery import find_candidates
from .mime import parse_raw_message
from .models import Candidate
from .resend import build_payload, fetch_source, reactivate_recipients

app = typer.Typer(
    help="Discover and resend Postmark voicemail emails blocked by hard-bounce suppressions.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"retry-bounces {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """retry-bounces — resend Postmark voicemails blocked by hard-bounce suppressions."""

# Shared options
StreamOpt = typer.Option(None, "--stream", help="Message stream ID (default: env or 'outbound').")
ApiBaseOpt = typer.Option(None, "--api-base", help="Postmark API base URL.")
DaysOpt = typer.Option(45, "--days", help="Retention window to search, in days.")
DomainOpt = typer.Option(None, "--domain", help="Limit to recipients at this domain (e.g. example.com).")
RecipientOpt = typer.Option(None, "--recipient", "-r", help="Target specific address(es); repeatable. Overrides domain scan.")
DateNoteOpt = typer.Option(False, "--add-date-note", help="Inject a visible 're-delivered' note with the original send date.")
NoteTextOpt = typer.Option(None, "--note-text", help="Custom text for --add-date-note. Use {date} where the original date should appear. Default is a generic re-delivery notice.")
TestRecipientOpt = typer.Option(None, "--test-recipient", help="Send a preview copy to this address instead of the real recipients.")
DryRunOpt = typer.Option(False, "--dry-run", help="Show what would happen without deleting suppressions or sending.")
YesOpt = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts (still respects --dry-run).")
ResendAnywayOpt = typer.Option(False, "--resend-anyway", help="Resend even if already in the audit log (default skips already-sent recipients).")
DelayOpt = typer.Option(0.5, "--delay", help="Seconds to pause between sends (avoids rate limiting on bulk runs). 0 to disable.")


def _load_config(stream: str | None, api_base: str | None) -> Config:
    try:
        return Config.load(stream=stream, api_base=api_base)
    except ConfigError as exc:
        err.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=1)


def _client(cfg: Config) -> PostmarkClient:
    return PostmarkClient(cfg.server_token, cfg.stream, cfg.api_base)


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #
@app.command()
def inspect(
    message_id: Optional[str] = typer.Argument(None, help="Postmark outbound MessageID (UUID) to inspect."),
    recipient: Optional[str] = typer.Option(
        None, "--recipient", "-r",
        help="Instead of inspecting one message, list recent outbound messages to this address (to find a MessageID).",
    ),
    days: int = DaysOpt,
    show_json: bool = typer.Option(False, "--json", help="Also print the full details JSON (large; includes raw body)."),
    save_eml: Optional[Path] = typer.Option(None, "--save-eml", help="Write the raw MIME dump to this .eml file."),
    stream: Optional[str] = StreamOpt,
    api_base: Optional[str] = ApiBaseOpt,
):
    """Inspect a message, or list a recipient's recent messages to find a MessageID.

    Pass a Postmark MessageID (the UUID from `export`/`find`, *not* the email's
    Message-ID header) to confirm the delivery-event field shapes and that the raw
    dump includes the audio attachment. Or pass --recipient to list recent messages.
    """
    cfg = _load_config(stream, api_base)
    with _client(cfg) as client:
        if recipient and not message_id:
            _inspect_list(client, recipient, days)
            return
        if not message_id:
            err.print("[red]Provide a MessageID, or use --recipient to list a recipient's messages.[/red]")
            raise typer.Exit(code=1)

        try:
            details = client.message_details(message_id)
        except PostmarkError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

        console.print("[bold]Message details[/bold] (selected fields):")
        for key in ("MessageID", "Status", "Subject", "From", "To", "Recipients", "ReceivedAt"):
            if key in details:
                console.print(f"  {key}: {details[key]}")
        console.print(f"  Attachments (per details): {len(details.get('Attachments') or [])}")

        events = details.get("MessageEvents", []) or []
        table = Table(title="MessageEvents", show_lines=False)
        table.add_column("Recipient")
        table.add_column("Type")
        table.add_column("Details")
        table.add_column("ReceivedAt")
        for ev in events:
            table.add_row(
                str(ev.get("Recipient", "")),
                str(ev.get("Type", "")),
                str(ev.get("Details", "")),
                str(ev.get("ReceivedAt", "")),
            )
        console.print(table if events else "[dim]No MessageEvents.[/dim]")

        if show_json:
            console.print("\n[bold]Full details JSON:[/bold]")
            console.print_json(json.dumps(details))

        console.print("\n[bold]Raw dump:[/bold]")
        try:
            raw = client.message_dump(message_id)
        except PostmarkError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
        if not raw:
            console.print("  [yellow]Dump is empty[/yellow] — raw content is not stored for this message.")
            return
        console.print(f"  Raw length: {len(raw)} bytes")
        parsed = parse_raw_message(raw)
        console.print(f"  From: {parsed.from_addr}")
        console.print(f"  Subject: {parsed.subject}")
        console.print(f"  HTML body: {'yes' if parsed.html_body else 'no'}; Text body: {'yes' if parsed.text_body else 'no'}")
        if parsed.attachments:
            console.print("  Attachments:")
            for att in parsed.attachments:
                size = int(len(att.content_base64) * 3 / 4)
                console.print(f"    - {att.name} ({att.content_type}, ~{size} bytes)")
        else:
            console.print("  [yellow]No attachments found in dump[/yellow].")

        if save_eml:
            save_eml.write_text(raw, encoding="utf-8")
            console.print(f"\n  Wrote raw MIME to {save_eml}")


def _inspect_list(client: PostmarkClient, recipient: str, days: int):
    """List recent outbound messages to ``recipient`` so the operator can grab an ID."""
    fromdate = (datetime.now() - timedelta(days=days + 1)).strftime("%Y-%m-%d")
    table = Table(title=f"Recent outbound messages to {recipient}")
    table.add_column("MessageID")
    table.add_column("ReceivedAt")
    table.add_column("Status")
    table.add_column("Subject")
    count = 0
    for msg in client.iter_outbound(recipient=recipient, fromdate=fromdate):
        table.add_row(
            str(msg.get("MessageID", "")),
            str(msg.get("ReceivedAt", "")),
            str(msg.get("Status", "")),
            str(msg.get("Subject", ""))[:50],
        )
        count += 1
    if count:
        console.print(table)
        console.print(f"{count} message(s). Inspect one with: retry-bounces inspect <MessageID>")
    else:
        console.print(f"No outbound messages to {recipient} in the last {days} days.")


# --------------------------------------------------------------------------- #
# bounces (diagnostic)
# --------------------------------------------------------------------------- #
@app.command()
def bounces(
    recipient: Optional[str] = typer.Option(None, "--recipient", "-r", help="Filter bounces to this email address."),
    domain: Optional[str] = typer.Option(None, "--domain", help="Filter bounces to this domain (client-side)."),
    bounce_type: Optional[str] = typer.Option(None, "--type", help="Filter by Postmark bounce Type (e.g. HardBounce, SMTPApiError, Blocked)."),
    days: int = DaysOpt,
    stream: Optional[str] = StreamOpt,
    api_base: Optional[str] = ApiBaseOpt,
):
    """List Postmark bounce records (BouncedAt, Type, MessageID, Inactive, dumps).

    Read-only diagnostic. Use it to see exactly which messages bounced for a
    recipient — including blocked/SMTP-API-error sends — and their types.
    """
    cfg = _load_config(stream, api_base)
    fromdate = (datetime.now() - timedelta(days=days + 1)).strftime("%Y-%m-%dT00:00:00")
    suffix = ("@" + domain.lstrip("@").lower()) if domain else None

    table = Table(title="Bounces", show_lines=False)
    for col in ("BouncedAt", "Type", "Email", "BounceID", "MessageID", "Inactive", "CanActivate", "BncDump?", "Subject"):
        table.add_column(col)

    count = 0
    with _client(cfg) as client:
        try:
            for b in client.iter_bounces(email_filter=recipient, bounce_type=bounce_type, fromdate=fromdate):
                if suffix and not str(b.get("Email", "")).lower().endswith(suffix):
                    continue
                table.add_row(
                    str(b.get("BouncedAt", ""))[:19],
                    str(b.get("Type", "")),
                    str(b.get("Email", "")),
                    str(b.get("ID", "")),
                    str(b.get("MessageID", "")),
                    str(b.get("Inactive", "")),
                    str(b.get("CanActivate", "")),
                    str(b.get("DumpAvailable", "")),  # bounce dump (expires at 30 days)
                    str(b.get("Subject", ""))[:40],
                )
                count += 1
        except PostmarkError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

    if count:
        console.print(table)
        console.print(f"{count} bounce record(s). 'BncDump?' = bounce dump available (≤30 days).")
        console.print("Inspect one with: retry-bounces inspect-bounce <BounceID>")
    else:
        console.print("No bounce records found for the given criteria.")


@app.command(name="inspect-bounce")
def inspect_bounce(
    bounce_id: int = typer.Argument(..., help="Numeric Postmark Bounce ID (from the `bounces` command)."),
    save_eml: Optional[Path] = typer.Option(None, "--save-eml", help="Write the bounce dump to this .eml file."),
    stream: Optional[str] = StreamOpt,
    api_base: Optional[str] = ApiBaseOpt,
):
    """Inspect a single bounce and its dump — to see if the original message is recoverable.

    Use this on an SMTPApiError record to determine whether Postmark retained the
    submitted voicemail (with attachment) in the bounce dump, even though it was
    never stored as an outbound message.
    """
    cfg = _load_config(stream, api_base)
    with _client(cfg) as client:
        try:
            b = client.get_bounce(bounce_id)
        except PostmarkError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

        console.print("[bold]Bounce[/bold] (selected fields):")
        for key in ("ID", "Type", "TypeCode", "Email", "MessageID", "BouncedAt", "Inactive", "CanActivate", "DumpAvailable", "Subject"):
            if key in b:
                console.print(f"  {key}: {b[key]}")
        content_field = b.get("Content") or ""
        console.print(f"  Content present in bounce record: {'yes (%d bytes)' % len(content_field) if content_field else 'no'}")

        console.print("\n[bold]Recoverable source:[/bold]")
        try:
            dump = client.bounce_dump(bounce_id)
        except PostmarkError as exc:
            console.print(f"  [yellow]Could not fetch bounce dump: {exc}[/yellow]")
            dump = ""
        # Prefer the /dump endpoint; fall back to the bounce record's Content field.
        source = dump or content_field
        if dump:
            console.print(f"  Using bounce /dump endpoint ({len(dump)} bytes).")
        elif content_field:
            console.print(f"  /dump empty; using bounce record Content field ({len(content_field)} bytes).")
        else:
            console.print("  [yellow]No bounce dump and no Content field — original message not retained here.[/yellow]")
            return
        dump = source
        console.print(f"  Raw length: {len(dump)} bytes")
        try:
            parsed = parse_raw_message(dump)
        except Exception as exc:  # noqa: BLE001 - dump may not be a full MIME message
            console.print(f"  [yellow]Dump is not a parseable MIME message: {exc}[/yellow]")
            if save_eml:
                save_eml.write_text(dump, encoding="utf-8")
                console.print(f"  Wrote raw dump to {save_eml}")
            return
        console.print(f"  From: {parsed.from_addr}")
        console.print(f"  Subject: {parsed.subject}")
        console.print(f"  HTML body: {'yes' if parsed.html_body else 'no'}; Text body: {'yes' if parsed.text_body else 'no'}")
        if parsed.attachments:
            console.print("  Attachments:")
            for att in parsed.attachments:
                size = int(len(att.content_base64) * 3 / 4)
                console.print(f"    - {att.name} ({att.content_type}, ~{size} bytes)")
        else:
            console.print("  [yellow]No attachments found in bounce dump[/yellow].")
        if save_eml:
            save_eml.write_text(dump, encoding="utf-8")
            console.print(f"\n  Wrote bounce dump to {save_eml}")


# --------------------------------------------------------------------------- #
# discovery helpers
# --------------------------------------------------------------------------- #
def _discover(client: PostmarkClient, domain, recipients, days) -> list[Candidate]:
    return find_candidates(
        client,
        domain=domain,
        recipients=list(recipients) if recipients else None,
        days=days,
        progress=lambda text: err.print(f"[dim]{text}[/dim]"),
    )


def _candidate_label(c: Candidate) -> str:
    # Subject is only meaningful for HardBounce; SMTPApiError carries Postmark's
    # error text, so for those select by date / type / recipients instead.
    recips = ", ".join(c.bounced_recipients)
    tail = f"  ·  {c.subject[:50]}" if c.bounce_type == "HardBounce" and c.subject else ""
    return f"{c.sent_at[:19]} | {c.bounce_type:<13} | → {recips}{tail}"


# --------------------------------------------------------------------------- #
# find (interactive)
# --------------------------------------------------------------------------- #
@app.command()
def find(
    domain: Optional[str] = DomainOpt,
    recipient: Optional[list[str]] = RecipientOpt,
    days: int = DaysOpt,
    add_date_note: bool = DateNoteOpt,
    note_text: Optional[str] = NoteTextOpt,
    test_recipient: Optional[str] = TestRecipientOpt,
    dry_run: bool = DryRunOpt,
    yes: bool = YesOpt,
    resend_anyway: bool = ResendAnywayOpt,
    delay: float = DelayOpt,
    stream: Optional[str] = StreamOpt,
    api_base: Optional[str] = ApiBaseOpt,
):
    """Discover blocked voicemails, pick which to resend, then reactivate and resend."""
    if not domain and not recipient:
        err.print("[yellow]No --domain or --recipient given; scanning ALL bounces in the window.[/yellow]")

    cfg = _load_config(stream, api_base)
    with _client(cfg) as client:
        try:
            candidates = _discover(client, domain, recipient, days)
        except PostmarkError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

        if not candidates:
            console.print("No blocked/bounced messages found for the given criteria.")
            return

        try:
            from InquirerPy import inquirer
            from InquirerPy.base.control import Choice
        except ImportError:  # pragma: no cover
            err.print("[red]InquirerPy is required for interactive selection.[/red]")
            raise typer.Exit(code=1)

        choices = [Choice(value=c.message_id, name=_candidate_label(c)) for c in candidates]
        # In a fuzzy prompt the space bar types into the filter, so Tab toggles a
        # selection. Esc cancels cleanly (so does Ctrl-C, handled below).
        try:
            selected_ids = inquirer.fuzzy(
                message="Filter & select messages to resend:",
                choices=choices,
                multiselect=True,
                border=True,
                instruction="(type to filter · Tab toggles · Enter confirms · Esc cancels)",
                max_height="70%",
                mandatory=False,
                keybindings={"skip": [{"key": "escape"}]},
            ).execute()
        except KeyboardInterrupt:
            console.print("Cancelled.")
            return

        if not selected_ids:
            console.print("Nothing selected — cancelled.")
            return

        selected = [c for c in candidates if c.message_id in set(selected_ids)]
        _process_resends(
            client, cfg, selected,
            add_date_note=add_date_note,
            note_text=note_text,
            test_recipient=test_recipient,
            dry_run=dry_run,
            assume_yes=yes,
            resend_anyway=resend_anyway,
            delay=delay,
        )


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
@app.command()
def export(
    output: Path = typer.Option(..., "--output", "-o", help="Output file (.json or .csv)."),
    domain: Optional[str] = DomainOpt,
    recipient: Optional[list[str]] = RecipientOpt,
    days: int = DaysOpt,
    stream: Optional[str] = StreamOpt,
    api_base: Optional[str] = ApiBaseOpt,
):
    """Export discovered candidates to a JSON/CSV file for offline review."""
    cfg = _load_config(stream, api_base)
    with _client(cfg) as client:
        try:
            candidates = _discover(client, domain, recipient, days)
        except PostmarkError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

    rows = [c.to_row() for c in candidates]
    if output.suffix.lower() == ".csv":
        fieldnames = list(rows[0].keys()) if rows else list(
            Candidate(message_id="", subject="", sent_at="", bounced_recipients=[]).to_row().keys()
        )
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        output.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    console.print(f"Wrote {len(rows)} candidate(s) to {output}.")


# --------------------------------------------------------------------------- #
# resend (from file)
# --------------------------------------------------------------------------- #
@app.command()
def resend(
    from_file: Path = typer.Option(..., "--from-file", help="JSON/CSV file produced by `export` (optionally edited)."),
    add_date_note: bool = DateNoteOpt,
    note_text: Optional[str] = NoteTextOpt,
    test_recipient: Optional[str] = TestRecipientOpt,
    dry_run: bool = DryRunOpt,
    yes: bool = YesOpt,
    resend_anyway: bool = ResendAnywayOpt,
    delay: float = DelayOpt,
    stream: Optional[str] = StreamOpt,
    api_base: Optional[str] = ApiBaseOpt,
):
    """Resend the candidates listed in an export file (bulk path)."""
    candidates = _load_candidates(from_file)
    if not candidates:
        console.print("No candidates found in file.")
        return

    cfg = _load_config(stream, api_base)
    with _client(cfg) as client:
        _process_resends(
            client, cfg, candidates,
            add_date_note=add_date_note,
            note_text=note_text,
            test_recipient=test_recipient,
            dry_run=dry_run,
            assume_yes=yes,
            resend_anyway=resend_anyway,
            delay=delay,
        )


def _load_candidates(path: Path) -> list[Candidate]:
    if not path.exists():
        err.print(f"[red]File not found: {path}[/red]")
        raise typer.Exit(code=1)
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    else:
        rows = json.loads(path.read_text(encoding="utf-8"))
    return [Candidate.from_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# shared resend orchestration
# --------------------------------------------------------------------------- #
class _BatchConfirm:
    """y/n/all/quit confirmation that can latch to 'all' for the rest of the run."""

    def __init__(self, assume_yes: bool = False):
        self._all = assume_yes
        self.quit = False

    def ask(self, prompt: str) -> bool:
        if self._all:
            return True
        ans = Prompt.ask(
            f"{prompt} (y)es / (n)o / (a)ll / (q)uit",
            choices=["y", "n", "a", "q"],
            default="y",
            show_choices=False,
        )
        if ans == "a":
            self._all = True
            return True
        if ans == "q":
            self.quit = True
            return False
        return ans == "y"


def _process_resends(
    client: PostmarkClient,
    cfg: Config,
    candidates: list[Candidate],
    *,
    add_date_note: bool,
    note_text: str | None,
    test_recipient: str | None,
    dry_run: bool,
    assume_yes: bool,
    resend_anyway: bool = False,
    delay: float = 0.0,
):
    confirmer = _BatchConfirm(assume_yes=assume_yes)
    sent = 0
    skipped = 0
    for c in candidates:
        console.rule(f"[bold]{c.subject or '(no subject)'}[/bold]")
        console.print(f"MessageID {c.message_id}  •  sent {c.sent_at}")
        console.print(f"Bounced recipient(s): {', '.join(c.bounced_recipients) or '(none recorded)'}")

        if test_recipient:
            targets = [test_recipient]
            console.print(f"[cyan]TEST MODE[/cyan] — sending preview to {test_recipient} (suppression check skipped).")
        else:
            pending = list(c.bounced_recipients)
            if not resend_anyway:
                already = [r for r in pending if previously_resent(c.message_id, r)]
                if already:
                    console.print(f"  [dim]already resent, skipping: {', '.join(already)}[/dim]")
                    pending = [r for r in pending if r not in already]
                if not pending:
                    console.print("  [yellow]All recipients already resent; skipping (use --resend-anyway to force).[/yellow]")
                    skipped += 1
                    continue

            def confirm(addr: str, reason: str) -> bool:
                return confirmer.ask(f"  Remove {addr} from suppression list ({reason}) so it can receive the resend?")

            targets = reactivate_recipients(
                client, pending, confirm=confirm, echo=console.print, dry_run=dry_run
            )
            if confirmer.quit:
                console.print("Quit — stopping.")
                break
            if not targets:
                console.print("  [yellow]No sendable recipients; skipping.[/yellow]")
                skipped += 1
                continue

        if c.bounce_type == "Blocked":
            console.print("  [dim]note: 'Blocked' type — fetching content like an SMTPApiError.[/dim]")
        raw, detail = fetch_source(client, c)
        if not raw:
            console.print(
                f"  [red]Original content not recoverable ({c.bounce_type}): {detail}; skipping.[/red]"
            )
            skipped += 1
            continue

        parsed = parse_raw_message(raw)
        try:
            payload = build_payload(
                parsed, targets, stream=cfg.stream, add_date_note=add_date_note, note_text=note_text
            )
        except ValueError as exc:
            console.print(f"  [red]Cannot build message: {exc}[/red]")
            skipped += 1
            continue

        att = payload.get("Attachments", [])
        console.print(f"  → To: {payload['To']}")
        console.print(f"  → From: {payload['From']}  •  Attachments: {len(att)}")

        if dry_run:
            console.print("  [dim]dry-run: not sending.[/dim]")
            continue

        if not confirmer.ask(f"  Send this message to {payload['To']}?"):
            if confirmer.quit:
                console.print("Quit — stopping.")
                break
            console.print("  Skipped.")
            skipped += 1
            continue

        if delay and sent > 0:
            time.sleep(delay)
        try:
            resp = client.send_email(payload)
        except PostmarkError as exc:
            console.print(f"  [red]Send failed: {exc}[/red]")
            skipped += 1
            continue

        pm_id = resp.get("MessageID")
        console.print(f"  [green]✓ Sent[/green] (Postmark MessageID {pm_id})")
        sent += 1
        if not test_recipient:
            record_resend(
                message_id=c.message_id,
                recipients=targets,
                postmark_message_id=pm_id,
                test_recipient=None,
            )

    console.rule()
    console.print(f"Done. Sent {sent}, skipped {skipped}.")


if __name__ == "__main__":  # pragma: no cover
    app()
