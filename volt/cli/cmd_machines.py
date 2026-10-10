"""`voltagegpu machines` and `voltagegpu templates`."""

from __future__ import annotations

from typing import List, Optional

import click

from ..sdk.models import CONFIDENTIAL, STANDARD, Machine
from .output import echo_json, hint, info, money, table
from .state import AppState, pass_state


def filter_machines(
    machines: List[Machine],
    gpu: Optional[str] = None,
    max_price: Optional[float] = None,
    min_gpus: Optional[int] = None,
) -> List[Machine]:
    """Pure filter used by `machines` and `deploy standard --cheapest`."""
    needle = (gpu or "").lower().replace(" ", "")
    out = []
    for m in machines:
        if needle and needle not in f"{m.gpu_type} {m.name}".lower().replace(" ", ""):
            continue
        if max_price is not None and (m.price_per_hour is None or m.price_per_hour > max_price):
            continue
        if min_gpus is not None and m.gpu_count < min_gpus:
            continue
        out.append(m)
    return out


def _print_confidential(machines: List[Machine]) -> None:
    table(
        "Confidential VMs (Intel TDX)",
        [("TIER", {"style": "cyan"}), ("GPU",), ("GPUS", {"justify": "right"}), ("VCPU", {"justify": "right"}),
         ("RAM GB", {"justify": "right"}), ("$/HOUR", {"justify": "right", "style": "green"}), ("AVAILABLE", {"justify": "right"})],
        [(m.id, m.gpu_type, m.gpu_count, m.vcpu, m.ram_gb, money(m.price_per_hour), m.available) for m in machines],
    )


def _print_standard(machines: List[Machine], total: int) -> None:
    title = "Standard machines (no enclave, for non-sensitive workloads)"
    if total > len(machines):
        title += f", {len(machines)} of {total} shown"
    table(
        title,
        [("MACHINE ID", {"style": "cyan", "no_wrap": True}), ("GPU", {"no_wrap": True}), ("GPUS", {"justify": "right"}),
         ("VRAM GB", {"justify": "right"}), ("$/HOUR", {"justify": "right", "style": "green"}),
         ("LOCATION", {"no_wrap": True, "max_width": 22, "overflow": "ellipsis"})],
        [(m.id, m.gpu_type, m.gpu_count, m.gpu_memory_gb, money(m.price_per_hour), m.location) for m in machines],
    )


@click.command("machines")
@click.option("--standard", "kind", flag_value=STANDARD, help="Standard machines only (no enclave).")
@click.option("--confidential", "kind", flag_value=CONFIDENTIAL, help="Confidential VM tiers only (Intel TDX).")
@click.option("--gpu", help="Filter by GPU model, e.g. H100, A6000, 4090.")
@click.option("--max-price", type=float, help="Maximum price per hour (USD).")
@click.option("--min-gpus", type=click.IntRange(1, 64), help="Minimum number of GPUs.")
@click.option("--limit", type=click.IntRange(1, 1000), default=15, show_default=True, help="Rows per kind.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (no row limit).")
@pass_state
def machines_cmd(state: AppState, kind: Optional[str], gpu: Optional[str], max_price: Optional[float],
                 min_gpus: Optional[int], limit: int, as_json: bool) -> None:
    """List deployable machines with the hourly price you are charged."""
    found = filter_machines(state.client().list_machines(kind or "all"), gpu, max_price, min_gpus)
    if as_json:
        echo_json([m.to_dict() for m in found])
        return
    conf = [m for m in found if m.confidential]
    std = [m for m in found if not m.confidential]
    if not found:
        info("No machine matches these filters right now.")
        return
    if conf:
        _print_confidential(conf[:limit])
        hint("Deploy: voltagegpu deploy confidential --tier TIER")
    if conf and std:
        info("")
    if std:
        _print_standard(std[:limit], len(std))
        hint("Deploy: voltagegpu deploy standard --machine MACHINE_ID   (or --cheapest)")
    hint("Prices are per hour for the whole machine. The first hour is charged at deploy, then per second; "
         "the unused part is refunded when you stop a standard pod.")


@click.command("templates")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_state
def templates_cmd(state: AppState, as_json: bool) -> None:
    """List the images available for standard pods (--template of `deploy standard`)."""
    items = state.client().list_templates()
    if as_json:
        echo_json([t.to_dict() for t in items])
        return
    table(
        "Standard pod templates",
        [("TEMPLATE ID", {"style": "cyan", "no_wrap": True}), ("NAME",), ("DEFAULT", {"justify": "center"})],
        [(t.id, t.name, "yes" if t.default else "") for t in items],
    )
    hint("Confidential VMs run Ubuntu 24.04 LTS; they take no template.")
