"""`voltagegpu deploy standard` and `voltagegpu deploy confidential`."""

from __future__ import annotations

import re
import secrets
from typing import Any, Dict, List, Optional, Tuple

import click

from ..sdk.errors import ApiError
from ..sdk.models import CONFIDENTIAL, STANDARD, DeployResult, Machine, Pod
from .cmd_machines import filter_machines
from .output import console, echo_json, fail, hint, info, is_interactive, money, status_style, warn
from .state import AppState, pass_state

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_PORT_RE = re.compile(r"^(\d{1,5})(?:/(tcp|udp))?$", re.IGNORECASE)


def default_name() -> str:
    return f"pod-{secrets.token_hex(3)}"


def check_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise click.BadParameter("use 1-40 lowercase letters, digits and hyphens, starting with a letter or digit", param_hint="--name")
    return name


def parse_port_spec(spec: str) -> Dict[str, Any]:
    """'8000' / '8000/tcp' / '53/udp' -> {"port": 8000, "protocol": "tcp"}."""
    match = _PORT_RE.match(spec.strip())
    if not match:
        raise click.BadParameter(f"{spec!r}: expected PORT or PORT/tcp or PORT/udp", param_hint="--port")
    port = int(match.group(1))
    if not 1 <= port <= 65535:
        raise click.BadParameter(f"{port} is not between 1 and 65535", param_hint="--port")
    if port == 22:
        raise click.BadParameter("port 22 (SSH) is already mapped for you", param_hint="--port")
    return {"port": port, "protocol": (match.group(2) or "tcp").lower()}


def pick_machine(machines: List[Machine], machine_id: Optional[str], cheapest: bool, gpu: Optional[str],
                 max_price: Optional[float]) -> Machine:
    """The machine to deploy: an explicit id, or the cheapest one matching the filters."""
    if machine_id:
        for m in machines:
            if m.id == machine_id:
                return m
        raise LookupError(f"Machine {machine_id} is not in the deployable list right now. "
                          "List machines with: voltagegpu machines --standard")
    candidates = filter_machines(machines, gpu=gpu, max_price=max_price)
    priced = [m for m in candidates if m.price_per_hour is not None]
    if not priced:
        raise LookupError("No standard machine matches these filters right now.")
    return min(priced, key=lambda m: m.price_per_hour or 0.0)


def _confirm_charge(amount: Optional[float], yes: bool) -> None:
    if yes:
        return
    if not is_interactive():
        fail("This deploy charges your balance. Re-run with --yes to confirm in a script.", code=2)
    if not click.confirm(f"Charge {money(amount)} now (first hour, prepaid) and deploy?", default=False):
        raise click.exceptions.Exit(1)


def _wait(state: AppState, pod_id: str, timeout: int) -> Pod:
    seen: Dict[str, str] = {}

    def on_poll(pod: Pod) -> None:
        label = pod.provider_status or pod.status
        if seen.get("s") != label:
            seen["s"] = label
            console.print(f"  status: [{status_style(pod.status)}]{label}[/]")

    info(f"Waiting for SSH on {pod_id} (up to {timeout} s)...")
    return state.client().wait_until_reachable(pod_id, timeout=timeout, interval=10.0, on_poll=on_poll)


def _after_deploy(state: AppState, result: DeployResult, wait: bool, timeout: int, as_json: bool) -> None:
    pod: Optional[Pod] = None
    if wait:
        try:
            pod = _wait(state, result.pod_id, timeout) if not as_json else state.client().wait_until_reachable(result.pod_id, timeout=timeout)
        except TimeoutError as exc:
            warn(str(exc))
    if as_json:
        echo_json({"deploy": result.to_dict(), "pod": pod.to_dict() if pod else None})
        return
    if pod and pod.ssh_command:
        console.print(f"[green]Ready.[/green] SSH: {pod.ssh_command}")
    info("")
    info("Next:")
    info(f"  voltagegpu pods wait {result.pod_id}     # until SSH is ready" if not pod else f"  voltagegpu ssh {result.pod_id}")
    info(f"  voltagegpu pods stop {result.pod_id}     # release it (billing stops)")


@click.group("deploy")
def deploy_group() -> None:
    """Deploy a standard pod or a Confidential VM (charges the first hour)."""


