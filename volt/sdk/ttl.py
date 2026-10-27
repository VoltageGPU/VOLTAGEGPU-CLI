"""``--ttl``: one duration for the user, each product's own auto-stop field for the API.

Every machine started by ``vgpu up`` has a deadline. The value is converted to
the product's unit by rounding UP (a machine never stops earlier than asked),
then checked against the bounds the server enforces, so an out-of-range value
is refused here, before anything is sent or charged.

    vm, cpu    Confidential VM (Intel TDX) and Confidential CPU (AMD SEV-SNP): hardstop_hours, up to 720 h
    standard   standard GPU pod: auto_terminate_hours, whole hours, 1 to 720
    baremetal  dedicated machine: hardstop_hours, whole hours, 1 to 72
    sandbox    sandbox: deleteTimeoutSec, after the idle timeout, up to 7 days
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

KINDS = ("vm", "cpu", "standard", "baremetal", "sandbox")

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
# Units in this order, each at most once: 2d, 1h30m, 45m, 90s.
_TTL_RE = re.compile(r"^(?:(\d{1,4})d)?(?:(\d{1,5})h)?(?:(\d{1,6})m)?(?:(\d{1,7})s)?$")
_MAX_TTL = 30 * 86400

# (minimum, maximum) in seconds, as enforced by the server for each product.
BOUNDS: Mapping[str, Tuple[int, int]] = {
    "vm": (60, 720 * 3600),
    "cpu": (60, 720 * 3600),
    "standard": (3600, 720 * 3600),
    "baremetal": (3600, 72 * 3600),
    "sandbox": (61, 7 * 86400),
}

# Sandboxes pause after this much inactivity (server default), always before the deadline.
_SANDBOX_IDLE_SEC = 300
_SANDBOX_IDLE_MIN_SEC = 30


class TtlError(ValueError):
    """The --ttl value cannot be read, or is outside what the product accepts."""


@dataclass(frozen=True)
class TtlPlan:
    kind: str
    requested_seconds: int
    effective_seconds: int
    fields: Dict[str, Any]


def parse_ttl(text: str) -> int:
    """``45m``, ``6h``, ``2d``, ``90s``, ``1h30m`` -> seconds (at most 30 days)."""
    value = (text or "").strip().lower()
    match = _TTL_RE.fullmatch(value)
    if not value or not match:
        raise TtlError(f"Invalid --ttl {text!r}: use a duration such as 45m, 1h, 1h30m, 6h or 2d.")
    seconds = sum(int(n) * _UNITS[u] for n, u in zip(match.groups(), "dhms", strict=True) if n)
    if seconds <= 0:
        raise TtlError("--ttl must be longer than zero: every machine needs a deadline.")
    if seconds > _MAX_TTL:
        raise TtlError("--ttl cannot exceed 30d.")
    return seconds


def _human(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def plan_ttl(kind: str, seconds: int) -> TtlPlan:
    """The API fields for ``kind`` with a deadline of at least ``seconds``."""
    if kind not in BOUNDS:
        raise TtlError(f"Unknown machine kind {kind!r}; expected one of {', '.join(KINDS)}.")
    low, high = BOUNDS[kind]
    if not low <= seconds <= high:
        raise TtlError(f"--ttl for {kind} must be between {_human(low)} and {_human(high)} (got {_human(seconds)}).")
    if kind in ("vm", "cpu"):
        hours = math.ceil(seconds / 36) / 100  # hundredths of an hour, rounded up
        fields: Dict[str, Any] = {"hardstop_hours": hours}
        effective = max(seconds, round(hours * 3600))
    elif kind == "standard":
        whole = math.ceil(seconds / 3600)
        fields = {"auto_terminate_hours": whole}
        effective = whole * 3600
    elif kind == "baremetal":
        whole = math.ceil(seconds / 3600)
        fields = {"hardstop_hours": whole}
        effective = whole * 3600
    else:
        idle = max(_SANDBOX_IDLE_MIN_SEC, min(_SANDBOX_IDLE_SEC, seconds - 30))
        fields = {"deleteTimeoutSec": seconds, "idleTimeoutSec": idle}
        effective = seconds
    if effective > high:
        raise TtlError(f"--ttl for {kind} must be at most {_human(high)}.")
    return TtlPlan(kind=kind, requested_seconds=seconds, effective_seconds=effective, fields=fields)


__all__ = ["BOUNDS", "KINDS", "TtlError", "TtlPlan", "parse_ttl", "plan_ttl"]
