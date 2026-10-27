"""`vgpu mcp`: the local MCP server (stdio, JSON-RPC 2.0) with the same guards as the CLI.
Simulated API, no network, no ssh binary."""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List

import httpx
import pytest

import volt.cli.vgpu as vgpu_mod
import volt.mcp.server as mcp_mod
from volt.mcp.server import McpServer

from .conftest import TEST_KEY
from .test_vgpu_cli import SEVERAL_KEYS, SNAPSHOT, STANDARD_TIER, VM_LISTED, VM_TIERS, FakeRun, machines, vm_deploy

INFERENCE_KEY = "vgpu_" + "c" * 40


@pytest.fixture
def api(cli_env, monkeypatch):
    cli_env.on("GET", "/api/confidential/vm/tiers", body=VM_TIERS)
    cli_env.on("GET", "/api/volt/machines", handler=machines)
    cli_env.on("GET", "/api/pricing/snapshot", body=SNAPSHOT)
    cli_env.on("GET", "/api/volt/sandboxes", body={"sandboxes": []})
    monkeypatch.setattr(vgpu_mod, "_sleep", lambda s: None)
    monkeypatch.setattr(vgpu_mod, "_tool", lambda name: name)
    return cli_env


@pytest.fixture
def server(api) -> McpServer:
    return McpServer()


def call(server: McpServer, tool: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    reply = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}})
    assert reply and reply["id"] == 7 and TEST_KEY not in json.dumps(reply)
    return reply["result"]


def payload(result: Dict[str, Any]) -> Any:
    return json.loads(result["content"][0]["text"])


def test_initialize_and_tools_list(server) -> None:
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "voltagegpu" and "tools" in init["result"]["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = {t["name"]: t for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert set(tools) == {"ls", "up", "ps", "exec", "scp", "rm", "attest", "inference", "whoami"}
    assert set(tools["up"]["inputSchema"]["required"]) == {"tier", "name", "ttl", "max_hourly_price"}
    assert tools["rm"]["inputSchema"]["required"] == ["name", "confirm"]
    for tool in tools.values():
        text = (tool["description"] + json.dumps(tool["inputSchema"])).lower()
        assert "free" not in text and "decentrali" not in text


def test_unknown_method_and_ping(server) -> None:
    assert server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})["result"] == {}


def test_ls_cheapest_first(server) -> None:
    offers = payload(call(server, "ls", {"kind": "vm"}))
    assert [o["tier"] for o in offers] == ["rtx6000b-small"]


def test_up_requires_ttl_and_max_price(server, api) -> None:
    result = call(server, "up", {"tier": "rtx6000b-small", "name": "a1", "max_hourly_price": 5})
    assert result["isError"] and "ttl" in result["content"][0]["text"]
    result = call(server, "up", {"tier": "rtx6000b-small", "name": "a1", "ttl": "1h"})
    assert result["isError"] and "max_hourly_price" in result["content"][0]["text"]
    assert api.sent("POST", "/api/confidential/vm/deploy") == []


def test_up_over_max_price_is_refused(server, api) -> None:
    result = call(server, "up", {"tier": "rtx6000b-small", "name": "a1", "ttl": "1h", "max_hourly_price": 1})
    assert result["isError"] and payload(result)["error"]["code"] == "OVER_MAX_PRICE"
    assert api.sent("POST", "/api/confidential/vm/deploy") == []


def test_up_deploys_with_the_product_ttl(server, api) -> None:
    api.on("POST", "/api/confidential/vm/deploy", handler=vm_deploy)
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    result = call(server, "up", {"tier": "rtx6000b-small", "name": "a1", "ttl": "2h", "max_hourly_price": 5})
    data = payload(result)
    assert not result.get("isError") and data["id"] == "wrk-1" and data["ssh_ready"] is True
    assert api.json_of("POST", "/api/confidential/vm/deploy")["hardstop_hours"] == 2.0
    assert "s3cret-pass" not in json.dumps(result)


def test_up_budget_exceeded_says_stop(server, api) -> None:
    api.on("POST", "/api/confidential/vm/deploy", status=402, body={"success": False, "error": "API_KEY_BUDGET_EXCEEDED",
                                                                    "message": "This API key reached its daily budget of $20.00.",
                                                                    "charged": False})
    result = call(server, "up", {"tier": "rtx6000b-small", "name": "a1", "ttl": "1h", "max_hourly_price": 5})
    err = payload(result)["error"]
    assert result["isError"] and err["code"] == "API_KEY_BUDGET_EXCEEDED" and err["exit_code"] == 6 and "do not retry" in err["message"]


