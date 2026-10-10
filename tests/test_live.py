"""Read-only checks against the production API. Run with:

    VOLT_LIVE=1 VOLT_API_KEY=volt_... pytest -m live

Nothing here deploys or deletes anything.
"""

import os

import pytest

from volt.sdk.client import VoltageGPUClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("VOLT_LIVE") != "1" or not os.environ.get("VOLT_API_KEY"),
                       reason="set VOLT_LIVE=1 and VOLT_API_KEY to run live checks"),
]


@pytest.fixture(scope="module")
def live():
    with VoltageGPUClient() as client:
        yield client


def test_machines_both_kinds(live):
    machines = live.list_machines()
    kinds = {m.kind for m in machines}
    assert "standard" in kinds
    assert all(m.price_per_hour and m.price_per_hour > 0 for m in machines)
    assert all(m.id for m in machines)


def test_templates(live):
    templates = live.list_templates()
    assert templates and any(t.default for t in templates)


def test_pods_and_keys_are_readable(live):
    assert isinstance(live.list_pods(), list)
    assert isinstance(live.list_ssh_keys(), list)
