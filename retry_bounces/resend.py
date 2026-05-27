"""Build /email payloads and reactivate suppressed recipients before sending."""

from __future__ import annotations

import re
from email.utils import parsedate_to_datetime
from html import escape
from typing import Callable
from zoneinfo import ZoneInfo

from .api import PostmarkClient, PostmarkError
from .models import Candidate, ParsedMessage

EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def friendly_eastern(date_header: str) -> str | None:
    """Format an RFC 2822 Date header as a friendly US Eastern time, or None.

    Example: ``"Mon, 27 Apr 2026 16:06:08 +0000"`` → ``"April 27, 2026 at
    12:06 PM EDT"``. The timezone abbreviation is DST-aware (EDT in summer,
    EST in winter). A header with no offset is assumed to be UTC.
    """
    try:
        dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    eastern = dt.astimezone(EASTERN)
    hour12 = eastern.strftime("%I").lstrip("0") or "12"
    return (
        f"{eastern.strftime('%B')} {eastern.day}, {eastern.year} "
        f"at {hour12}:{eastern.strftime('%M %p')} {eastern.strftime('%Z')}"
    )

# Default re-delivery notice. `{date}` is replaced with the friendly Eastern date.
DEFAULT_NOTE = (
    "This message was originally sent on {date} and is being re-delivered "
    "after a mailbox delivery issue was resolved."
)

_BANNER_BG = "#fff7d6"
_BANNER_BORDER = "#e0c200"
_BANNER_FG = "#594b00"


def _banner_html(text: str) -> str:
    """A centered, ~600px, Outlook-safe table banner wrapping ``text``.

    Uses a table with a ``bgcolor`` attribute and cell padding (which Word/Outlook
    honor, unlike background/padding on a <div>), centered via an outer table.
    """
    safe = escape(text)
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="border-collapse:collapse;"><tr>'
        '<td align="center" style="padding:0;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'border="0" style="width:600px;max-width:600px;margin:0 auto;border-collapse:collapse;">'
        f'<tr><td bgcolor="{_BANNER_BG}" style="background:{_BANNER_BG};'
        f'border:1px solid {_BANNER_BORDER};color:{_BANNER_FG};padding:10px 14px;'
        'font-family:Helvetica,Arial,sans-serif;font-size:13px;line-height:18px;">'
        f"{safe}</td></tr></table></td></tr></table>"
    )


def render_note(note_text: str | None, friendly_date: str) -> str:
    """Fill the ``{date}`` placeholder in the note (default or custom)."""
    return (note_text or DEFAULT_NOTE).replace("{date}", friendly_date)


def fetch_source(client: PostmarkClient, candidate: Candidate) -> tuple[str | None, str | None]:
    """Fetch the raw MIME of the original voicemail.

    Returns ``(raw, error_detail)``. On success ``raw`` is the MIME and
    ``error_detail`` is None. On failure ``raw`` is None and ``error_detail``
    distinguishes a transient API error (so the caller can say "try again") from
    content that is genuinely absent (e.g. a bounce dump aged out at 30 days).

    The source is keyed on bounce type:
      * HardBounce           → the outbound message dump (the bounce dump would be
                               the remote DSN, not the voicemail). If empty, give up.
      * SMTPApiError/Blocked → the bounce dump, falling back to the bounce record's
                               ``Content`` field (the message was never stored as an
                               outbound message). Each recipient's bounce ID is tried
                               in order, since one may have aged out while another has not.
    """
    if candidate.bounce_type == "HardBounce":
        try:
            raw = client.message_dump(candidate.message_id)
        except PostmarkError as exc:
            return None, f"message dump API error: {exc}"
        return (raw, None) if raw else (None, "message dump empty (raw content not stored)")

    # SMTPApiError / Blocked
    errors: list[str] = []
    for bid in candidate.bounce_ids:
        try:
            raw = client.bounce_dump(bid)
            if raw:
                return raw, None
        except PostmarkError as exc:
            errors.append(f"bounce {bid} dump: {exc}")
        try:
            bounce = client.get_bounce(bid)
            if bounce.get("Content"):
                return bounce["Content"], None
        except PostmarkError as exc:
            errors.append(f"bounce {bid} fetch: {exc}")
    if errors:
        return None, "API error(s): " + "; ".join(errors)
    return None, "no bounce dump or Content returned (no longer retained by Postmark)"


