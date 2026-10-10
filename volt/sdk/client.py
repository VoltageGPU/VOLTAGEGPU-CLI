"""HTTP client for the VoltageGPU public API (``/api/volt/*``, ``X-API-Key``)."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import httpx

from .. import __version__
from .config import Config
from .errors import ApiError
from .models import (
    CONFIDENTIAL,
    STANDARD,
    Balance,
    DeployResult,
    Machine,
    Pod,
    ReleaseResult,
    SshKey,
    Template,
)

MACHINE_KINDS = ("all", STANDARD, CONFIDENTIAL)


def _as_list(data: Any, *keys: str) -> List[Mapping[str, Any]]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, Mapping)]
    if isinstance(data, Mapping):
        for key in keys or ("items", "data", "pods", "keys", "tiers"):
            value = data.get(key)
            if isinstance(value, list):
                return [d for d in value if isinstance(d, Mapping)]
    return []


class VoltageGPUClient:
    """Thin, typed wrapper over the public API.

    TLS certificates are always verified and redirects are never followed:
    the API key is a header, and a redirect to another host would carry it.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        *,
        timeout: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.config = config or Config.load()
        self._http = httpx.Client(
            base_url=self.config.base_url,
            headers={
                "X-API-Key": self.config.api_key,
                "Accept": "application/json",
                "User-Agent": f"voltagegpu-cli/{__version__}",
            },
            timeout=httpx.Timeout(timeout, connect=15.0),
            follow_redirects=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"VoltageGPUClient(base_url={self.config.base_url!r})"

    def __enter__(self) -> "VoltageGPUClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ core

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json: Any = None) -> Any:
        try:
            response = self._http.request(method, path, params=params, json=json)
        except httpx.TimeoutException:
            raise ApiError(0, "The VoltageGPU API did not answer in time. Try again.") from None
        except httpx.RequestError as exc:
            raise ApiError(0, f"Could not reach the VoltageGPU API ({type(exc).__name__}).") from None
        if response.is_redirect:
            raise ApiError(response.status_code, "Unexpected redirect from the API; refusing to follow it with your key.")
        body: Any
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = response.text
        if response.status_code >= 400:
            raise ApiError.from_response(response.status_code, body)
        return body

    # -------------------------------------------------------------- machines

    def list_machines(self, kind: str = "all") -> List[Machine]:
        """Deployable machines with the price charged, cheapest first within each kind."""
        if kind not in MACHINE_KINDS:
            raise ValueError(f"kind must be one of {MACHINE_KINDS}")
        data = self._request("GET", "/volt/machines", params={"provider": kind})
        machines = [Machine.from_api(m) for m in _as_list(data)]
        return sorted(machines, key=lambda m: (m.kind != CONFIDENTIAL, m.price_per_hour if m.price_per_hour is not None else 1e9))

    def list_templates(self) -> List[Template]:
        """Curated images for standard pods (``template_id`` of a standard deploy)."""
        data = self._request("GET", "/volt/templates", params={"provider": STANDARD})
        return [Template.from_api(t) for t in _as_list(data, "items")]

    # ------------------------------------------------------------------ pods

    def list_pods(self) -> List[Pod]:
        """Your running pods and Confidential VMs."""
        return [Pod.from_api(p) for p in _as_list(self._request("GET", "/volt/pods"))]

    def get_pod(self, pod_id: str) -> Pod:
        """One pod. The list carries the SSH details of Confidential VMs, so it is read first."""
        for pod in self.list_pods():
            if pod.id == pod_id:
                return pod
        data = self._request("GET", f"/volt/pods/{_path_id(pod_id)}")
        if isinstance(data, Mapping) and data.get("provider") == STANDARD:
            return Pod.from_api(data)
        state = data.get("state") if isinstance(data, Mapping) else None
        status = str(state.get("status")) if isinstance(state, Mapping) and state.get("status") else "unknown"
        return Pod(id=pod_id, kind=CONFIDENTIAL, status=status.upper(), provider_status=status)

    def deploy_standard(
        self,
        machine_id: str,
        name: str,
        *,
        template_id: Optional[str] = None,
        ssh_key_id: Optional[str] = None,
        gpu_count: Optional[int] = None,
        jupyter: Optional[bool] = None,
        port_count: Optional[int] = None,
        auto_terminate_hours: Optional[int] = None,
    ) -> DeployResult:
        """Deploy a standard pod (no enclave). Charges the first hour now."""
        body: Dict[str, Any] = {"provider": STANDARD, "name": name, "machine_id": machine_id}
        optional = {
            "template_id": template_id,
            "ssh_key_id": ssh_key_id,
            "gpu_count": gpu_count,
            "jupyter": jupyter,
            "port_count": port_count,
            "auto_terminate_hours": auto_terminate_hours,
        }
        body.update({k: v for k, v in optional.items() if v is not None})
        return DeployResult.from_api(self._request("POST", "/volt/pods", json=body))

    def deploy_confidential(
        self,
        tier: str,
        name: str,
        *,
        hardstop_hours: Optional[int] = None,
        ports: Optional[Iterable[Mapping[str, Any]]] = None,
    ) -> DeployResult:
        """Deploy a Confidential VM (Intel TDX). Charges the first hour now.

        Every SSH key on the account is bound to the VM at creation; a VM cannot
        be given a key later, so add your key first.
        """
        body: Dict[str, Any] = {"provider": CONFIDENTIAL, "name": name, "resource_name": tier}
        if hardstop_hours is not None:
            body["hardstop_hours"] = hardstop_hours
        port_list = list(ports or [])
        if port_list:
            body["ports"] = port_list
        return DeployResult.from_api(self._request("POST", "/volt/pods", json=body))

    def release_pod(self, pod_id: str) -> ReleaseResult:
        """Stop and release a pod. Standard pods get the unused prepaid seconds back."""
        data = self._request("DELETE", f"/volt/pods/{_path_id(pod_id)}")
        refund = data.get("prepaidRefund") if isinstance(data, Mapping) else None
        return ReleaseResult(pod_id=pod_id, prepaid_refund=float(refund) if isinstance(refund, (int, float)) else None)

    def wait_until_reachable(
        self,
        pod_id: str,
        *,
        timeout: float = 600.0,
        interval: float = 10.0,
        on_poll: Optional[Callable[[Pod], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> Pod:
        """Poll until the pod has a usable SSH command, or raise TimeoutError."""
        deadline = clock() + timeout
        while True:
            pod = self.get_pod(pod_id)
            if on_poll:
                on_poll(pod)
            if pod.ssh_command_rejected:
                raise ApiError(502, f"The API returned an SSH command of an unexpected form for {pod_id}; it was not used.")
            if pod.reachable:
                return pod
            if pod.status.upper() in ("STOPPED", "TERMINATED", "FAILED", "DELETED"):
                raise ApiError(409, f"Pod {pod_id} is {pod.status}; it will not become reachable.")
            if clock() >= deadline:
                raise TimeoutError(f"Pod {pod_id} was not reachable after {int(timeout)} s (last status {pod.status}).")
            sleep(interval)

    # -------------------------------------------------------------- ssh keys

    def list_ssh_keys(self) -> List[SshKey]:
        return [SshKey.from_api(k) for k in _as_list(self._request("GET", "/volt/ssh-keys"), "keys", "data")]

    def add_ssh_key(self, name: str, public_key: str) -> SshKey:
        data = self._request("POST", "/volt/ssh-keys", json={"name": name, "public_key": public_key})
        return SshKey.from_api(data if isinstance(data, Mapping) else {})

    def delete_ssh_key(self, key_id: str) -> None:
        self._request("DELETE", f"/volt/ssh-keys/{_path_id(key_id)}")

    # --------------------------------------------------------------- account

    def get_balance(self) -> Balance:
        data = self._request("GET", "/volt/balance")
        return Balance.from_api(data if isinstance(data, Mapping) else {})


def _path_id(value: str) -> str:
    """Ids go into the URL path: refuse anything that could change the path."""
    text = (value or "").strip()
    if not text or len(text) > 128 or not all(c.isalnum() or c in "-_." for c in text) or text in (".", ".."):
        raise ValueError(f"Invalid id {value!r}")
    return text


__all__ = ["VoltageGPUClient", "MACHINE_KINDS"]
