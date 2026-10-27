"""One catalogue for ``vgpu ls`` and ``vgpu up``: every kind of machine, the price charged, cheapest first.

    vm         Confidential VM, Intel TDX, NVIDIA GPU     GET /confidential/vm/tiers (kind gpu)
    cpu        Confidential CPU, AMD SEV-SNP, no GPU      GET /confidential/vm/tiers (kind cpu)
    standard   standard GPU pod, no enclave               GET /volt/machines?provider=standard
    baremetal  dedicated machine, no enclave              GET /volt/machines?provider=dedicated
    sandbox    CPU sandbox, no enclave                    GET /pricing/snapshot (public price list)

A source that fails does not hide the others: its error is returned next to the offers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import ApiError
from .models import _int, _num, _str, clean_gpu_name
from .ttl import KINDS


@dataclass(frozen=True)
class Offer:
    kind: str
    tier: str  # what `vgpu up` takes: tier name, machine id or template name
    name: str
    gpu: Optional[str]
    gpu_count: int
    vcpu: Optional[int]
    memory_gb: Optional[int]
    price_per_hour: Optional[float]
    available: Optional[int]  # None = the source does not say
    confidential: bool
    tee: Optional[str]
    gpu_attestation: bool = False

    @property
    def in_stock(self) -> bool:
        return self.available is None or self.available > 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _list(data: Any, key: str) -> List[Mapping[str, Any]]:
    value = data.get(key) if isinstance(data, Mapping) else data
    return [d for d in value if isinstance(d, Mapping)] if isinstance(value, list) else []


def offers_from_vm_tiers(data: Any) -> List[Offer]:
    offers = []
    for t in _list(data, "tiers"):
        cpu = t.get("kind") == "cpu"
        offers.append(Offer(
            kind="cpu" if cpu else "vm",
            tier=str(t.get("resourceName") or ""),
            name=_str(t.get("displayName")) or str(t.get("resourceName") or ""),
            gpu=None if cpu else clean_gpu_name(_str(t.get("gpuType"))),
            gpu_count=0 if cpu else (_int(t.get("gpuCount")) or 1),
            vcpu=_int(t.get("vcpu")),
            memory_gb=_int(t.get("memoryGb")),
            price_per_hour=_num(t.get("pricePerHour")),
            available=_int(t.get("available")),
            confidential=True,
            tee="AMD SEV-SNP" if cpu else "Intel TDX",
            gpu_attestation=bool(t.get("gpuAttestation")),
        ))
    return offers


def offers_from_standard(data: Any) -> List[Offer]:
    return [Offer(
        kind="standard",
        tier=str(m.get("machine_id") or ""),
        name=_str(m.get("name")) or clean_gpu_name(_str(m.get("gpu_type"))),
        gpu=clean_gpu_name(_str(m.get("gpu_type"))),
        gpu_count=_int(m.get("gpu_count")) or 1,
        vcpu=_int(m.get("cpu_cores")),
        memory_gb=_int(m.get("ram_gb")),
        price_per_hour=_num(m.get("price_per_hour", m.get("price"))),
        available=_int(m.get("available_gpu_count")),
        confidential=False,
        tee=None,
    ) for m in _list(data, "items")]


def offers_from_dedicated(data: Any) -> List[Offer]:
    return [Offer(
        kind="baremetal",
        tier=str(m.get("machine_id") or m.get("resourceName") or ""),
        name=_str(m.get("name")) or str(m.get("resourceName") or ""),
        gpu=clean_gpu_name(_str(m.get("gpuModel"))),
        gpu_count=_int(m.get("gpuCount")) or 1,
        vcpu=_int(m.get("vcpu")),
        memory_gb=None,
        price_per_hour=_num(m.get("pricePerHour")),
        available=_int(m.get("available")),
        confidential=False,
        tee=None,
    ) for m in _list(data, "items")]


def offers_from_snapshot(data: Any) -> List[Offer]:
    box = data.get("sandboxes") if isinstance(data, Mapping) else None
    return [Offer(
        kind="sandbox",
        tier=str(t.get("resourceName") or ""),
        name=f"Sandbox {_str(t.get('name')) or ''}".strip(),
        gpu=None,
        gpu_count=0,
        vcpu=_int(t.get("vcpu")),
        memory_gb=_int(t.get("memoryGb")),
        price_per_hour=_num(t.get("pricePerHour")),
        available=_int(t.get("available")),
        confidential=False,
        tee=None,
    ) for t in _list(box or {}, "templates")]


def list_offers(client: Any, kinds: Iterable[str] = KINDS) -> Tuple[List[Offer], List[str]]:
    """Every offer of the requested kinds, cheapest first, plus one message per source that failed."""
    wanted = set(kinds)
    sources = []
    if wanted & {"vm", "cpu"}:
        sources.append(("Confidential VM tiers", lambda: offers_from_vm_tiers(client.call("GET", "/confidential/vm/tiers"))))
    if "standard" in wanted:
        sources.append(("standard machines",
                        lambda: offers_from_standard(client.call("GET", "/volt/machines", params={"provider": "standard"}))))
    if "baremetal" in wanted:
        sources.append(("dedicated machines",
                        lambda: offers_from_dedicated(client.call("GET", "/volt/machines", params={"provider": "dedicated"}))))
    if "sandbox" in wanted:
        sources.append(("sandboxes", lambda: offers_from_snapshot(client.call("GET", "/pricing/snapshot"))))
    offers: List[Offer] = []
    errors: List[str] = []
    for label, fetch in sources:
        try:
            offers += [o for o in fetch() if o.kind in wanted and o.tier]
        except ApiError as exc:
            if exc.is_auth_error:
                raise
            errors.append(f"{label}: {exc.message}")
    offers.sort(key=lambda o: (o.price_per_hour if o.price_per_hour is not None else float("inf"), o.kind, o.tier))
    return offers, errors


def filter_offers(offers: Iterable[Offer], *, gpu: Optional[str] = None, max_price: Optional[float] = None,
                  include_sold_out: bool = False) -> List[Offer]:
    needle = (gpu or "").strip().lower()
    result = []
    for o in offers:
        if needle and needle not in f"{o.gpu or ''} {o.name} {o.tier}".lower():
            continue
        if max_price is not None and (o.price_per_hour is None or o.price_per_hour > max_price):
            continue
        if not include_sold_out and not o.in_stock:
            continue
        result.append(o)
    return result


__all__ = ["Offer", "filter_offers", "list_offers", "offers_from_dedicated", "offers_from_snapshot", "offers_from_standard",
           "offers_from_vm_tiers"]