def inject_date_note_html(html: str, note: str) -> str:
    """Insert the note banner just inside <body>, else at the top."""
    banner = _banner_html(note)
    match = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if match:
        idx = match.end()
        return html[:idx] + banner + html[idx:]
    return banner + html


def build_payload(
    parsed: ParsedMessage,
    to_addrs: list[str],
    *,
    stream: str,
    add_date_note: bool = False,
    note_text: str | None = None,
) -> dict:
    """Construct the Postmark /email request body for a faithful resend."""
    if not to_addrs:
        raise ValueError("No recipients to send to.")

    html = parsed.html_body
    text = parsed.text_body
    if not html and not text:
        raise ValueError("Parsed message has neither an HTML nor a text body.")

    headers: list[dict] = []
    if add_date_note and parsed.original_date:
        friendly = friendly_eastern(parsed.original_date) or parsed.original_date
        note = render_note(note_text, friendly)
        if html:
            html = inject_date_note_html(html, note)
        if text:
            text = f"{note}\n\n{text}"
        # Keep the precise original value in the header; show the friendly one to readers.
        headers.append({"Name": "X-Original-Date", "Value": parsed.original_date})

    payload: dict = {
        "From": parsed.from_addr,
        "To": ", ".join(to_addrs),
        "Subject": parsed.subject,
        "MessageStream": stream,
    }
    if html:
        payload["HtmlBody"] = html
    if text:
        payload["TextBody"] = text
    if parsed.reply_to:
        payload["ReplyTo"] = parsed.reply_to
    if parsed.attachments:
        payload["Attachments"] = [a.to_api() for a in parsed.attachments]
    if headers:
        payload["Headers"] = headers
    return payload


def reactivate_recipients(
    client: PostmarkClient,
    recipients: list[str],
    *,
    confirm: Callable[[str, str], bool],
    echo: Callable[[str], None],
    dry_run: bool = False,
) -> list[str]:
    """Ensure each recipient is sendable, reactivating (with confirmation) if needed.

    Returns the subset of ``recipients`` that are now safe to send to. A recipient
    is dropped if it stays suppressed (declined, failed, or a non-deletable
    SpamComplaint). ``confirm(address, reason)`` gates each deletion; ``echo``
    reports progress.

    When ``dry_run`` is set, this is strictly read-only: it checks suppression
    status but never deletes, and reports what *would* be reactivated.
    """
    sendable: list[str] = []
    for addr in recipients:
        record = client.is_suppressed(addr)
        if record is None:
            sendable.append(addr)
            continue

        reason = record.get("SuppressionReason", "")
        if reason == "SpamComplaint":
            echo(f"  ! {addr} is suppressed as a SpamComplaint and cannot be removed; skipping.")
            continue

        if dry_run:
            echo(f"  would remove {addr} from suppression list ({reason}) before sending.")
            sendable.append(addr)
            continue

        if not confirm(addr, reason):
            echo(f"  - {addr} left on the suppression list; skipping (would fail to send).")
            continue

        results = client.delete_suppressions([addr])
        status = results[0].get("Status") if results else None
        if status == "Deleted":
            echo(f"  ✓ {addr} removed from suppression list.")
            sendable.append(addr)
        else:
            detail = results[0].get("Message", "unknown error") if results else "no response"
            echo(f"  ! Failed to remove {addr} from suppression list: {detail}; skipping.")
    return sendable
