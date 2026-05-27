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

_BANNER_TEMPLATE = (
    '<div style="background:#fff7d6;border:1px solid #e0c200;color:#594b00;'
    'padding:10px 14px;margin:0 0 12px 0;font-family:Open Sans,Helvetica,Arial,'
    'sans-serif;font-size:13px;line-height:18px;">'
    "This voicemail notification was originally sent on {date} and is being "
    "re-delivered after a mailbox delivery issue was resolved."
    "</div>"
)


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


def inject_date_note_html(html: str, date_str: str) -> str:
    """Insert a 'originally sent' banner just inside <body>, else at the top."""
    banner = _BANNER_TEMPLATE.format(date=escape(date_str))
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
        if html:
            html = inject_date_note_html(html, friendly)
        if text:
            text = f"[Originally sent {friendly}]\n\n{text}"
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
