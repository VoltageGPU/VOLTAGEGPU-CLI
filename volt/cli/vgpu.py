"""`vgpu`: the short command for scripts and AI agents (next to `voltagegpu`).

    vgpu ls [--kind vm|cpu|standard|baremetal|sandbox] [--gpu H100] [--json]
    vgpu up TIER --name N --ttl 1h -y [--json]
    vgpu ps | exec N "cmd" [-d] | scp N remote local | rm N -y | attest N | whoami

The key is read from VOLT_API_KEY or VOLTAGEGPU_API_KEY (or the file written by
`voltagegpu login`) and never printed. Every machine gets a deadline (--ttl).
Exit codes are a contract, see EXIT_CODES; `exec` returns the remote command's
own code (255 when SSH itself fails).
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import click

from .. import __version__
from ..sdk import attest
from ..sdk.catalog import filter_offers, list_offers
from ..sdk.config import API_KEYS_PAGE
from ..sdk.errors import ApiError, ConfigError, scrub
from ..sdk.fleet import Instance, NameStore, check_name, deploy, list_instances, release, resolve
from ..sdk.remote import RemotePathError, detached_command, exec_argv, scp_argv
from ..sdk.ssh import find_ssh_binary
from ..sdk.ttl import KINDS, TtlError, parse_ttl, plan_ttl
from .main import configure_stream
from .state import AppState

EXIT_CODES = {
    0: "ok",
    1: "error",
    2: "usage",
    3: "api key missing, malformed or refused",
    4: "api key scope missing",
    5: "not found",
    6: "balance or api key budget exceeded (402)",
    7: "unavailable, sold out or over --max-price",
    8: "attestation failed",
    9: "attestation incomplete or local tool missing",
}

# Seams for the tests (no ssh binary, no waiting).
_sleep = time.sleep
_run = subprocess.run


def _tool(name: str) -> Optional[str]:
    return find_ssh_binary(name=name)


class Fail(Exception):
    def __init__(self, exit_code: int, message: str, code: Optional[str] = None, extra: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message
        self.code = code or {2: "USAGE", 5: "NOT_FOUND", 7: "UNAVAILABLE", 8: "ATTESTATION_FAILED", 9: "TOOL_MISSING"}.get(exit_code, "ERROR")
        self.extra = dict(extra or {})


def api_fail(exc: ApiError) -> Fail:
    payload = exc.payload or {}
    raw_code = payload.get("error") if isinstance(payload.get("error"), str) else None
    code = raw_code if raw_code and raw_code.isupper() and " " not in raw_code else str(payload.get("code") or f"HTTP_{exc.status_code or 0}")
    status = exc.status_code
    exit_code = {401: 3, 403: 4, 404: 5, 402: 6, 409: 7, 410: 7, 429: 7, 503: 7, 400: 2, 422: 2}.get(status, 1)
    if status == 403 and code not in ("API_KEY_SCOPE_MISSING", "API_KEY_ACCOUNT_RESOURCE"):
        exit_code = 7  # offer closed or account not eligible: not a scope problem
    extra = {k: payload[k] for k in ("window", "budget_usd", "spent_usd", "requested_usd", "charged", "required_scope") if k in payload}
    message = exc.message
    if code == "API_KEY_BUDGET_EXCEEDED":
        message = f"Stop: {exc.message} Nothing was charged. Ask the account owner to raise the budget; do not retry."
    elif exit_code == 3:
        message = f"The API refused the key (HTTP {status}). Check VOLT_API_KEY; keys are created at {API_KEYS_PAGE}."
    return Fail(exit_code, message, code, {"http_status": status, **extra})


def ssh_key_needed(exc: ApiError, kind: str, given: bool, how: str) -> Optional[Fail]:
    """The 400 of a standard GPU when the account has several SSH keys and none was named.

    ``how`` names the way to give one (``--ssh-key-id`` for the CLI, ``ssh_key_ids`` for MCP).
    The API answers it before any charge; its text is the only marker (no code in the body).
    """
    if kind != "standard" or given or exc.status_code != 400 or "more than one ssh key" not in exc.message.lower():
        return None
    return Fail(2, f"This account has several SSH keys: pass {how} with one key id (the account owner lists them with "
                   "`voltagegpu ssh-keys list`). Nothing was charged.", "SSH_KEY_ID_REQUIRED", {"http_status": 400})


def _emit(as_json: bool, data: Any, human: str) -> None:
    click.echo(json.dumps(data, indent=2, default=str) if as_json else human)


def _state(ctx: click.Context) -> AppState:
    return ctx.ensure_object(AppState)


def _confirm(yes: bool, question: str) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise Fail(2, f"{question} Not a terminal: pass -y to confirm.")
    if not click.confirm(question, default=False):
        raise click.Abort()


def _find(ctx: click.Context, ref: str) -> Instance:
    return find_instance(_state(ctx).client(), ref)


def _reachable(inst: Instance):
    target = inst.target
    if target is None:
        raise Fail(7, f"{inst.name or inst.id} is not reachable over SSH yet (status {inst.status}). Wait, then run `vgpu ps`.")
    return target


def _need(tool: str) -> str:
    path = _tool(tool)
    if not path:
        raise Fail(9, f"`{tool}` was not found on PATH (OpenSSH client). Install it, then retry.")
    return path


def _identity(identity: Optional[str]) -> Optional[str]:
    return identity or os.environ.get("VGPU_SSH_KEY") or None


def _money(value: Optional[float]) -> str:
    return f"${value:.2f}/h" if isinstance(value, (int, float)) else "?"


@click.group(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 110})
@click.version_option(__version__, "-V", "--version", prog_name="vgpu")
@click.option("--base-url", envvar="VOLT_BASE_URL", hidden=True)
@click.pass_context
def vgpu(ctx: click.Context, base_url: Optional[str]) -> None:
    """VoltageGPU for scripts and AI agents: Confidential VMs, Confidential CPU, standard GPUs, dedicated machines, sandboxes.

    \b
    Always give --ttl, release with `vgpu rm` when done, stop on exit code 6
    (budget or balance), and call a machine confidential only after `vgpu attest`
    returns VERIFIED. Exit codes: 0 ok, 1 error, 2 usage, 3 key, 4 scope,
    5 not found, 6 budget/balance (402), 7 unavailable, 8 attestation failed,
    9 attestation incomplete or tool missing.
    """
    state = ctx.ensure_object(AppState)
    state.base_url = base_url
    ctx.call_on_close(state.close)


@vgpu.command("ls")
@click.option("--kind", type=click.Choice(KINDS), help="Only this kind of machine.")
@click.option("--gpu", help="GPU model contains this text (H100, a5000...).")
@click.option("--max-price", type=float, help="Only offers at or under this price per hour (USD).")
@click.option("--all", "all_", is_flag=True, help="Include sold-out offers.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def ls_cmd(ctx: click.Context, kind: Optional[str], gpu: Optional[str], max_price: Optional[float], all_: bool, as_json: bool) -> None:
    """Offers with the price charged, cheapest first."""
    offers, errors = list_offers(_state(ctx).client(), (kind,) if kind else KINDS)
    offers = filter_offers(offers, gpu=gpu, max_price=max_price, include_sold_out=all_)
    for err in errors:
        click.echo(f"warning: {scrub(err)}", err=True)
    lines = [f"{'KIND':<10} {'TIER':<38} {'PRICE':>10}  {'AVAIL':>5}  NAME"]
    lines += [f"{o.kind:<10} {o.tier:<38} {_money(o.price_per_hour):>10}  {'' if o.available is None else o.available:>5}  "
              f"{o.name}{'  [' + o.tee + ']' if o.tee else ''}" for o in offers]
    _emit(as_json, [o.to_dict() for o in offers], "\n".join(lines) if offers else "No offer matches.")


def _wait_ready_client(client: Any, pod_id: str, timeout: int) -> Optional[Instance]:
    for _ in range(max(1, timeout // 10)):
        inst = resolve(list_instances(client), pod_id)
        if inst is not None and inst.target is not None:
            return inst
        _sleep(10)
    return None


@vgpu.command("up")
@click.argument("tier")
@click.option("--name", required=True, help="Your name for the machine (lowercase, digits, hyphens).")
@click.option("--ttl", required=True, help="Deadline: 45m, 1h, 6h, 2d. Mandatory: the machine stops by itself then.")
@click.option("--kind", type=click.Choice(KINDS), help="Look the tier up in this kind only.")
@click.option("--image", help="Sandbox image (ubuntu-24.04 by default).")
@click.option("--max-price", type=float, help="Refuse if the hourly price is above this (USD).")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation (required when not in a terminal).")
@click.option("--wait/--no-wait", default=True, help="Wait until SSH answers (default).")
@click.option("--timeout", default=900, show_default=True, help="Seconds to wait for SSH.")
@click.option("--ssh-key-id", "ssh_key_ids", multiple=True,
              help="SSH key id to bind: one for a standard GPU (needed when the account has several keys); "
                   "repeatable for dedicated machines and sandboxes.")
@click.option("--show-secret", is_flag=True, help="Print the sudo password of a Confidential VM (shown once, never stored).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def up_cmd(ctx: click.Context, tier: str, name: str, ttl: str, kind: Optional[str], image: Optional[str], max_price: Optional[float],
           yes: bool, wait: bool, timeout: int, ssh_key_ids: tuple, show_secret: bool, as_json: bool) -> None:
    """Start a machine. Charges the first hour now; the unused part comes back when released."""
    check_name(name)
    seconds = parse_ttl(ttl)
    client = _state(ctx).client()
    offers, _ = list_offers(client, (kind,) if kind else KINDS)
    offer = next((o for o in offers if o.tier == tier), None)
    if offer is None:
        raise Fail(5, f"Unknown tier {tier!r}. Run `vgpu ls` for the current offers.")
    if not offer.in_stock:
        raise Fail(7, f"{tier} is sold out right now. Run `vgpu ls` for what is available.", "SOLD_OUT")
    if max_price is not None and (offer.price_per_hour is None or offer.price_per_hour > max_price):
        raise Fail(7, f"{tier} costs {_money(offer.price_per_hour)}, above --max-price {max_price}.", "OVER_MAX_PRICE")
    plan = plan_ttl(offer.kind, seconds)
    names = NameStore()
    known = names.load().get(name)
    if known and resolve(list_instances(client), known.get("id", "")):
        raise Fail(2, f"A running machine is already named {name!r}. Pick another --name or `vgpu rm {name} -y`.")
    _confirm(yes, f"Start {offer.kind} {tier} at {_money(offer.price_per_hour)} (first hour prepaid), stopping after {ttl}?")
    try:
        result = deploy(client, offer, name, plan, image=image, ssh_key_ids=list(ssh_key_ids) or None)
    except ApiError as exc:
        if exc.status_code == 0:
            raise Fail(1, "The deploy request got no answer: a machine may have started and been charged. Run `vgpu ps` "
                          "before trying again; vgpu never retries a deploy by itself.", "NO_ANSWER") from None
        needed = ssh_key_needed(exc, offer.kind, bool(ssh_key_ids), "--ssh-key-id")
        if needed:
            raise needed from None
        raise
    secret = result.pop("_sudo_password", None)
    names.put(name, {"id": result["id"], "kind": result["kind"], "tier": tier, "created_at": datetime.now(timezone.utc).isoformat()})
    ready = _wait_ready_client(client, result["id"], timeout) if wait else None
    result.update({"ssh": ready.ssh if ready else None, "ssh_ready": bool(ready), "status": ready.status if ready else "STARTING"})
    if show_secret and secret:
        result["sudo_password"] = secret
    human = (f"Started {name} ({result['id']}), {offer.kind} {tier}, {_money(result['hourly_price'])}, stops after {ttl}.\n"
             + (f"SSH: {result['ssh']}" if ready else "Not reachable yet: `vgpu ps` shows when SSH is ready."))
    _emit(as_json, result, human)


@vgpu.command("ps")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def ps_cmd(ctx: click.Context, as_json: bool) -> None:
    """Machines of this key (or of the account for a full key)."""
    rows = list_instances(_state(ctx).client())
    lines = [f"{'NAME':<20} {'ID':<38} {'KIND':<10} {'STATUS':<10} {'PRICE':>10}  SSH"]
    lines += [f"{(i.name or '-'):<20} {i.id:<38} {i.kind:<10} {i.status:<10} {_money(i.hourly_price):>10}  "
              f"{i.ssh if i.ssh_ready else '-'}" for i in rows]
    _emit(as_json, [i.to_dict() for i in rows], "\n".join(lines) if rows else "No machine.")


@vgpu.command("exec", context_settings={"ignore_unknown_options": True})
@click.argument("ref")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.option("-d", "--detach", is_flag=True, help="Start in the background; prints the PID and the log path.")
@click.option("-i", "--identity", help="Private key file (default: VGPU_SSH_KEY, then your ssh defaults).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def exec_cmd(ctx: click.Context, ref: str, command: tuple, detach: bool, identity: Optional[str], as_json: bool) -> None:
    """Run a command on the machine over SSH, with your local key."""
    text = command[0] if len(command) == 1 else shlex.join(command)
    inst = _find(ctx, ref)
    target = _reachable(inst)
    ssh = _need("ssh")
    if not detach:
        proc = _run(exec_argv(target, inst.id, text, identity=_identity(identity), ssh_bin=ssh))
        ctx.exit(proc.returncode)
    job = secrets.token_hex(4)
    remote, log = detached_command(text, job)
    proc = _run(exec_argv(target, inst.id, remote, identity=_identity(identity), ssh_bin=ssh), capture_output=True, timeout=120)
    out = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    if proc.returncode != 0 or not out or not out[-1].strip().isdigit():
        raise Fail(1, f"Could not start the background command (ssh exit {proc.returncode}).")
    data = {"name": inst.name, "id": inst.id, "job": job, "pid": int(out[-1]), "log": log}
    _emit(as_json, data, f"Started PID {data['pid']}, log {log}. Read it with: vgpu exec {ref} 'tail -n 50 {log}'")


@vgpu.command("scp")
@click.argument("ref")
@click.argument("source")
@click.argument("destination")
@click.option("--up", "upload", is_flag=True, help="Copy local SOURCE to remote DESTINATION (default: download).")
@click.option("-r", "--recursive", is_flag=True)
@click.option("-i", "--identity", help="Private key file (default: VGPU_SSH_KEY).")
@click.pass_context
def scp_cmd(ctx: click.Context, ref: str, source: str, destination: str, upload: bool, recursive: bool, identity: Optional[str]) -> None:
    """Copy files: `vgpu scp N /remote/path ./local` (or --up ./local /remote/path)."""
    inst = _find(ctx, ref)
    target = _reachable(inst)
    argv = scp_argv(target, inst.id, source, destination, upload=upload, recursive=recursive,
                    identity=_identity(identity), scp_bin=_need("scp"))
    ctx.exit(_run(argv).returncode)


@vgpu.command("rm")
@click.argument("ref")
@click.option("-y", "--yes", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def rm_cmd(ctx: click.Context, ref: str, yes: bool, as_json: bool) -> None:
    """Release a machine (the unused prepaid time of a standard pod is refunded)."""
    inst = _find(ctx, ref)
    _confirm(yes, f"Release {inst.name or inst.id} ({inst.kind})? Its disk is deleted.")
    data = release(_state(ctx).client(), inst)
    NameStore().remove_id(inst.id)
    _emit(as_json, data, f"Released {inst.name or inst.id}.")


def _attest_failure(kind: str, challenge: str, check: str, detail: str) -> Tuple[Dict[str, Any], int]:
    return {"verified": False, "status": "NOT_VERIFIED", "tee": attest.TEE[kind], "challenge": challenge, "failed": [check], "detail": detail}, 8


def find_instance(client: Any, ref: str) -> Instance:
    inst = resolve(list_instances(client), ref)
    if inst is None:
        raise Fail(5, f"No machine named or with id {ref!r} for this key. Run `vgpu ps`.")
    return inst


def attest_machine(client: Any, ref: str, *, out: Optional[str], offline: bool, identity: Optional[str],
                   vmpl: int = 0) -> Tuple[Dict[str, Any], int]:
    """The whole `vgpu attest` (CLI and local MCP server): result and exit code (0, 8 or 9).

    ``vmpl``: Confidential CPU only, the VMPL level the SEV-SNP report is requested at (SNP_VMPL).
    """
    inst = find_instance(client, ref)
    try:
        script = attest.load_script(inst.kind)
    except attest.AttestError as exc:
        raise Fail(2 if inst.kind not in attest.SCRIPTS else 9, str(exc)) from None
    target = _reachable(inst)
    ssh, scp = _need("ssh"), _need("scp")
    challenge = attest.new_challenge(inst.kind)
    folder = Path(out or f"vgpu-attest-{inst.id}-{int(time.time())}")
    folder.mkdir(parents=True, exist_ok=True)
    key = _identity(identity)
    level = vmpl if inst.kind == "cpu" else None
    proc = _run(exec_argv(target, inst.id, attest.remote_command(challenge, level), identity=key, ssh_bin=ssh),
                input=script, capture_output=True, timeout=1800)
    log = folder / "in-the-vm.log"
    log.write_bytes((proc.stdout or b"") + (proc.stderr or b""))
    if proc.returncode != 0 and inst.kind == "cpu" and proc.returncode in attest.SNP_SCRIPT_EXIT:
        name = attest.SNP_SCRIPT_EXIT[proc.returncode]
        return _attest_failure(inst.kind, challenge, name,
                               f"the proof script refused the report ({name}, exit {proc.returncode}, SNP_VMPL={level}); see {log}")
    if proc.returncode != 0:
        return _attest_failure(inst.kind, challenge, "in_vm_script", f"the proof script exited with {proc.returncode}; see {log}")
    archive = folder / Path(attest.ARCHIVES[inst.kind]).name
    copy = _run(scp_argv(target, inst.id, attest.ARCHIVES[inst.kind], str(archive), identity=key, scp_bin=scp))
    if copy.returncode != 0 or not archive.is_file():
        return _attest_failure(inst.kind, challenge, "archive_copy", f"could not copy {attest.ARCHIVES[inst.kind]} (scp exit {copy.returncode})")
    try:
        extracted = attest.extract_archive(archive, folder / "proof")
        if inst.kind == "vm":
            result = attest.verify_gpu(extracted, challenge, offline=offline)
        else:
            result = attest.verify_cpu(extracted, challenge, snpguest=_tool("snpguest"), runner=_run, vmpl=vmpl)
    except attest.ToolMissing as exc:
        raise Fail(9, str(exc)) from None
    except attest.AttestError as exc:
        return _attest_failure(inst.kind, challenge, "archive", str(exc))
    data = {"name": inst.name, "id": inst.id, **result.to_dict(), "folder": str(folder)}
    return data, 0 if result.verified else 9 if result.status == "INCOMPLETE" else 8


@vgpu.command("attest")
@click.argument("ref")
@click.option("--offline", is_flag=True, help="GPU: use the collateral inside the bundle, no network call.")
@click.option("--out", type=click.Path(file_okay=False), help="Folder for the archive and the report.")
@click.option("-i", "--identity", help="Private key file (default: VGPU_SSH_KEY).")
@click.option("--vmpl", type=click.IntRange(0, 3), default=0, show_default=True,
              help="Confidential CPU: VMPL level the SEV-SNP report is requested at (the script asks again at 1 if 0 is refused).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def attest_cmd(ctx: click.Context, ref: str, offline: bool, out: Optional[str], identity: Optional[str], vmpl: int,
               as_json: bool) -> None:
    """Prove the machine is a genuine TEE: challenge drawn here, proofs made in the VM, checked here."""
    data, code = attest_machine(_state(ctx).client(), ref, out=out, offline=offline, identity=identity, vmpl=vmpl)
    failed = data.get("failed") or []
    human = f"{data['status']}: {data['tee']}, challenge {data['challenge'][:16]}..." + (f" failed: {', '.join(failed)}" if failed else "")
    if data.get("detail"):
        human += f" ({data['detail']})"
    if (data.get("facts") or {}).get("vmpl_fallback"):
        human += " (VMPL 0 refused: the guest is not at VMPL 0; report requested at VMPL 1)"
    _emit(as_json, data, human)
    raise click.exceptions.Exit(code)


@vgpu.command("whoami")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def whoami_cmd(ctx: click.Context, as_json: bool) -> None:
    """The key's scopes, visibility, budgets and spend, and the balance (the key itself is never shown)."""
    state = _state(ctx)
    client = state.client()
    try:
        raw = client.call("GET", "/volt/whoami")
    except ApiError as exc:
        if exc.status_code != 404:
            raise
        bal = client.get_balance()
        raw = {"balance": bal.balance, "balance_hidden": bal.balance_hidden, "key": {}}
    key = raw.get("key") if isinstance(raw, Mapping) and isinstance(raw.get("key"), Mapping) else {}
    data: Dict[str, Any] = {"key": {**key, "masked": client.config.masked_key}, "balance": raw.get("balance"),
                            "currency": raw.get("currency", "USD"), "base_url": client.config.base_url}
    if raw.get("balance") is None:
        data["balance_hidden"] = raw.get("balance_hidden") or "This key cannot read the account balance."
    lines: List[str] = [f"key       {data['key']['masked']} {key.get('name') or ''}".rstrip(),
                        f"scopes    {', '.join(key.get('scopes') or []) or 'full access'}",
                        f"machines  {key.get('pod_visibility') or 'account'}",
                        f"budgets   {json.dumps(key.get('budgets_usd') or {})}  spent {json.dumps(key.get('spent_usd') or {})}",
                        f"balance   {data['balance']} {data['currency']}" if data["balance"] is not None
                        else f"balance   hidden ({data['balance_hidden']})"]
    _emit(as_json, data, "\n".join(lines))