@deploy_group.command("standard")
@click.option("--machine", "machine_id", help="machine_id from `voltagegpu machines --standard`.")
@click.option("--cheapest", is_flag=True, help="Pick the cheapest available machine (combine with --gpu/--max-price).")
@click.option("--gpu", help="With --cheapest: GPU model filter, e.g. A6000.")
@click.option("--max-price", type=float, help="With --cheapest: maximum price per hour (USD).")
@click.option("--name", help="Pod name: lowercase letters, digits, hyphens (default: pod-xxxxxx).")
@click.option("--template", "template_id", help="Template id from `voltagegpu templates` (default: PyTorch).")
@click.option("--ssh-key", "ssh_key_id", help="SSH key id (required if your account has several keys).")
@click.option("--jupyter", is_flag=True, default=None, help="Start Jupyter on the pod.")
@click.option("--ports", "port_count", type=click.IntRange(1, 31), help="Number of extra public ports to map.")
@click.option("--auto-terminate", "auto_terminate_hours", type=click.IntRange(1, 720), help="Release automatically after N hours.")
@click.option("--wait", is_flag=True, help="Wait until SSH is ready and print the command.")
@click.option("--timeout", type=click.IntRange(30, 3600), default=900, show_default=True, help="Seconds to wait with --wait.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation before charging.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_state
def deploy_standard(state: AppState, machine_id: Optional[str], cheapest: bool, gpu: Optional[str], max_price: Optional[float],
                    name: Optional[str], template_id: Optional[str], ssh_key_id: Optional[str], jupyter: Optional[bool],
                    port_count: Optional[int], auto_terminate_hours: Optional[int], wait: bool, timeout: int,
                    yes: bool, as_json: bool) -> None:
    """Deploy a standard pod: a whole machine, no enclave, for non-sensitive workloads."""
    if bool(machine_id) == bool(cheapest):
        fail("Give exactly one of --machine MACHINE_ID or --cheapest.", code=2)
    pod_name = check_name(name) if name else default_name()
    client = state.client()
    try:
        machine = pick_machine(client.list_machines(STANDARD), machine_id, cheapest, gpu, max_price)
    except LookupError as exc:
        fail(str(exc))
    if not as_json:
        info(f"Standard pod [bold]{pod_name}[/bold] on {machine.gpu_count}x {machine.gpu_type}"
             f"{' (' + machine.location + ')' if machine.location else ''}, {money(machine.price_per_hour)}/hour")
        hint(f"machine {machine.id}. No enclave: use a Confidential VM for sensitive data.")
    _confirm_charge(machine.price_per_hour, yes)
    result = client.deploy_standard(
        machine.id, pod_name, template_id=template_id, ssh_key_id=ssh_key_id, gpu_count=None,
        jupyter=jupyter, port_count=port_count, auto_terminate_hours=auto_terminate_hours,
    )
    if not as_json:
        console.print(f"[green]Deployed.[/green] Pod {result.pod_id}, {money(result.hourly_price)}/hour, "
                      f"{money(result.prepaid_amount)} prepaid.")
    _after_deploy(state, result, wait, timeout, as_json)


@deploy_group.command("confidential")
@click.option("--tier", required=True, help="Tier from `voltagegpu machines --confidential`, e.g. rtx6000b-small.")
@click.option("--name", help="VM name: lowercase letters, digits, hyphens (default: pod-xxxxxx).")
@click.option("--hardstop", "hardstop_hours", type=click.IntRange(0, 720), help="Release automatically after N hours.")
@click.option("--port", "ports", multiple=True, metavar="PORT[/tcp|udp]", help="Extra inbound port, repeatable (single-GPU tiers).")
@click.option("--wait", is_flag=True, help="Wait until SSH is ready and print the command.")
@click.option("--timeout", type=click.IntRange(30, 3600), default=900, show_default=True, help="Seconds to wait with --wait.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation before charging.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (includes the one-time sudo password).")
@pass_state
def deploy_confidential(state: AppState, tier: str, name: Optional[str], hardstop_hours: Optional[int], ports: Tuple[str, ...],
                        wait: bool, timeout: int, yes: bool, as_json: bool) -> None:
    """Deploy a Confidential VM: Intel TDX, your SSH keys only, attestation you verify yourself."""
    pod_name = check_name(name) if name else default_name()
    port_list = [parse_port_spec(p) for p in ports]
    client = state.client()
    tiers = {m.id: m for m in client.list_machines(CONFIDENTIAL)}
    machine = tiers.get(tier)
    if machine is None:
        fail(f"'{tier}' is not an available Confidential VM tier right now. List tiers with: voltagegpu machines --confidential")
    if not client.list_ssh_keys():
        fail("Add an SSH key first (voltagegpu ssh-keys add). A Confidential VM binds your keys at creation "
             "and cannot be given one afterwards.")
    if not as_json:
        info(f"Confidential VM [bold]{pod_name}[/bold] on {machine.id} ({machine.gpu_count}x {machine.gpu_type}), "
             f"{money(machine.price_per_hour)}/hour")
        hint("Every SSH key on your account is bound to the VM. We keep no key on it.")
    _confirm_charge(machine.price_per_hour, yes)
    result = client.deploy_confidential(tier, pod_name, hardstop_hours=hardstop_hours, ports=port_list)
    if not as_json:
        console.print(f"[green]Deployed.[/green] VM {result.pod_id}, {money(result.hourly_price)}/hour, booting.")
        if result.sudo_password:
            console.print(f"  sudo password (shown once, we do not keep it): [bold]{result.sudo_password}[/bold]")
        if result.hard_stop_at:
            info(f"  automatic release at {result.hard_stop_at}")
        commands = (result.attestation or {}).get("verifyInsideTheVm")
        if isinstance(commands, list) and commands:
            hint("Verify the attestation inside the VM (then check the bundle with `voltagegpu verify`):")
            for line in commands:
                hint(f"  {line}")
    _after_deploy(state, result, wait, timeout, as_json)


__all__ = ["deploy_group", "parse_port_spec", "pick_machine", "check_name", "default_name", "ApiError"]