def test_up_standard_passes_ssh_key_ids(server, api) -> None:
    api.on("POST", "/api/volt/pods", handler=lambda r: httpx.Response(201, json={"success": True, "pod": {"id": "pod-9", "hourly_price": 0.59}}))
    result = call(server, "up", {"tier": STANDARD_TIER, "name": "s1", "ttl": "1h", "max_hourly_price": 1,
                                 "ssh_key_ids": ["key_7"], "wait": False})
    assert not result.get("isError")
    assert api.json_of("POST", "/api/volt/pods")["ssh_key_id"] == "key_7"
    result = call(server, "up", {"tier": STANDARD_TIER, "name": "s2", "ttl": "1h", "max_hourly_price": 1,
                                 "ssh_key_ids": ["key_7", "key_8"], "wait": False})
    assert result["isError"] and payload(result)["error"]["exit_code"] == 2
    assert len(api.sent("POST", "/api/volt/pods")) == 1


def test_up_standard_several_ssh_keys_says_pass_ssh_key_ids(server, api) -> None:
    api.on("POST", "/api/volt/pods", status=400, body=SEVERAL_KEYS)
    result = call(server, "up", {"tier": STANDARD_TIER, "name": "s1", "ttl": "1h", "max_hourly_price": 1, "wait": False})
    err = payload(result)["error"]
    assert result["isError"] and err["code"] == "SSH_KEY_ID_REQUIRED" and err["exit_code"] == 2
    assert "several SSH keys" in err["message"] and "ssh_key_ids" in err["message"] and "--ssh-key-id" not in err["message"]


def test_rm_needs_confirm_true(server, api) -> None:
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    api.on("DELETE", "/api/volt/pods/wrk-1", body={"success": True})
    assert call(server, "rm", {"name": "wrk-1"})["isError"]
    assert call(server, "rm", {"name": "wrk-1", "confirm": False})["isError"]
    assert api.sent("DELETE", "/api/volt/pods/wrk-1") == []
    assert payload(call(server, "rm", {"name": "wrk-1", "confirm": True}))["released"] is True


def test_exec_captures_output_and_exit_code(server, api, monkeypatch) -> None:
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    fake = FakeRun(codes=[2])
    monkeypatch.setattr(vgpu_mod, "_run", fake)
    data = payload(call(server, "exec", {"name": "wrk-1", "command": "nvidia-smi -L"}))
    assert data["exit_code"] == 2 and data["stdout"] == "4242\n"
    assert fake.calls[0][-2:] == ["ubuntu@203.0.113.7", "nvidia-smi -L"]


def test_exec_detached(server, api, monkeypatch) -> None:
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    monkeypatch.setattr(vgpu_mod, "_run", FakeRun())
    data = payload(call(server, "exec", {"name": "wrk-1", "command": "python train.py", "detach": True}))
    assert data["pid"] == 4242 and data["log"].startswith("~/.vgpu/")


def test_scp_refuses_shell_characters(server, api, monkeypatch) -> None:
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    monkeypatch.setattr(vgpu_mod, "_run", FakeRun())
    assert call(server, "scp", {"name": "wrk-1", "source": "/tmp/$(id)", "destination": "x"})["isError"]
    ok = payload(call(server, "scp", {"name": "wrk-1", "source": "/tmp/out.txt", "destination": "out.txt"}))
    assert ok["exit_code"] == 0


def test_attest_refuses_a_machine_without_attestation(server, api) -> None:
    api.on("GET", "/api/volt/pods", body=[{"id": "pod-9", "status": "RUNNING", "provider": "standard", "confidential_compute": False,
                                          "ssh_command": "ssh root@198.51.100.9 -p 40400"}])
    result = call(server, "attest", {"name": "pod-9"})
    assert result["isError"] and "no tenant attestation" in result["content"][0]["text"]


def chat_api(seen: List[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen3-32B-TEE"}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hello"}}], "model": "Qwen3-32B-TEE"},
                              headers={"X-VoltageGPU-Cost-USD": "0.0001"})
    return httpx.MockTransport(handler)


def test_inference_uses_the_vgpu_key_and_the_v1_api(server, monkeypatch) -> None:
    seen: List[httpx.Request] = []
    monkeypatch.setenv("VOLTAGEGPU_API_KEY", INFERENCE_KEY)
    monkeypatch.setattr(mcp_mod, "_inference_transport", chat_api(seen))
    data = payload(call(server, "inference", {"model": "Qwen3-32B-TEE", "messages": [{"role": "user", "content": "hi"}]}))
    assert data["content"] == "hello" and data["cost_usd"] == "0.0001"
    assert seen[0].url == "https://api.voltagegpu.com/v1/chat/completions"
    assert seen[0].headers["Authorization"] == f"Bearer {INFERENCE_KEY}"
    assert payload(call(server, "inference", {"list_models": True})) == ["Qwen3-32B-TEE"]


def test_inference_without_a_vgpu_key_is_refused(server, monkeypatch) -> None:
    monkeypatch.delenv("VOLTAGEGPU_API_KEY", raising=False)
    result = call(server, "inference", {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    assert result["isError"] and "vgpu_" in result["content"][0]["text"]


def test_serve_over_stdio(server) -> None:
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "{not json",
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), ""]
    out = io.StringIO()
    server.serve(io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert replies[1]["error"]["code"] == -32700 and len(replies) == 2


def test_vgpu_mcp_command_is_registered() -> None:
    assert "mcp" in vgpu_mod.vgpu.commands