@vgpu.command("mcp")
def mcp_cmd() -> None:
    """Local MCP server over stdio (ls, up, ps, exec, scp, rm, attest, inference, whoami) for AI agents."""
    from ..mcp.server import run_stdio

    run_stdio()


def _report(fail: Fail, as_json: bool) -> int:
    message = scrub(fail.message)
    if as_json:
        click.echo(json.dumps({"error": {"code": fail.code, "message": message, "exit_code": fail.exit_code, **fail.extra}}, indent=2))
    click.echo(f"Error: {message}", err=True)
    return fail.exit_code


def main(argv: Optional[List[str]] = None) -> None:
    configure_stream(sys.stdout)
    configure_stream(sys.stderr)
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    debug = os.environ.get("VOLT_DEBUG") == "1"
    try:
        code = vgpu.main(args=args, prog_name="vgpu", standalone_mode=False)
        sys.exit(code if isinstance(code, int) else 0)
    except click.exceptions.Exit as exc:
        sys.exit(exc.exit_code)
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        sys.exit(1)
    except click.ClickException as exc:
        exc.show()
        sys.exit(2 if isinstance(exc, click.UsageError) else exc.exit_code)
    except Fail as fail:
        sys.exit(_report(fail, as_json))
    except ApiError as exc:
        if debug:
            raise
        sys.exit(_report(api_fail(exc), as_json))
    except ConfigError as exc:
        sys.exit(_report(Fail(3, str(exc), "API_KEY_MISSING"), as_json))
    except (TtlError, RemotePathError, ValueError) as exc:
        sys.exit(_report(Fail(2, str(exc)), as_json))
    except subprocess.TimeoutExpired:
        sys.exit(_report(Fail(1, "The remote command timed out."), as_json))
    except KeyboardInterrupt:
        click.echo("Interrupted.", err=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
