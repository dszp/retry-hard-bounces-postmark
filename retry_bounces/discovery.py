"""Find bounced voicemail messages worth resending.

Strategy: query Postmark's Bounces API, which records one entry per recipient
that failed on a message — including the original ``HardBounce`` and the
subsequent ``SMTPApiError`` (blocked-while-inactive) sends. Group those by
message; each group is a resend candidate carrying the bounced recipients and
the bounce IDs used to recover the original content.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import chain

from .api import PostmarkClient
from .models import Candidate

# Bounce types we resend. HardBounce = the original failure; SMTPApiError =
# voicemails blocked because the (now-reactivated) recipient was inactive;
# Blocked = ISP-level block (treated like SMTPApiError, logged when seen).
RESEND_TYPES = {"HardBounce", "SMTPApiError", "Blocked"}

# Deliberately excluded, with reasons:
#   AutoResponder       - vacation/auto-reply, not a delivery failure (per user)
#   SpamComplaint       - cannot be reactivated; must not be resent
#   Transient           - soft bounce; Postmark already retries automatically
#   ManuallyDeactivated - intentionally turned off by an admin
EXCLUDED_TYPES = {"AutoResponder", "SpamComplaint", "Transient", "ManuallyDeactivated"}

# Excluded by default but opt-in-able via ``extra_types``. A Transient bounce is
# normally Postmark's own business (it retries), but a *permanent* fault that the
# remote reports as temporary — e.g. an NXDOMAIN typo'd recipient domain, which
# retries until "QUEUE.Expired" — never gets delivered and is a legitimate resend
# (usually with --redirect-to, since the original address is unreachable by design).
OPT_IN_TYPES = {"Transient"}

# SpamComplaint must never be resent: it cannot be reactivated and resending is
# an abuse vector, so it stays excluded even when explicitly requested.
NEVER_TYPES = {"SpamComplaint"}


def _from_datetime(days: int) -> str:
    # Bounces API filters by Eastern-time datetime; widen by a day for TZ slack.
    return (datetime.now() - timedelta(days=days + 1)).strftime("%Y-%m-%dT00:00:00")


def resolve_types(extra_types: list[str] | None) -> set[str]:
    """The set of bounce types to resend, widened by opt-in ``extra_types``.

    Raises :class:`ValueError` for a type that must never be resent
    (:data:`NEVER_TYPES`), so a typo or a bad flag fails loudly rather than
    silently mailing someone who filed a spam complaint.
    """
    types = set(RESEND_TYPES)
    for raw in extra_types or []:
        name = raw.strip()
        if name in NEVER_TYPES:
            raise ValueError(f"{name} can never be resent (it cannot be reactivated).")
        types.add(name)
    return types


def find_candidates(
    client: PostmarkClient,
    *,
    domain: str | None = None,
    recipients: list[str] | None = None,
    days: int = 45,
    extra_types: list[str] | None = None,
    progress=None,
) -> list[Candidate]:
    """Return resend candidates grouped by message, sourced from the Bounces API.

    ``recipients`` narrows to specific addresses (one bounce query each);
    otherwise all bounces in the window are scanned and filtered to ``domain``
    client-side. ``extra_types`` widens :data:`RESEND_TYPES` (see
    :data:`OPT_IN_TYPES`). ``progress`` is an optional ``(text)`` status callback.
    """
    resend_types = resolve_types(extra_types)
    fromdate = _from_datetime(days)
    suffix = ("@" + domain.lstrip("@").lower()) if domain else None

    if recipients:
        if progress:
            progress(f"Querying bounces for {len(recipients)} recipient(s)…")
        sources = chain.from_iterable(
            client.iter_bounces(email_filter=addr, fromdate=fromdate) for addr in recipients
        )
    else:
        if progress:
            progress("Scanning all bounces in window…")
        sources = client.iter_bounces(fromdate=fromdate)

    candidates: dict[str, Candidate] = {}
    for b in sources:
        btype = b.get("Type", "")
        if btype not in resend_types:
            continue
        email = b.get("Email", "")
        if suffix and not email.lower().endswith(suffix):
            continue
        mid = b.get("MessageID")
        if not mid:
            continue

        cand = candidates.get(mid)
        if cand is None:
            cand = Candidate(
                message_id=mid,
                subject=b.get("Subject", "") or "",
                sent_at=b.get("BouncedAt", "") or "",
                bounced_recipients=[],
                bounce_type=btype,
                bounce_ids=[],
            )
            candidates[mid] = cand
        if email and email not in cand.bounced_recipients:
            cand.bounced_recipients.append(email)
            bounce_id = b.get("ID")
            if bounce_id is not None:
                cand.bounce_ids.append(int(bounce_id))

    return sorted(candidates.values(), key=lambda c: c.sent_at)
