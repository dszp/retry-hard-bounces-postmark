"""Offline unit tests for the parsing, payload, discovery, config, and audit logic."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path

import pytest

from retry_bounces import audit, discovery, resend
from retry_bounces.config import op_read_command, resolve_secret
from retry_bounces.mime import parse_raw_message
from retry_bounces.models import Candidate, ParsedMessage


def _sample_raw_with_attachment() -> str:
    msg = EmailMessage()
    msg["From"] = "notify@example.com"
    msg["Reply-To"] = "notify@example.com"
    msg["To"] = "alice@example.com, bob@example.com"
    msg["Subject"] = "You have a new voicemail from (555) 010-1234"
    msg["Date"] = "Wed, 27 May 2026 18:39:19 +0000"
    msg.set_content("Listen to the attached voicemail.")
    msg.add_alternative("<html><body><h1>New voicemail</h1></body></html>", subtype="html")
    audio = b"RIFF....fake-wav-bytes...."
    msg.add_attachment(
        audio, maintype="audio", subtype="x-wav", filename="voicemail.wav"
    )
    return msg.as_string()


def test_parse_extracts_body_and_attachment():
    parsed = parse_raw_message(_sample_raw_with_attachment())
    assert parsed.from_addr == "notify@example.com"
    assert parsed.reply_to == "notify@example.com"
    assert "voicemail" in parsed.subject.lower()
    assert parsed.html_body and "<h1>New voicemail" in parsed.html_body
    assert parsed.text_body and "Listen to the attached" in parsed.text_body
    assert parsed.original_date == "Wed, 27 May 2026 18:39:19 +0000"

    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert att.name == "voicemail.wav"
    assert att.content_type == "audio/x-wav"
    assert base64.b64decode(att.content_base64) == b"RIFF....fake-wav-bytes...."


def test_build_payload_targets_and_attachments():
    parsed = parse_raw_message(_sample_raw_with_attachment())
    payload = resend.build_payload(
        parsed, ["bob@example.com"], stream="outbound"
    )
    assert payload["To"] == "bob@example.com"
    assert payload["From"] == "notify@example.com"
    assert payload["MessageStream"] == "outbound"
    assert payload["ReplyTo"] == "notify@example.com"
    assert payload["Attachments"][0]["Name"] == "voicemail.wav"
    assert payload["Attachments"][0]["ContentType"] == "audio/x-wav"
    assert "X-Original-Date" not in [h["Name"] for h in payload.get("Headers", [])]


def test_build_payload_multiple_targets_joined():
    parsed = parse_raw_message(_sample_raw_with_attachment())
    payload = resend.build_payload(
        parsed, ["a@x.com", "b@x.com"], stream="outbound"
    )
    assert payload["To"] == "a@x.com, b@x.com"


def test_build_payload_requires_targets():
    parsed = parse_raw_message(_sample_raw_with_attachment())
    with pytest.raises(ValueError):
        resend.build_payload(parsed, [], stream="outbound")


def test_date_note_injected_into_body_and_header():
    parsed = parse_raw_message(_sample_raw_with_attachment())
    payload = resend.build_payload(
        parsed, ["a@x.com"], stream="outbound", add_date_note=True
    )
    # 18:39:19 +0000 on May 27 (EDT, UTC-4) → 2:39 PM EDT
    assert "originally sent on May 27, 2026 at 2:39 PM EDT" in payload["HtmlBody"]
    assert "re-delivered after a mailbox delivery issue was resolved" in payload["HtmlBody"]
    # Banner is inserted just inside <body>, as an Outlook-safe table.
    assert payload["HtmlBody"].index("<body") < payload["HtmlBody"].index("originally sent")
    assert payload["HtmlBody"].index("<body") < payload["HtmlBody"].index("<table")
    # Header keeps the precise original RFC 2822 value.
    assert {"Name": "X-Original-Date", "Value": "Wed, 27 May 2026 18:39:19 +0000"} in payload["Headers"]
    assert payload["TextBody"].startswith("This message was originally sent on May 27, 2026 at 2:39 PM EDT")


def test_custom_note_text_with_date_placeholder():
    parsed = parse_raw_message(_sample_raw_with_attachment())
    payload = resend.build_payload(
        parsed, ["a@example.com"], stream="outbound", add_date_note=True,
        note_text="Heads up — recorded {date}. Resending now.",
    )
    assert "Heads up — recorded May 27, 2026 at 2:39 PM EDT. Resending now." in payload["HtmlBody"]
    assert payload["TextBody"].startswith("Heads up — recorded May 27, 2026 at 2:39 PM EDT. Resending now.")


def test_render_note_default_and_custom():
    assert resend.render_note(None, "May 1, 2026 at 9:00 AM EDT").startswith(
        "This message was originally sent on May 1, 2026 at 9:00 AM EDT and is being re-delivered"
    )
    assert resend.render_note("sent {date}", "X") == "sent X"
    # A custom note without {date} is used verbatim.
    assert resend.render_note("no placeholder", "X") == "no placeholder"


def test_friendly_eastern_converts_utc_to_eastern_dst_aware():
    # Summer → EDT (UTC-4)
    assert resend.friendly_eastern("Mon, 27 Apr 2026 16:06:08 +0000") == "April 27, 2026 at 12:06 PM EDT"
    # Winter → EST (UTC-5)
    assert resend.friendly_eastern("Thu, 15 Jan 2026 16:06:08 +0000") == "January 15, 2026 at 11:06 AM EST"
    # Already-Eastern offset is respected
    assert resend.friendly_eastern("Fri, 22 May 2026 13:28:52 -0400") == "May 22, 2026 at 1:28 PM EDT"


def test_friendly_eastern_invalid_returns_none():
    assert resend.friendly_eastern("not a date") is None


def test_inject_note_without_body_tag_prepends():
    out = resend.inject_date_note_html("<p>hi</p>", "originally sent 2026-05-01")
    assert out.startswith("<table")
    assert "originally sent 2026-05-01" in out
    assert "<p>hi</p>" in out


def test_build_payload_requires_a_body():
    parsed = ParsedMessage(from_addr="x@y.com", subject="s", html_body=None, text_body=None)
    with pytest.raises(ValueError):
        resend.build_payload(parsed, ["a@x.com"], stream="outbound")


# -- discovery -------------------------------------------------------------- #
class _BounceDiscoveryClient:
    def __init__(self, bounces):
        self._bounces = bounces

    def iter_bounces(self, *, email_filter=None, bounce_type=None, fromdate=None, todate=None, page_size=500):
        for b in self._bounces:
            if email_filter and b.get("Email") != email_filter:
                continue
            if bounce_type and b.get("Type") != bounce_type:
                continue
            yield b


def _bounce(mid, email, btype, bid, subject="s", at="2026-05-01T00:00:00"):
    return {"MessageID": mid, "Email": email, "Type": btype, "ID": bid, "Subject": subject, "BouncedAt": at}


def test_find_candidates_groups_by_message_and_collects_bounce_ids():
    client = _BounceDiscoveryClient([
        _bounce("m1", "bob@example.com", "SMTPApiError", 101),
        _bounce("m1", "alice@example.com", "SMTPApiError", 102),
        _bounce("m2", "bob@example.com", "HardBounce", 200),
        _bounce("m3", "office@example.com", "AutoResponder", 300),   # excluded
        _bounce("m4", "x@example.com", "SpamComplaint", 400),        # excluded
    ])
    cands = discovery.find_candidates(client, domain="example.com")
    by_id = {c.message_id: c for c in cands}
    assert set(by_id) == {"m1", "m2"}  # AutoResponder + SpamComplaint excluded
    assert by_id["m1"].bounce_type == "SMTPApiError"
    assert by_id["m1"].bounced_recipients == ["bob@example.com", "alice@example.com"]
    assert by_id["m1"].bounce_ids == [101, 102]
    assert by_id["m2"].bounce_type == "HardBounce"
    assert by_id["m2"].bounce_ids == [200]


def test_find_candidates_domain_filter():
    client = _BounceDiscoveryClient([
        _bounce("m1", "a@example.com", "SMTPApiError", 1),
        _bounce("m2", "b@other.com", "SMTPApiError", 2),
    ])
    cands = discovery.find_candidates(client, domain="example.com")
    assert [c.message_id for c in cands] == ["m1"]


class _ContentClient:
    def __init__(self, message_dumps=None, bounce_dumps=None, bounce_contents=None, forbid_bounce=False):
        self._md = message_dumps or {}
        self._bd = bounce_dumps or {}
        self._bc = bounce_contents or {}
        self._forbid_bounce = forbid_bounce

    def message_dump(self, mid):
        return self._md.get(mid, "")

    def bounce_dump(self, bid):
        if self._forbid_bounce:
            raise AssertionError("bounce_dump must not be called for HardBounce")
        return self._bd.get(bid, "")

    def get_bounce(self, bid):
        if self._forbid_bounce:
            raise AssertionError("get_bounce must not be called for HardBounce")
        return {"Content": self._bc.get(bid, "")}


def test_fetch_source_hardbounce_uses_message_dump_only():
    client = _ContentClient(message_dumps={"m1": "RAW"}, forbid_bounce=True)
    cand = Candidate("m1", "s", "t", ["a@x.com"], bounce_type="HardBounce", bounce_ids=[1])
    assert resend.fetch_source(client, cand) == ("RAW", None)


def test_fetch_source_smtperror_uses_bounce_dump():
    client = _ContentClient(bounce_dumps={102: "RAWMIME"})
    cand = Candidate("m1", "s", "t", ["a@x.com", "b@x.com"], bounce_type="SMTPApiError", bounce_ids=[101, 102])
    # 101 has no dump -> falls through to 102
    assert resend.fetch_source(client, cand) == ("RAWMIME", None)


def test_fetch_source_smtperror_falls_back_to_content_field():
    client = _ContentClient(bounce_dumps={}, bounce_contents={101: "FROMCONTENT"})
    cand = Candidate("m1", "s", "t", ["a@x.com"], bounce_type="SMTPApiError", bounce_ids=[101])
    assert resend.fetch_source(client, cand) == ("FROMCONTENT", None)


def test_fetch_source_returns_detail_when_unrecoverable():
    client = _ContentClient()
    cand = Candidate("m1", "s", "t", ["a@x.com"], bounce_type="SMTPApiError", bounce_ids=[101])
    raw, detail = resend.fetch_source(client, cand)
    assert raw is None
    assert "no longer retained" in detail


def test_candidate_row_roundtrip():
    c = Candidate("m1", "subj", "2026-05-01", ["a@x.com", "b@x.com"], bounce_type="SMTPApiError", bounce_ids=[1, 2])
    restored = Candidate.from_row(c.to_row())
    assert restored == c


class _ReactivateClient:
    """Fake client where deleting a suppression is forbidden (to catch dry-run leaks)."""

    def __init__(self, suppressed):
        self._suppressed = suppressed  # dict addr -> reason

    def is_suppressed(self, email):
        reason = self._suppressed.get(email.lower())
        return {"SuppressionReason": reason} if reason else None

    def delete_suppressions(self, emails):
        raise AssertionError("delete_suppressions must not be called in dry-run")


def test_reactivate_dry_run_never_deletes():
    client = _ReactivateClient({"a@x.com": "HardBounce"})
    msgs: list[str] = []
    targets = resend.reactivate_recipients(
        client, ["a@x.com", "b@x.com"],
        confirm=lambda addr, reason: True,
        echo=msgs.append,
        dry_run=True,
    )
    # Both are returned as would-be targets; no delete attempted (no AssertionError).
    assert set(targets) == {"a@x.com", "b@x.com"}
    assert any("would remove a@x.com" in m for m in msgs)


def test_reactivate_skips_spam_complaint():
    client = _ReactivateClient({"a@x.com": "SpamComplaint"})
    targets = resend.reactivate_recipients(
        client, ["a@x.com"],
        confirm=lambda addr, reason: True,
        echo=lambda _m: None,
    )
    assert targets == []


# -- config ----------------------------------------------------------------- #
def test_resolve_secret_passthrough_for_raw_value():
    assert resolve_secret("raw-token-123") == "raw-token-123"


def test_op_read_command_with_and_without_account():
    assert op_read_command("op://V/I/f", None) == ["op", "read", "op://V/I/f"]
    assert op_read_command("op://V/I/f", "my-account.1password.com") == [
        "op", "read", "--account", "my-account.1password.com", "op://V/I/f",
    ]


# -- audit ------------------------------------------------------------------ #
def test_audit_roundtrip(tmp_path: Path):
    log = tmp_path / "resent.jsonl"
    assert audit.previously_resent("m1", "a@x.com", log_path=log) is False
    audit.record_resend(
        message_id="m1", recipients=["a@x.com"], postmark_message_id="pm1",
        test_recipient=None, log_path=log,
    )
    assert audit.previously_resent("m1", "a@x.com", log_path=log) is True
    assert audit.previously_resent("m1", "b@x.com", log_path=log) is False


def test_audit_ignores_test_sends(tmp_path: Path):
    log = tmp_path / "resent.jsonl"
    audit.record_resend(
        message_id="m1", recipients=["me@test.com"], postmark_message_id="pm1",
        test_recipient="me@test.com", log_path=log,
    )
    assert audit.previously_resent("m1", "me@test.com", log_path=log) is False
