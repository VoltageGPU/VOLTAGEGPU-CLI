"""`vgpu mcp`: a local MCP server over stdio for AI agents (Claude Code, Codex, Cursor...).

Why local: `exec`, `scp` and `attest` need the user's SSH private key, which
must never leave the user's machine. The remote MCP server of voltagegpu.com
has no exec for that reason.

Protocol: JSON-RPC 2.0, one message per line on stdin and stdout (MCP stdio
transport): initialize, ping, tools/list, tools/call. Nothing else is ever
written to stdout. No dependency beyond the CLI's own.

Same guards as the CLI: `up` needs `ttl` and `max_hourly_price` (the agent
cannot answer a confirmation prompt, the price ceiling plays that role),
`rm` needs `confirm: true`, a 402 is returned as API_KEY_BUDGET_EXCEEDED with
"do not retry", the key is never part of any answer.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, TextIO
from urllib.parse import urlsplit

import httpx

from .. import __version__
from ..cli import vgpu as ops
from ..cli.state import AppState
from ..sdk.catalog import filter_offers, list_offers
from ..sdk.config import validate_base_url
from ..sdk.errors import ApiError, ConfigError, scrub
from ..sdk.fleet import NameStore, check_name, deploy, list_instances, release, resolve
from ..sdk.remote import RemotePathError, detached_command, exec_argv, scp_argv
from ..sdk.ttl import KINDS, TtlError, parse_ttl, plan_ttl

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INFERENCE_KEY_ENV = "VOLTAGEGPU_API_KEY"
_MAX_OUTPUT = 64 * 1024
_inference_transport: Optional[httpx.BaseTransport] = None  # test seam

_RULES = ("Always pass ttl. Release machines with rm when done. On error API_KEY_BUDGET_EXCEEDED (exit 6) stop and tell the user; "
          "do not retry. Call a machine confidential only after attest returns VERIFIED.")

TOOLS: List[Dict[str, Any]] = [
    {"name": "ls", "description": "List VoltageGPU offers with the price charged per hour, cheapest first. "
     "Kinds: vm (Confidential VM, Intel TDX, NVIDIA GPU), cpu (Confidential CPU, AMD SEV-SNP), standard (GPU pod, no enclave), "
     "baremetal (dedicated machine, no enclave), sandbox (CPU sandbox, no enclave).",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": list(KINDS)}, "gpu": {"type": "string", "description": "GPU model contains this text"},
         "max_price": {"type": "number"}, "include_sold_out": {"type": "boolean"}}, "additionalProperties": False}},
    {"name": "up", "description": "Start a machine. Charges the first hour now. " + _RULES,
     "inputSchema": {"type": "object", "properties": {
         "tier": {"type": "string", "description": "tier from ls"}, "name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,31}$"},
         "ttl": {"type": "string", "description": "deadline such as 45m, 1h, 6h, 2d; the machine stops by itself then"},
         "max_hourly_price": {"type": "number", "description": "refuse if the hourly price is above this (USD)"},
         "kind": {"type": "string", "enum": list(KINDS)}, "image": {"type": "string"},
         "ssh_key_ids": {"type": "array", "items": {"type": "string"}, "description": "for baremetal and sandbox"},
         "wait": {"type": "boolean", "description": "wait until SSH answers (default true)"}},
         "required": ["tier", "name", "ttl", "max_hourly_price"], "additionalProperties": False}},
    {"name": "ps", "description": "Machines of this key with status, price and SSH readiness.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "exec", "description": "Run a shell command on a machine over SSH with the user's local key. Returns exit_code, stdout and "
     "stderr (truncated to 64 KB). detach=true starts it in the background and returns the PID and the log path.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "command": {"type": "string"}, "detach": {"type": "boolean"},
         "timeout_s": {"type": "integer", "minimum": 1, "maximum": 3600}}, "required": ["name", "command"], "additionalProperties": False}},
    {"name": "scp", "description": "Copy a file between the machine and this computer (direction download by default, or upload).",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "source": {"type": "string"}, "destination": {"type": "string"},
         "direction": {"type": "string", "enum": ["download", "upload"]}, "recursive": {"type": "boolean"}},
         "required": ["name", "source", "destination"], "additionalProperties": False}},
    {"name": "rm", "description": "Release a machine; its disk is deleted. Requires confirm=true.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "confirm": {"type": "boolean", "const": True}},
                     "required": ["name", "confirm"], "additionalProperties": False}},
    {"name": "attest", "description": "Check that a Confidential VM (Intel TDX + NVIDIA) or Confidential CPU (AMD SEV-SNP) machine is genuine: "
     "challenge drawn on this computer, proofs generated in the machine, verified on this computer. Standard pods, dedicated "
     "machines and sandboxes have no tenant attestation.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "offline": {"type": "boolean"}, "out": {"type": "string"}},
                     "required": ["name"], "additionalProperties": False}},
    {"name": "inference", "description": "Chat with a confidential model (ids ending in -TEE) on the OpenAI-compatible API, no GPU to rent. "
     "Needs an inference key (vgpu_...) in VOLTAGEGPU_API_KEY. list_models=true returns the model ids.",
     "inputSchema": {"type": "object", "properties": {
         "model": {"type": "string"}, "list_models": {"type": "boolean"}, "max_tokens": {"type": "integer", "minimum": 1, "maximum": 32768},
         "messages": {"type": "array", "items": {"type": "object", "properties": {
             "role": {"type": "string", "enum": ["system", "user", "assistant"]}, "content": {"type": "string"}},
             "required": ["role", "content"]}}}, "additionalProperties": False}},
    {"name": "whoami", "description": "Scopes, machine visibility, budgets and spend of the API key (never the key itself).",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


class ToolError(Exception):
    def __init__(self, fail: "ops.Fail") -> None:
        super().__init__(fail.message)
        self.fail = fail


def _text(data: Any, is_error: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}]}
    if is_error:
        result["isError"] = True
    return result


def _required(args: Mapping[str, Any], *names: str) -> None:
    missing = [n for n in names if args.get(n) in (None, "")]
    if missing:
        raise ToolError(ops.Fail(2, f"Missing argument(s): {', '.join(missing)}."))


class McpServer:
    def __init__(self) -> None:
        self._state = AppState()
        self._handlers: Dict[str, Callable[[Mapping[str, Any]], Any]] = {
            "ls": self._ls, "up": self._up, "ps": self._ps, "exec": self._exec, "scp": self._scp,
            "rm": self._rm, "attest": self._attest, "inference": self._inference, "whoami": self._whoami,
        }

    # ------------------------------------------------------------ protocol

    def handle(self, msg: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(msg, Mapping) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:
            return None  # notification (initialized, cancelled...): never answered
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, Mapping) else None
            result: Any = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "voltagegpu", "version": __version__},
                           "instructions": _RULES}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call" and isinstance(params, Mapping):
            result = self._call(str(params.get("name") or ""), params.get("arguments") or {})
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply: Optional[Dict[str, Any]] = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply, default=str) + "\n")
                stdout.flush()
        self._state.close()

    def _call(self, name: str, args: Any) -> Dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None or not isinstance(args, Mapping):
            return _text({"error": {"code": "USAGE", "message": f"Unknown tool {name!r}.", "exit_code": 2}}, True)
        try:
            return _text(handler(args))
        except ToolError as exc:
            fail = exc.fail
        except ops.Fail as exc:
            fail = exc
        except ApiError as exc:
            fail = ops.api_fail(exc)
        except ConfigError as exc:
            fail = ops.Fail(3, str(exc), "API_KEY_MISSING")
        except (TtlError, RemotePathError, ValueError) as exc:
            fail = ops.Fail(2, str(exc))
        except Exception as exc:  # a tool error must never kill the server; no message text (could carry data)
            fail = ops.Fail(1, f"Internal error ({type(exc).__name__}).")
        return _text({"error": {"code": fail.code, "message": scrub(fail.message), "exit_code": fail.exit_code, **fail.extra}}, True)

    @property
    def _client(self) -> Any:
        return self._state.client()

    # --------------------------------------------------------------- tools

    def _ls(self, args: Mapping[str, Any]) -> Any:
        kind = args.get("kind")
        offers, errors = list_offers(self._client, (kind,) if kind else KINDS)
        offers = filter_offers(offers, gpu=args.get("gpu"), max_price=args.get("max_price"), include_sold_out=bool(args.get("include_sold_out")))
        data = [o.to_dict() for o in offers]
        return {"offers": data, "warnings": errors} if errors else data

    def _up(self, args: Mapping[str, Any]) -> Any:
        _required(args, "tier", "name", "ttl", "max_hourly_price")
        name, tier, ceiling = check_name(str(args["name"])), str(args["tier"]), float(args["max_hourly_price"])
        seconds = parse_ttl(str(args["ttl"]))
        kind = args.get("kind")
        offers, _ = list_offers(self._client, (kind,) if kind else KINDS)
        offer = next((o for o in offers if o.tier == tier), None)
        if offer is None:
            raise ops.Fail(5, f"Unknown tier {tier!r}. Call ls for the current offers.")
        if not offer.in_stock:
            raise ops.Fail(7, f"{tier} is sold out right now.", "SOLD_OUT")
        if offer.price_per_hour is None or offer.price_per_hour > ceiling:
            raise ops.Fail(7, f"{tier} costs {offer.price_per_hour} USD/h, above max_hourly_price {ceiling}.", "OVER_MAX_PRICE")
        plan = plan_ttl(offer.kind, seconds)
        names = NameStore()
        known = names.load().get(name)
        if known and resolve(list_instances(self._client), known.get("id", "")):
            raise ops.Fail(2, f"A running machine is already named {name!r}.")
        try:
            result = deploy(self._client, offer, name, plan, image=args.get("image"), ssh_key_ids=list(args.get("ssh_key_ids") or []) or None)
        except ApiError as exc:
            if exc.status_code == 0:
                raise ops.Fail(1, "The deploy request got no answer: a machine may have started. Call ps before trying again; "
                                  "never retry a deploy blindly.", "NO_ANSWER") from None
            raise
        result.pop("_sudo_password", None)  # never handed to an agent
        names.put(name, {"id": result["id"], "kind": result["kind"], "tier": tier})
        ready = None
        if args.get("wait", True):
            ready = ops._wait_ready_client(self._client, result["id"], 900)
        result.update({"ssh": ready.ssh if ready else None, "ssh_ready": bool(ready), "status": ready.status if ready else "STARTING"})
        return result

    def _ps(self, args: Mapping[str, Any]) -> Any:
        return [i.to_dict() for i in list_instances(self._client)]

    def _exec(self, args: Mapping[str, Any]) -> Any:
        _required(args, "name", "command")
        inst = ops.find_instance(self._client, str(args["name"]))
        target = ops._reachable(inst)
        ssh = ops._need("ssh")
        command = str(args["command"])
        timeout = int(args.get("timeout_s") or 600)
        if args.get("detach"):
            job = secrets.token_hex(4)
            remote, log = detached_command(command, job)
            proc = ops._run(exec_argv(target, inst.id, remote, identity=ops._identity(None), ssh_bin=ssh), capture_output=True, timeout=120)
            out = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
            if proc.returncode != 0 or not out or not out[-1].strip().isdigit():
                raise ops.Fail(1, f"Could not start the background command (ssh exit {proc.returncode}).")
            return {"name": inst.name, "id": inst.id, "job": job, "pid": int(out[-1]), "log": log}
        proc = ops._run(exec_argv(target, inst.id, command, identity=ops._identity(None), ssh_bin=ssh), capture_output=True, timeout=timeout)
        return {"exit_code": proc.returncode, "stdout": _clip(proc.stdout), "stderr": _clip(proc.stderr)}

    def _scp(self, args: Mapping[str, Any]) -> Any:
        _required(args, "name", "source", "destination")
        inst = ops.find_instance(self._client, str(args["name"]))
        target = ops._reachable(inst)
        argv = scp_argv(target, inst.id, str(args["source"]), str(args["destination"]), upload=args.get("direction") == "upload",
                        recursive=bool(args.get("recursive")), identity=ops._identity(None), scp_bin=ops._need("scp"))
        proc = ops._run(argv, capture_output=True, timeout=3600)
        return {"exit_code": proc.returncode, "stderr": _clip(proc.stderr)}

    def _rm(self, args: Mapping[str, Any]) -> Any:
        _required(args, "name")
        if args.get("confirm") is not True:
            raise ops.Fail(2, "rm releases the machine and deletes its disk: pass confirm=true.")
        inst = ops.find_instance(self._client, str(args["name"]))
        data = release(self._client, inst)
        NameStore().remove_id(inst.id)
        return data

    def _attest(self, args: Mapping[str, Any]) -> Any:
        _required(args, "name")
        data, code = ops.attest_machine(self._client, str(args["name"]), out=args.get("out"), offline=bool(args.get("offline")), identity=None)
        if code != 0:
            failed = ", ".join(data.get("failed") or [])
            raise ops.Fail(code, f"Attestation {data.get('status')}: {failed}. Do not call this machine confidential.",
                           "ATTESTATION_INCOMPLETE" if code == 9 else "ATTESTATION_FAILED", {"result": data})
        return data

    def _inference(self, args: Mapping[str, Any]) -> Any:
        key = (os.environ.get(INFERENCE_KEY_ENV) or "").strip()
        if not key.startswith("vgpu_") or len(key) > 200 or any(c.isspace() for c in key):
            raise ops.Fail(3, f"Inference needs an inference key (vgpu_...) in {INFERENCE_KEY_ENV}; a volt_ key does not open inference.",
                           "INFERENCE_KEY_MISSING")
        base = _inference_base(self._state.base_url or os.environ.get("VOLT_BASE_URL"))
        with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {key}", "User-Agent": f"voltagegpu-cli/{__version__}"},
                          timeout=httpx.Timeout(300.0, connect=15.0), follow_redirects=False, transport=_inference_transport) as http:
            if args.get("list_models"):
                body = _json_or_fail(http.get("/models"))
                return [str(m.get("id")) for m in body.get("data", []) if isinstance(m, Mapping)]
            _required(args, "model", "messages")
            request: Dict[str, Any] = {"model": str(args["model"]), "messages": list(args["messages"])}
            if args.get("max_tokens"):
                request["max_tokens"] = int(args["max_tokens"])
            response = http.post("/chat/completions", json=request)
            body = _json_or_fail(response)
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") if isinstance(choice, Mapping) else None
        return {"model": body.get("model"), "content": (message or {}).get("content"),
                "cost_usd": response.headers.get("X-VoltageGPU-Cost-USD"), "usage": body.get("usage")}

    def _whoami(self, args: Mapping[str, Any]) -> Any:
        client = self._client
        try:
            raw = client.call("GET", "/volt/whoami")
        except ApiError as exc:
            if exc.status_code != 404:
                raise
            bal = client.get_balance()
            raw = {"balance": bal.balance, "balance_hidden": bal.balance_hidden, "key": {}}
        key = raw.get("key") if isinstance(raw.get("key"), Mapping) else {}
        return {"key": {**key, "masked": client.config.masked_key}, "balance": raw.get("balance"), "balance_hidden": raw.get("balance_hidden")}


def _clip(data: Any) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data or "")
    return text if len(text) <= _MAX_OUTPUT else text[:_MAX_OUTPUT] + f"\n[... {len(text) - _MAX_OUTPUT} more characters cut]"


def _inference_base(base_url: Optional[str]) -> str:
    """https://api.voltagegpu.com/api -> https://api.voltagegpu.com/v1 (same host checks as the pod API)."""
    url = validate_base_url(base_url or "https://api.voltagegpu.com/api", allow_custom=os.environ.get("VOLT_ALLOW_CUSTOM_BASE_URL") == "1")
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/v1"


def _json_or_fail(response: httpx.Response) -> Dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        raise ApiError.from_response(response.status_code, body)
    return body if isinstance(body, dict) else {}


def run_stdio() -> None:
    McpServer().serve()


__all__ = ["McpServer", "TOOLS", "run_stdio"]
