"""Shared fixtures: a fake API behind httpx.MockTransport, so no test touches the network."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

from volt.sdk.client import VoltageGPUClient
from volt.sdk.config import Config

TEST_KEY = "volt_" + "a" * 32 + "_" + "b" * 32

# Shapes captured from the production API on 2026-10-10 (trimmed).
STANDARD_MACHINES = [
    {"machine_id": "a292aae7-2767-44b0-803b-12cc3ab47012", "name": "2x NVIDIA RTX A5000", "provider": "standard",
     "confidential_compute": False, "gpu_type": "NVIDIA RTX A5000", "gpu_count": 2, "gpu_memory_gb": 24,
     "price_per_hour": 0.59, "price_per_gpu_hour": 0.3, "price": 0.59, "available": True, "available_gpu_count": 2,
     "location": {"city": "Oakton", "country": "United States", "country_code": "US"}, "cpu_cores": 16, "ram_gb": 125,
     "disk_gb": 900, "max_cuda_version": "13"},
    {"machine_id": "d088a695-bbe8-4ee3-8fee-be39da2f3eb4", "name": "NVIDIA L40S", "provider": "standard",
     "confidential_compute": False, "gpu_type": "NVIDIA L40S", "gpu_count": 1, "gpu_memory_gb": 45, "price_per_hour": 0.7,
     "price_per_gpu_hour": 0.7, "price": 0.7, "available": True, "available_gpu_count": 1,
     "location": {"city": "Las Vegas", "country": "United States", "country_code": "US"}},
    {"machine_id": "fd78c800-3255-4d66-bd64-077fb2e86e70", "name": "NVIDIA RTX A6000", "provider": "standard",
     "confidential_compute": False, "gpu_type": "NVIDIA RTX A6000", "gpu_count": 1, "gpu_memory_gb": 48,
     "price_per_hour": 0.78, "price": 0.78, "available": True, "available_gpu_count": 1, "location": {}},
]
CONFIDENTIAL_MACHINES = [
    {"name": "NVIDIA H100 - Small [Confidential VM]", "price": 6.95, "total_gpu_count": 8, "provider": "confidential",
     "resource_name": "h100-small", "gpu_type": "NVIDIA-H100", "gpu_count": 1, "vcpu": 12000, "memory": 175000,
     "confidential_compute": True},
    {"name": "NVIDIA RTX Pro 6000B - Small [Confidential VM]", "price": 3.8, "total_gpu_count": 9, "provider": "confidential",
     "resource_name": "rtx6000b-small", "gpu_type": "NVIDIA-RTX6000B", "gpu_count": 1, "vcpu": 32000, "memory": 128000,
     "confidential_compute": True},
]
STANDARD_TEMPLATES = {"success": True, "items": [
    {"id": "bb50bd64-e363-4adc-8111-1d44c8e36807", "name": "PyTorch 2.12 - CUDA 13", "description": "Official PyTorch",
     "category": "PyTorch", "provider": "standard", "default": True},
    {"id": "30612657-5da8-4c89-84af-0edd12bbc47c", "name": "CUDA 13 (full toolkit)", "description": "Bare CUDA",
     "category": "CUDA", "provider": "standard", "default": False},
]}
STANDARD_POD = {"id": "fda79e1c-9784-4207-85f8-a980840bb9c9", "name": "cli-e2e", "status": "RUNNING", "phase": None,
                "gpu_type": "NVIDIA RTX A5000", "gpu_count": 2, "hourly_price": 0.59,
                "ssh_command": "ssh root@147.135.114.178 -p 40400", "provider": "standard", "confidential_compute": False}
VM_POD = {"id": "wrk-abc123", "name": "wrk-abc123", "status": "RUNNING", "provider_status": "running",
          "resource_name": "rtx6000b-small", "gpu_type": "Confidential GPU", "gpu_count": 1, "hourly_price": 3.8,
          "ssh_host": "203.0.113.7", "ssh_port": 30022, "ssh_user": "ubuntu", "ssh_ready": True,
          "ssh_command": "ssh -p 30022 ubuntu@203.0.113.7", "provider": "confidential", "confidential_compute": True}
SSH_KEYS = [{"id": "key_1", "name": "laptop", "publicKey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExample me@laptop",
             "createdAt": "2026-10-09T10:00:00.000Z"}]

Handler = Callable[[httpx.Request], httpx.Response]


class FakeApi:
    """Routes (METHOD, path) to canned answers and records every request."""

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], Any] = {}
        self.requests: List[httpx.Request] = []

    def on(self, method: str, path: str, status: int = 200, body: Any = None, handler: Optional[Handler] = None) -> "FakeApi":
        self.routes[(method.upper(), path)] = handler or (status, body)
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"success": False, "error": "Not found"})
        if callable(route):
            return route(request)
        status, body = route
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    def sent(self, method: str, path: str) -> List[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def json_of(self, method: str, path: str, index: int = -1) -> Any:
        return json.loads(self.sent(method, path)[index].content)


@pytest.fixture(autouse=True)
def no_real_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests must never reach the real API (only tests marked `live` may)."""
    if request.node.get_closest_marker("live"):
        return

    def refuse(self: httpx.HTTPTransport, req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unit test tried a real network call to {req.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture
def api() -> FakeApi:
    fake = FakeApi()
    fake.on("GET", "/api/volt/pods", body=[])
    fake.on("GET", "/api/volt/ssh-keys", body=SSH_KEYS)
    return fake


@pytest.fixture
def client(api: FakeApi) -> VoltageGPUClient:
    cfg = Config(api_key=TEST_KEY, base_url="https://api.test.voltagegpu.com/api", source="test")
    return VoltageGPUClient(cfg, transport=httpx.MockTransport(api))


@pytest.fixture
def cli_env(api: FakeApi, monkeypatch: pytest.MonkeyPatch, tmp_path) -> FakeApi:
    """Make every CLI invocation use the fake API and an isolated HOME."""
    monkeypatch.setenv("VOLT_API_KEY", TEST_KEY)
    monkeypatch.delenv("VOLTAGEGPU_API_KEY", raising=False)
    monkeypatch.delenv("VOLT_BASE_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    import volt.cli.state as state_mod

    real = state_mod.VoltageGPUClient

    def factory(config: Config) -> VoltageGPUClient:
        return real(config, transport=httpx.MockTransport(api))

    monkeypatch.setattr(state_mod, "VoltageGPUClient", factory)
    return api
