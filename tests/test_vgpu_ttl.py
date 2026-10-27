"""--ttl: parsing and translation to each product's own auto-stop field, with its bounds."""

from __future__ import annotations

import pytest

from volt.sdk.ttl import TtlError, parse_ttl, plan_ttl


@pytest.mark.parametrize("text,seconds", [
    ("90s", 90), ("45m", 2700), ("1h", 3600), ("6h", 21600), ("2d", 172800), ("1h30m", 5400), (" 2H ", 7200),
])
def test_parse_ttl(text: str, seconds: int) -> None:
    assert parse_ttl(text) == seconds


@pytest.mark.parametrize("text", ["", "0h", "-1h", "1", "h", "1w", "1h;rm", "1.5h", "31d", "1h 30m", "1m1m1m1m1m"])
def test_parse_ttl_refuses(text: str) -> None:
    with pytest.raises(TtlError):
        parse_ttl(text)


def test_vm_and_cpu_use_hardstop_hours_never_shorter() -> None:
    plan = plan_ttl("vm", 3600)
    assert plan.fields == {"hardstop_hours": 1.0}
    assert plan.effective_seconds == 3600
    odd = plan_ttl("cpu", 100)  # rounded up to the hundredth of an hour, never down
    assert odd.fields["hardstop_hours"] == 0.03
    assert odd.effective_seconds >= 100


def test_standard_uses_whole_auto_terminate_hours() -> None:
    assert plan_ttl("standard", 5400).fields == {"auto_terminate_hours": 2}
    assert plan_ttl("standard", 5400).effective_seconds == 7200


def test_baremetal_uses_hardstop_hours_within_72() -> None:
    assert plan_ttl("baremetal", 3600 * 72).fields == {"hardstop_hours": 72}
    with pytest.raises(TtlError, match="72h"):
        plan_ttl("baremetal", 3600 * 73)


def test_sandbox_uses_delete_timeout_after_idle_timeout() -> None:
    plan = plan_ttl("sandbox", 3600)
    assert plan.fields == {"deleteTimeoutSec": 3600, "idleTimeoutSec": 300}
    short = plan_ttl("sandbox", 90)
    assert short.fields["idleTimeoutSec"] < short.fields["deleteTimeoutSec"]
    assert short.fields["idleTimeoutSec"] >= 30
    with pytest.raises(TtlError):
        plan_ttl("sandbox", 8 * 86400)


@pytest.mark.parametrize("kind,seconds", [("standard", 1800), ("baremetal", 600), ("vm", 30), ("vm", 721 * 3600), ("sandbox", 60)])
def test_out_of_bounds_is_refused_locally(kind: str, seconds: int) -> None:
    with pytest.raises(TtlError):
        plan_ttl(kind, seconds)


def test_unknown_kind() -> None:
    with pytest.raises(TtlError):
        plan_ttl("gpu", 3600)
