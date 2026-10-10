"""Typed, immutable views of the API payloads.

Every ``from_api`` is tolerant: a missing field becomes None rather than an
exception, so a new server field or a partial answer never crashes the CLI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .ssh import safe_ssh_command

STANDARD = "standard"
CONFIDENTIAL = "confidential"


def _num(value: Any) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n else None  # NaN guard


def _int(value: Any) -> Optional[int]:
    n = _num(value)
    return int(n) if n is not None else None


def strip_controls(text: str) -> str:
    """Drop C0/C1 control characters (ESC sequences, BEL...) from server text,
    so nothing the API returns can drive the terminal."""
    return "".join(ch for ch in text if ch in "\t\n" or not (ord(ch) < 32 or 127 <= ord(ch) < 160))


def _str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = strip_controls(str(value)).strip()
    return text or None


def clean_gpu_name(name: Optional[str]) -> str:
    """'NVIDIA-H100' / 'NVIDIA GeForce RTX 3090' -> 'H100' / 'GeForce RTX 3090'."""
    text = (name or "").strip()
    for prefix in ("NVIDIA-", "NVIDIA "):
        if text.upper().startswith(prefix.upper()):
            text = text[len(prefix):]
    return text or "GPU"


def _kind(raw: Mapping[str, Any]) -> str:
    provider = str(raw.get("provider") or "").lower()
    if provider == STANDARD or raw.get("confidential_compute") is False:
        return STANDARD
    return CONFIDENTIAL


@dataclass(frozen=True)
class Machine:
    """A deployable machine: a standard machine or a Confidential VM tier."""

    id: str  # machine_id (standard) or tier name such as "h100-small" (confidential)
    kind: str
    name: str
    gpu_type: str
    gpu_count: int
    price_per_hour: Optional[float]
    price_per_gpu_hour: Optional[float] = None
    available: Optional[int] = None
    gpu_memory_gb: Optional[int] = None
    vcpu: Optional[int] = None
    ram_gb: Optional[int] = None
    location: Optional[str] = None
    cuda: Optional[str] = None

    @property
    def confidential(self) -> bool:
        return self.kind == CONFIDENTIAL

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Machine":
        kind = _kind(raw)
        gpu_count = _int(raw.get("gpu_count")) or 1
        if kind == STANDARD:
            loc = raw.get("location") or {}
            place = ", ".join(p for p in (_str(loc.get("city")), _str(loc.get("country_code"))) if p) if isinstance(loc, Mapping) else None
            return cls(
                id=str(raw.get("machine_id") or raw.get("id") or ""),
                kind=kind,
                name=_str(raw.get("name")) or clean_gpu_name(_str(raw.get("gpu_type"))),
                gpu_type=clean_gpu_name(_str(raw.get("gpu_type"))),
                gpu_count=gpu_count,
                price_per_hour=_num(raw.get("price_per_hour", raw.get("price"))),
                price_per_gpu_hour=_num(raw.get("price_per_gpu_hour")),
                available=_int(raw.get("available_gpu_count")),
                gpu_memory_gb=_int(raw.get("gpu_memory_gb")),
                vcpu=_int(raw.get("cpu_cores")),
                ram_gb=_int(raw.get("ram_gb")),
                location=place or None,
                cuda=_str(raw.get("max_cuda_version")),
            )
        price = _num(raw.get("price", raw.get("price_per_hour", raw.get("pricePerHour"))))
        vcpu_milli = _int(raw.get("vcpu"))
        memory_mb = _int(raw.get("memory"))
        name = _str(raw.get("name")) or _str(raw.get("displayName")) or ""
        return cls(
            id=str(raw.get("resource_name") or raw.get("resourceName") or ""),
            kind=kind,
            name=name.replace(" [Confidential VM]", ""),
            gpu_type=clean_gpu_name(_str(raw.get("gpu_type") or raw.get("gpuType"))),
            gpu_count=gpu_count if raw.get("gpu_count") is not None else (_int(raw.get("gpuCount")) or 1),
            price_per_hour=price,
            price_per_gpu_hour=(price / gpu_count) if price is not None and gpu_count else None,
            available=_int(raw.get("total_gpu_count", raw.get("available"))),
            vcpu=round(vcpu_milli / 1000) if vcpu_milli and vcpu_milli >= 1000 else vcpu_milli,
            ram_gb=round(memory_mb / 1000) if memory_mb and memory_mb >= 1000 else memory_mb,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "confidential": self.confidential}


@dataclass(frozen=True)
class Template:
    """A curated image for standard pods."""

    id: str
    name: str
    description: str = ""
    category: Optional[str] = None
    default: bool = False

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Template":
        return cls(
            id=str(raw.get("id") or raw.get("template_id") or ""),
            name=_str(raw.get("name")) or "",
            description=_str(raw.get("description")) or "",
            category=_str(raw.get("category")),
            default=bool(raw.get("default")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Pod:
    """A pod or Confidential VM owned by the caller."""

    id: str
    kind: str
    status: str
    name: Optional[str] = None
    gpu_type: Optional[str] = None
    gpu_count: Optional[int] = None
    hourly_price: Optional[float] = None
    ssh_command: Optional[str] = None
    ssh_ready: Optional[bool] = None
    provider_status: Optional[str] = None
    resource_name: Optional[str] = None
    jupyter_url: Optional[str] = None
    created_at: Optional[str] = None
    ssh_command_rejected: bool = False  # the API sent an SSH command of an unexpected form

    @property
    def confidential(self) -> bool:
        return self.kind == CONFIDENTIAL

    @property
    def reachable(self) -> bool:
        """True once the SSH command is usable."""
        if not self.ssh_command:
            return False
        return self.ssh_ready is not False

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Pod":
        kind = _kind(raw)
        gpu = _str(raw.get("gpu_type") or raw.get("gpuType"))
        raw_ssh = _str(raw.get("ssh_command"))
        ssh = safe_ssh_command(raw_ssh)
        return cls(
            id=str(raw.get("id") or raw.get("workloadUid") or raw.get("podId") or ""),
            kind=kind,
            status=_str(raw.get("status")) or "unknown",
            name=_str(raw.get("name")) if kind == STANDARD else _str(raw.get("resource_name")) or _str(raw.get("name")),
            gpu_type=clean_gpu_name(gpu) if gpu else None,
            gpu_count=_int(raw.get("gpu_count")),
            hourly_price=_num(raw.get("hourly_price", raw.get("hourlyPrice"))),
            ssh_command=ssh,
            ssh_ready=raw.get("ssh_ready") if isinstance(raw.get("ssh_ready"), bool) else None,
            provider_status=_str(raw.get("provider_status") or raw.get("phase")),
            resource_name=_str(raw.get("resource_name")),
            jupyter_url=_str(raw.get("jupyter_url")),
            created_at=_str(raw.get("created_at") or raw.get("createdAt")),
            ssh_command_rejected=bool(raw_ssh) and ssh is None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "confidential": self.confidential}


@dataclass(frozen=True)
class SshKey:
    id: str
    name: str
    public_key: str = ""
    created_at: Optional[str] = None

    @property
    def summary(self) -> str:
        """Key type and the last characters of the key, enough to recognise it."""
        parts = self.public_key.split()
        if len(parts) >= 2:
            return f"{parts[0]} ...{parts[1][-12:]}"
        return self.public_key[:24]

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "SshKey":
        return cls(
            id=str(raw.get("id") or ""),
            name=_str(raw.get("name")) or "",
            public_key=_str(raw.get("publicKey") or raw.get("public_key")) or "",
            created_at=_str(raw.get("createdAt") or raw.get("created_at")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Balance:
    balance: float
    currency: str = "USD"
    active_pods: Optional[int] = None
    top_up_url: str = "https://voltagegpu.com/billing"

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Balance":
        return cls(
            balance=_num(raw.get("balance")) or 0.0,
            currency=_str(raw.get("currency")) or "USD",
            active_pods=_int(raw.get("active_pods", raw.get("activePods"))),
            top_up_url=_str(raw.get("top_up_url")) or "https://voltagegpu.com/billing",
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DeployResult:
    """What a deploy returned. ``sudo_password`` is shown once and never stored."""

    pod_id: str
    kind: str
    hourly_price: Optional[float]
    status: str = "RUNNING"
    name: Optional[str] = None
    machine: Optional[str] = None
    prepaid_amount: Optional[float] = None
    message: Optional[str] = None
    sudo_password: Optional[str] = field(default=None, repr=False)
    hard_stop_at: Optional[str] = None
    record_id: Optional[str] = None
    attestation: Optional[Mapping[str, Any]] = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "DeployResult":
        pod = raw.get("pod")
        if isinstance(pod, Mapping):  # standard pods (and the legacy confidential shape)
            return cls(
                pod_id=str(pod.get("id") or pod.get("workloadUid") or ""),
                kind=_kind(pod),
                hourly_price=_num(pod.get("hourly_price", pod.get("hourlyPrice"))),
                status=_str(pod.get("status")) or "RUNNING",
                name=_str(pod.get("name")),
                machine=_str(pod.get("machine_id") or pod.get("resource_name")),
                prepaid_amount=_num(pod.get("prepaid_amount")),
                message=_str(raw.get("message")),
            )
        # Confidential VM: { podId, workloadUid, resourceName, hourlyPrice, sudoPassword, ... }
        attestation = raw.get("attestation")
        return cls(
            pod_id=str(raw.get("workloadUid") or raw.get("podId") or ""),
            kind=CONFIDENTIAL,
            hourly_price=_num(raw.get("hourlyPrice")),
            status="BOOTING",
            machine=_str(raw.get("resourceName")),
            prepaid_amount=_num(raw.get("hourlyPrice")),
            message=_str(raw.get("message")),
            sudo_password=_str(raw.get("sudoPassword")),
            hard_stop_at=_str(raw.get("hardStopAt")),
            record_id=_str(raw.get("podId")),
            attestation=attestation if isinstance(attestation, Mapping) else None,
        )

    def to_dict(self, include_secret: bool = True) -> Dict[str, Any]:
        data = asdict(self)
        if not include_secret:
            data.pop("sudo_password", None)
        if self.attestation is not None:
            data["attestation"] = dict(self.attestation)
        return data


@dataclass(frozen=True)
class ReleaseResult:
    pod_id: str
    prepaid_refund: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


MachineList = Tuple[Machine, ...]

__all__ = [
    "Balance",
    "CONFIDENTIAL",
    "DeployResult",
    "Machine",
    "Pod",
    "ReleaseResult",
    "STANDARD",
    "SshKey",
    "Template",
    "clean_gpu_name",
]
