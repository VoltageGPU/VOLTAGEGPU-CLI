"""Start, list and release machines of every kind for ``vgpu`` and the local MCP server.

Names: the API does not keep the name chosen at ``up`` for every product, so
``vgpu`` records ``name -> machine id`` in ``~/.volt/vgpu-names.json`` (mode
0600, ids only, never a key or a password). Any command also accepts the id.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .catalog import Offer
from .client import _path_id
from .errors import ApiError
from .models import _num, _str
from .ssh import SshTarget, UnsafeSshCommand, parse_ssh_command
from .ttl import TtlPlan

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
DEFAULT_SANDBOX_IMAGE = "ubuntu-24.04"
_GONE = ("STOPPED", "TERMINATED", "FAILED", "DELETED", "RELEASED")


def check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError("--name must be 1 to 32 lowercase letters, digits or hyphens, starting with a letter or digit.")
    return name


class NameStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path.home() / ".volt" / "vgpu-names.json"

    def load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict) and NAME_RE.fullmatch(k)} if isinstance(data, dict) else {}

    def _save(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)

    def put(self, name: str, record: Mapping[str, Any]) -> None:
        self._save({**self.load(), name: dict(record)})

    def remove_id(self, pod_id: str) -> None:
        data = self.load()
        kept = {k: v for k, v in data.items() if v.get("id") != pod_id}
        if kept != data:
            self._save(kept)


@dataclass(frozen=True)
class Instance:
    id: str
    kind: str
    status: str
    name: Optional[str] = None
    tier: Optional[str] = None
    hourly_price: Optional[float] = None
    ssh_ready: bool = False
    ssh: Optional[str] = None
    confidential: bool = False
    hard_stop_at: Optional[str] = None

    @property
    def target(self) -> Optional[SshTarget]:
        if not self.ssh or not self.ssh_ready:
            return None
        try:
            return parse_ssh_command(self.ssh)
        except UnsafeSshCommand:
            return None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe_ssh(raw: Any) -> Optional[str]:
    """The validated form of a server SSH command, or None (a tampered one is never shown)."""
    text = _str(raw)
    if not text:
        return None
    try:
        return parse_ssh_command(text).display()
    except UnsafeSshCommand:
        return None


def _from_pod(raw: Mapping[str, Any]) -> Optional[Instance]:
    tier = str(raw.get("tier") or "")
    if tier == "sandbox":
        return None  # listed by /volt/sandboxes, with the id its own routes use
    resource = _str(raw.get("resource_name"))
    if raw.get("provider") == "standard" or (raw.get("confidential_compute") is False and tier != "dedicated"):
        kind = "standard"
    elif tier == "dedicated":
        kind = "baremetal"
    else:
        kind = "cpu" if (resource or "").startswith("cpu-") else "vm"
    ssh = _safe_ssh(raw.get("ssh_command"))
    ready = raw.get("ssh_ready")
    return Instance(
        id=str(raw.get("id") or ""),
        kind=kind,
        status=_str(raw.get("status")) or "unknown",
        tier=resource or _str(raw.get("machine_id")),
        hourly_price=_num(raw.get("hourly_price")),
        ssh_ready=bool(ssh) and ready is not False,
        ssh=ssh,
        confidential=kind in ("vm", "cpu"),
        hard_stop_at=_str(raw.get("hardstop_at")),
    )


def _from_sandbox(raw: Mapping[str, Any]) -> Instance:
    ssh = _safe_ssh(raw.get("sshCommand"))
    return Instance(id=str(raw.get("id") or ""), kind="sandbox", status=_str(raw.get("status")) or "unknown",
                    name=_str(raw.get("name")), tier=_str(raw.get("templateName")), hourly_price=_num(raw.get("pricePerHour")),
                    ssh_ready=bool(ssh), ssh=ssh)


def list_instances(client: Any, names: Optional[NameStore] = None) -> List[Instance]:
    pods = client.call("GET", "/volt/pods")
    boxes = client.call("GET", "/volt/sandboxes")
    found = [i for i in (_from_pod(p) for p in (pods if isinstance(pods, list) else []) if isinstance(p, Mapping)) if i and i.id]
    sandboxes = boxes.get("sandboxes") if isinstance(boxes, Mapping) else None
    found += [_from_sandbox(b) for b in (sandboxes or []) if isinstance(b, Mapping) and b.get("id")]
    local = {v.get("id"): k for k, v in (names or NameStore()).load().items()}
    return [Instance(**{**i.to_dict(), "name": local.get(i.id) or i.name}) for i in found if i.status.upper() not in _GONE]


def resolve(instances: List[Instance], ref: str, names: Optional[NameStore] = None) -> Optional[Instance]:
    record = (names or NameStore()).load().get(ref)
    wanted = record.get("id") if record else ref
    for inst in instances:
        if inst.id == wanted:
            return inst
    return next((i for i in instances if i.name == ref), None)


def _ssh_key_ids(client: Any, given: Optional[List[str]]) -> List[str]:
    """Ids of the SSH keys to bind: those given, else every key of the account.

    A key with own visibility cannot read the account's keys (403
    API_KEY_ACCOUNT_RESOURCE since 27/10/2026): it must be given the ids, or
    use a Confidential VM or CPU tier, which binds the account keys by itself.
    """
    if given:
        return list(given)
    try:
        keys = client.call("GET", "/volt/ssh-keys")
    except ApiError as exc:
        if (exc.payload or {}).get("error") == "API_KEY_ACCOUNT_RESOURCE":
            raise ApiError(403, "This key cannot read the account's SSH keys. Pass --ssh-key-id (ask the account owner for the id), "
                                "or pick a vm or cpu tier, which uses the keys already on the account.", exc.payload) from None
        raise
    items = keys if isinstance(keys, list) else (keys.get("keys") or keys.get("data") or []) if isinstance(keys, Mapping) else []
    ids = [str(k["id"]) for k in items if isinstance(k, Mapping) and k.get("id")]
    if not ids:
        raise ApiError(400, "No SSH key on the account: add one first (voltagegpu ssh-keys add), or pass --ssh-key-id.")
    return ids


def deploy(client: Any, offer: Offer, name: str, ttl: TtlPlan, *, image: Optional[str] = None,
           ssh_key_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Start one machine. Charges the first hour. Never retried here: a timeout may have started it."""
    if ttl.kind != offer.kind:
        raise ValueError("internal: ttl plan for another kind")
    if offer.kind in ("vm", "cpu"):
        raw = client.call("POST", "/confidential/vm/deploy", json={"name": name, "resource_name": offer.tier, **ttl.fields})
        pod_id, price, secret = raw.get("workloadUid") or raw.get("podId"), raw.get("hourlyPrice"), raw.get("sudoPassword")
        hard_stop = raw.get("hardStopAt")
    elif offer.kind == "standard":
        # One SSH key (ssh_key_id). Without it the API takes the account's only key, and
        # answers 400 when there are several: the key list is never read here (an own key cannot).
        if ssh_key_ids and len(ssh_key_ids) > 1:
            raise ValueError("A standard GPU takes one SSH key: give a single SSH key id.")
        key = {"ssh_key_id": ssh_key_ids[0]} if ssh_key_ids else {}
        raw = client.call("POST", "/volt/pods", json={"provider": "standard", "name": name, "machine_id": offer.tier, **ttl.fields, **key})
        pod = raw.get("pod") or {}
        pod_id, price, secret, hard_stop = pod.get("id"), pod.get("hourly_price"), None, None
    elif offer.kind == "baremetal":
        keys = _ssh_key_ids(client, ssh_key_ids)
        raw = client.call("POST", "/volt/pods", json={"provider": "dedicated", "name": name, "machine_id": offer.tier,
                                                      **ttl.fields, "ssh_key_ids": keys})
        pod = raw.get("pod") or {}
        pod_id, price, secret, hard_stop = pod.get("id"), pod.get("hourlyPrice"), None, pod.get("hardStopAt")
    else:
        raw = client.call("POST", "/volt/sandboxes", json={"templateName": offer.tier, "image": image or DEFAULT_SANDBOX_IMAGE,
                                                           "name": name, **ttl.fields, "sshKeyIds": _ssh_key_ids(client, ssh_key_ids)})
        pod_id, price, secret, hard_stop = raw.get("podId"), raw.get("hourlyPrice"), None, None
    if not pod_id:
        raise ApiError(502, "The API accepted the request but returned no machine id. Run `vgpu ps` before trying again.")
    hourly = _num(price) if price is not None else offer.price_per_hour
    return {"id": str(pod_id), "name": name, "kind": offer.kind, "tier": offer.tier, "hourly_price": hourly,
            "prepaid_usd": hourly, "ttl_seconds": ttl.requested_seconds, "ttl_effective_seconds": ttl.effective_seconds,
            "hard_stop_at": hard_stop, "confidential": offer.confidential, "tee": offer.tee, "_sudo_password": secret}


def release(client: Any, inst: Instance) -> Dict[str, Any]:
    if inst.kind == "sandbox":
        raw = client.call("DELETE", f"/volt/sandboxes/{_path_id(inst.id)}")
    else:
        raw = client.call("DELETE", f"/volt/pods/{_path_id(inst.id)}")
    refund = raw.get("prepaidRefund") if isinstance(raw, Mapping) else None
    return {"id": inst.id, "name": inst.name, "kind": inst.kind, "released": True,
            "prepaid_refund": float(refund) if isinstance(refund, (int, float)) else None}


__all__ = ["DEFAULT_SANDBOX_IMAGE", "Instance", "NameStore", "check_name", "deploy", "list_instances", "release", "resolve"]
