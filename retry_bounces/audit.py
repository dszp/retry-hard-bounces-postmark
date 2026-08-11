"""Append-only audit log of resends, used to warn about duplicates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOG = Path("resent.jsonl")


def record_resend(
    *,
    message_id: str,
    recipients: list[str],
    postmark_message_id: str | None,
    test_recipient: str | None,
    redirected_to: str | None = None,
    log_path: Path = DEFAULT_LOG,
) -> None:
    """Append one resend record. Only real (non-test) sends should be recorded.

    ``recipients`` is always the *original* bounced address(es) — that is the key
    :func:`previously_resent` de-duplicates on, so a redirected resend still
    suppresses a second attempt at the same (message, original recipient) pair.
    ``redirected_to`` records where the mail was actually delivered instead.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_message_id": message_id,
        "recipients": recipients,
        "postmark_message_id": postmark_message_id,
        "test_recipient": test_recipient,
    }
    if redirected_to:
        entry["redirected_to"] = redirected_to
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def previously_resent(message_id: str, recipient: str, log_path: Path = DEFAULT_LOG) -> bool:
    """True if this (message, recipient) pair was already resent for real."""
    if not log_path.exists():
        return False
    recipient = recipient.lower()
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("test_recipient"):
                continue  # test sends don't count
            if entry.get("source_message_id") != message_id:
                continue
            recips = [r.lower() for r in entry.get("recipients", [])]
            if recipient in recips:
                return True
    return False
