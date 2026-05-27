"""Thin Postmark REST client.

Wraps only the endpoints this tool needs: outbound message search/details/dump,
suppression dump/delete, and single-email send. A single Postmark *Server* token
authorizes all of them.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import httpx


class PostmarkError(Exception):
    """A non-success response from the Postmark API."""

    def __init__(self, status_code: int, error_code: int | None, message: str):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(f"Postmark API error (HTTP {status_code}, code {error_code}): {message}")


class PostmarkClient:
    """Minimal client for the Postmark Messages, Suppressions, and Email APIs."""

    def __init__(self, server_token: str, stream: str, api_base: str, timeout: float = 30.0):
        self.stream = stream
        self._client = httpx.Client(
            base_url=api_base.rstrip("/"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Postmark-Server-Token": server_token,
            },
            timeout=timeout,
        )

    def __enter__(self) -> "PostmarkClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- internal -------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        # Retry on HTTP 429 (rate limited), honoring Retry-After when present.
        for attempt in range(3):
            resp = self._client.request(method, path, **kwargs)
            if resp.status_code != 429:
                break
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 2.0 * (attempt + 1)
            except ValueError:
                wait = 2.0 * (attempt + 1)
            time.sleep(wait)
        if resp.status_code >= 400:
            error_code: int | None = None
            message = resp.text
            try:
                body = resp.json()
                error_code = body.get("ErrorCode")
                message = body.get("Message", message)
            except ValueError:
                pass
            raise PostmarkError(resp.status_code, error_code, message)
        if not resp.content:
            return {}
        return resp.json()

    # -- messages -------------------------------------------------------------

    def search_outbound(
        self,
        *,
        recipient: str | None = None,
        fromdate: str | None = None,
        todate: str | None = None,
        count: int = 500,
        offset: int = 0,
    ) -> dict:
        """One page of outbound message search results."""
        params: dict[str, Any] = {
            "count": count,
            "offset": offset,
            "messagestream": self.stream,
        }
        if recipient:
            params["recipient"] = recipient
        if fromdate:
            params["fromdate"] = fromdate
        if todate:
            params["todate"] = todate
        return self._request("GET", "/messages/outbound", params=params)

    def iter_outbound(
        self,
        *,
        recipient: str | None = None,
        fromdate: str | None = None,
        todate: str | None = None,
        page_size: int = 500,
    ) -> Iterator[dict]:
        """Iterate all outbound messages matching the filters, handling paging."""
        offset = 0
        while True:
            page = self.search_outbound(
                recipient=recipient,
                fromdate=fromdate,
                todate=todate,
                count=page_size,
                offset=offset,
            )
            messages = page.get("Messages", [])
            for msg in messages:
                yield msg
            total = page.get("TotalCount", 0)
            offset += page_size
            if offset >= total or not messages:
                break

    def message_details(self, message_id: str) -> dict:
        return self._request("GET", f"/messages/outbound/{message_id}/details")

    def message_dump(self, message_id: str) -> str:
        """Raw MIME source of a message ('' if no dump is available)."""
        data = self._request("GET", f"/messages/outbound/{message_id}/dump")
        return data.get("Body", "") or ""

    # -- suppressions ---------------------------------------------------------

    def dump_suppressions(
        self,
        *,
        reason: str | None = None,
        origin: str | None = None,
        email: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {}
        if reason:
            params["SuppressionReason"] = reason
        if origin:
            params["Origin"] = origin
        if email:
            params["EmailAddress"] = email
        data = self._request(
            "GET",
            f"/message-streams/{self.stream}/suppressions/dump",
            params=params,
        )
        return data.get("Suppressions", [])

    def is_suppressed(self, email: str) -> dict | None:
        """Return the suppression record for ``email`` on this stream, or None."""
        for entry in self.dump_suppressions(email=email):
            if entry.get("EmailAddress", "").lower() == email.lower():
                return entry
        return None

    def delete_suppressions(self, emails: list[str]) -> list[dict]:
        """Delete (reactivate) suppressions. Returns per-address status objects."""
        body = {"Suppressions": [{"EmailAddress": e} for e in emails]}
        data = self._request(
            "POST",
            f"/message-streams/{self.stream}/suppressions/delete",
            json=body,
        )
        return data.get("Suppressions", [])

    # -- bounces --------------------------------------------------------------

    def search_bounces(
        self,
        *,
        email_filter: str | None = None,
        bounce_type: str | None = None,
        message_id: str | None = None,
        fromdate: str | None = None,
        todate: str | None = None,
        inactive: bool | None = None,
        count: int = 500,
        offset: int = 0,
    ) -> dict:
        """One page of bounce search results."""
        params: dict[str, Any] = {
            "count": count,
            "offset": offset,
            "messagestream": self.stream,
        }
        if email_filter:
            params["emailFilter"] = email_filter
        if bounce_type:
            params["type"] = bounce_type
        if message_id:
            params["messageID"] = message_id
        if fromdate:
            params["fromdate"] = fromdate
        if todate:
            params["todate"] = todate
        if inactive is not None:
            params["inactive"] = str(inactive).lower()
        return self._request("GET", "/bounces", params=params)

    def iter_bounces(
        self,
        *,
        email_filter: str | None = None,
        bounce_type: str | None = None,
        fromdate: str | None = None,
        todate: str | None = None,
        page_size: int = 500,
    ) -> Iterator[dict]:
        """Iterate all bounces matching the filters, handling paging."""
        offset = 0
        while True:
            page = self.search_bounces(
                email_filter=email_filter,
                bounce_type=bounce_type,
                fromdate=fromdate,
                todate=todate,
                count=page_size,
                offset=offset,
            )
            bounces = page.get("Bounces", [])
            for bounce in bounces:
                yield bounce
            total = page.get("TotalCount", 0)
            offset += page_size
            if offset >= total or not bounces:
                break

    def get_bounce(self, bounce_id: int | str) -> dict:
        return self._request("GET", f"/bounces/{bounce_id}")

    def bounce_dump(self, bounce_id: int | str) -> str:
        """Raw source retained for a bounce ('' if none; bounce dumps expire at 30 days)."""
        data = self._request("GET", f"/bounces/{bounce_id}/dump")
        return data.get("Body", "") or ""

    # -- send -----------------------------------------------------------------

    def send_email(self, payload: dict) -> dict:
        """Send a single email via POST /email."""
        return self._request("POST", "/email", json=payload)
