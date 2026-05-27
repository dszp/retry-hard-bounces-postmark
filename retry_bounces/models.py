"""Data structures passed between the API, discovery, and resend layers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Suppression:
    """A single entry from the Postmark suppression dump."""

    email: str
    reason: str  # HardBounce | SpamComplaint | ManualSuppression
    origin: str  # Recipient | Customer | Admin
    created_at: str

    @classmethod
    def from_api(cls, data: dict) -> "Suppression":
        return cls(
            email=data.get("EmailAddress", ""),
            reason=data.get("SuppressionReason", ""),
            origin=data.get("Origin", ""),
            created_at=data.get("CreatedAt", ""),
        )


@dataclass
class Attachment:
    """A decoded attachment ready to hand to the Postmark /email endpoint."""

    name: str
    content_base64: str
    content_type: str
    content_id: str | None = None

    def to_api(self) -> dict:
        payload = {
            "Name": self.name,
            "Content": self.content_base64,
            "ContentType": self.content_type,
        }
        if self.content_id:
            payload["ContentID"] = self.content_id
        return payload


@dataclass
class ParsedMessage:
    """Structured form of a raw MIME message dump, ready to rebuild for /email."""

    from_addr: str
    subject: str
    html_body: str | None
    text_body: str | None
    attachments: list[Attachment] = field(default_factory=list)
    reply_to: str | None = None
    original_date: str | None = None


@dataclass
class Candidate:
    """A bounced voicemail message that is a candidate for resending.

    Built from Postmark bounce records grouped by ``message_id``.
    ``bounced_recipients`` are the addresses that failed on this message (the
    default resend targets). ``bounce_type`` selects how the original content is
    fetched (HardBounce → message dump; SMTPApiError/Blocked → bounce dump).
    ``bounce_ids`` holds one bounce record ID per bounced recipient, tried in
    order as content sources.
    """

    message_id: str
    subject: str
    sent_at: str
    bounced_recipients: list[str]
    bounce_type: str = ""
    bounce_ids: list[int] = field(default_factory=list)
    dump_available: bool = True

    def to_row(self) -> dict:
        """Flat dict for CSV/JSON export."""
        return {
            "MessageID": self.message_id,
            "Subject": self.subject,
            "SentAt": self.sent_at,
            "BouncedRecipients": ";".join(self.bounced_recipients),
            "BounceType": self.bounce_type,
            "BounceIDs": ";".join(str(i) for i in self.bounce_ids),
            "DumpAvailable": self.dump_available,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Candidate":
        def split(val) -> list[str]:
            if isinstance(val, list):
                return [str(v) for v in val if v != "" and v is not None]
            return [v for v in str(val or "").split(";") if v]

        dump = row.get("DumpAvailable", True)
        if isinstance(dump, str):
            dump = dump.strip().lower() in ("true", "1", "yes")
        return cls(
            message_id=row["MessageID"],
            subject=row.get("Subject", ""),
            sent_at=row.get("SentAt", ""),
            bounced_recipients=split(row.get("BouncedRecipients")),
            bounce_type=row.get("BounceType", ""),
            bounce_ids=[int(i) for i in split(row.get("BounceIDs"))],
            dump_available=bool(dump),
        )
