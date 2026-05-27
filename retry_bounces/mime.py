"""Parse a raw MIME message dump into the structured pieces /email needs."""

from __future__ import annotations

import base64
from email import message_from_string, policy
from email.message import EmailMessage

from .models import Attachment, ParsedMessage


def parse_raw_message(raw: str) -> ParsedMessage:
    """Parse raw RFC822 source (from the Postmark dump) into a :class:`ParsedMessage`.

    Extracts From/Reply-To/Subject/Date, the HTML and/or plain text body, and all
    attachments (base64-encoded, ready for the Postmark /email ``Attachments`` field).
    """
    msg: EmailMessage = message_from_string(raw, policy=policy.default)  # type: ignore[assignment]

    html_body = _body_content(msg, "html")
    text_body = _body_content(msg, "plain")

    attachments: list[Attachment] = []
    for part in msg.iter_attachments():  # type: ignore[attr-defined]
        attachments.append(_to_attachment(part))

    return ParsedMessage(
        from_addr=str(msg["From"] or "").strip(),
        subject=str(msg["Subject"] or "").strip(),
        html_body=html_body,
        text_body=text_body,
        attachments=attachments,
        reply_to=(str(msg["Reply-To"]).strip() if msg["Reply-To"] else None),
        original_date=(str(msg["Date"]).strip() if msg["Date"] else None),
    )


def _body_content(msg: EmailMessage, subtype: str) -> str | None:
    part = msg.get_body(preferencelist=(subtype,))
    if part is None:
        return None
    # Only accept the part if it actually matches the requested subtype;
    # get_body falls back to other types when the preferred one is absent.
    if part.get_content_subtype() != subtype:
        return None
    content = part.get_content()
    return content if isinstance(content, str) else None


def _to_attachment(part: EmailMessage) -> Attachment:
    content = part.get_content()
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content  # bytes for binary attachments

    content_id = part.get("Content-ID")
    if content_id:
        content_id = content_id.strip().strip("<>")

    return Attachment(
        name=part.get_filename() or "attachment",
        content_base64=base64.b64encode(content_bytes).decode("ascii"),
        content_type=part.get_content_type(),
        content_id=content_id or None,
    )
