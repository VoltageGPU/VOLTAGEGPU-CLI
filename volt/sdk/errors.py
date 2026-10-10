"""Exceptions raised by the VoltageGPU SDK."""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

# Defence in depth: the API already answers in brand-neutral English, but an
# error relayed from an upstream system must never reach a terminal with an
# internal supplier name in it.
_INTERNAL_NAMES = re.compile(r"\b(?:celium|lium|bittensor|targon|datura)\b", re.IGNORECASE)
_DECENTRALIZED = re.compile(r"\bdecentrali[sz]ed\s*", re.IGNORECASE)
_MAX_MESSAGE = 600


def scrub(text: str) -> str:
    """Return ``text`` without control characters or internal supplier names, trimmed to a sane length."""
    text = "".join(ch for ch in text if not (ord(ch) < 32 or 127 <= ord(ch) < 160) or ch in "\t\n")
    cleaned = _DECENTRALIZED.sub("", _INTERNAL_NAMES.sub("provider", text))
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _MAX_MESSAGE:
        cleaned = cleaned[: _MAX_MESSAGE - 3] + "..."
    return cleaned


class VoltageGPUError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(VoltageGPUError):
    """The client cannot be configured (no API key, unsafe base URL...)."""


class ApiError(VoltageGPUError):
    """The API answered with an error, or could not be reached.

    ``status_code`` is 0 when no HTTP answer was received (timeout, DNS, TLS).
    """

    def __init__(self, status_code: int, message: str, payload: Optional[Mapping[str, Any]] = None) -> None:
        self.status_code = status_code
        self.message = scrub(message) or "Unknown error"
        self.payload: Mapping[str, Any] = payload or {}
        super().__init__(self.message)

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)

    @property
    def is_not_found(self) -> bool:
        return self.status_code == 404

    @classmethod
    def from_response(cls, status_code: int, body: Any) -> "ApiError":
        """Build an error from a JSON (or text) error body."""
        if isinstance(body, Mapping):
            error = body.get("error")
            detail = body.get("message")
            parts = [str(p) for p in (error, detail) if p and isinstance(p, (str, int, float))]
            if len(parts) == 2 and parts[1] in parts[0]:
                parts = parts[:1]
            if len(parts) == 2 and parts[0] in parts[1]:
                parts = parts[1:]
            message = ": ".join(parts) if parts else f"HTTP {status_code}"
            return cls(status_code, message, body)
        if isinstance(body, str) and body.strip() and not body.lstrip().startswith("<"):
            return cls(status_code, body.strip())
        return cls(status_code, f"HTTP {status_code}")


__all__ = ["VoltageGPUError", "ConfigError", "ApiError", "scrub"]
