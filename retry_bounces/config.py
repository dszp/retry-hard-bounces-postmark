"""Configuration and secret resolution.

The Postmark server token may be supplied either as a raw token or as a
1Password secret reference (``op://Vault/Item/field``). References are resolved
at runtime via the ``op`` CLI and never written to disk.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_API_BASE = "https://api.postmarkapp.com"
DEFAULT_STREAM = "outbound"


class ConfigError(Exception):
    """Raised when configuration is missing or a secret cannot be resolved."""


def op_read_command(reference: str, account: str | None) -> list[str]:
    """Build the ``op read`` argv, optionally pinning a specific 1Password account.

    ``account`` may be a sign-in address (e.g. ``my-account.1password.com``), an
    account shorthand, or an account/user UUID — whatever ``op --account`` accepts.
    """
    cmd = ["op", "read"]
    if account:
        cmd += ["--account", account]
    cmd.append(reference)
    return cmd


def resolve_secret(value: str, account: str | None = None) -> str:
    """Resolve a config value, expanding a 1Password ``op://`` reference if present.

    A plain value is returned unchanged. An ``op://`` reference is resolved by
    shelling out to ``op read`` (optionally against ``account``); failures raise
    :class:`ConfigError` with actionable guidance.
    """
    value = value.strip()
    if not value.startswith("op://"):
        return value

    if shutil.which("op") is None:
        raise ConfigError(
            "POSTMARK_SERVER_TOKEN is a 1Password reference but the `op` CLI was "
            "not found. Install the 1Password CLI (https://developer.1password.com/docs/cli/) "
            "or set a raw token instead."
        )

    cmd = op_read_command(value, account)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - env dependent
        raise ConfigError(f"Failed to run `op read`: {exc}") from exc

    if result.returncode != 0:
        stderr = result.stderr.strip() or "unknown error"
        hint = (
            "Make sure the 1Password CLI is signed in (`op signin`) and the "
            "reference path is correct."
        )
        if not account:
            hint += (
                " If you have multiple 1Password accounts, set OP_ACCOUNT in .env "
                "to the right one (e.g. your sign-in address)."
            )
        raise ConfigError(f"`op read` failed for {value!r}: {stderr}. {hint}")

    token = result.stdout.strip()
    if not token:
        raise ConfigError(f"`op read` returned an empty value for {value!r}.")
    return token


@dataclass
class Config:
    """Runtime configuration with the resolved Postmark server token."""

    server_token: str
    stream: str = DEFAULT_STREAM
    api_base: str = DEFAULT_API_BASE

    @classmethod
    def load(cls, *, stream: str | None = None, api_base: str | None = None) -> "Config":
        """Load configuration from environment (and a ``.env`` file if present).

        ``stream`` and ``api_base`` override the corresponding env vars / defaults
        when provided (e.g. from a CLI flag).
        """
        load_dotenv()

        raw = os.environ.get("POSTMARK_SERVER_TOKEN")
        if not raw:
            raise ConfigError(
                "POSTMARK_SERVER_TOKEN is not set. Copy .env.example to .env and set "
                "it (raw token or an op:// reference)."
            )
        token = resolve_secret(raw, account=os.environ.get("OP_ACCOUNT") or None)

        return cls(
            server_token=token,
            stream=stream or os.environ.get("POSTMARK_MESSAGE_STREAM") or DEFAULT_STREAM,
            api_base=api_base or os.environ.get("POSTMARK_API_BASE") or DEFAULT_API_BASE,
        )
