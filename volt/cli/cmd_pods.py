"""`voltagegpu pods ...` and `voltagegpu ssh`."""

from __future__ import annotations

import subprocess
from typing import Optional, Tuple

import click

from ..sdk import ssh as ssh_lookup
from ..sdk.models import Pod
from ..sdk.ssh import UnsafeSshCommand, parse_ssh_command
from .output import console, echo_json, esc, fail, hint, info, is_interactive, money, status_style, styled, table
from .state import AppState, pass_state

REJECTED_SSH = ("The API returned an SSH command of an unexpected form, so it is not shown or run. "
                "Contact contact@voltagegpu.com.")


class AliasedGroup(click.Group):
    """`pods get` = `pods show`, `pods delete` / `pods release` = `pods stop` (earlier releases)."""

    ALIASES = {"get": "show", "delete": "stop", "release": "stop", "ls": "list"}

    def get_command(self, ctx: click.Context, cmd_name: str) -> Optional[click.Command]:
        return super().get_command(ctx, self.ALIASES.get(cmd_name, cmd_name))

    def resolve_command(self, ctx: click.Context, args: list):  # type: ignore[override]
        _, cmd, rest = super().resolve_command(ctx, args)
        return (cmd.name if cmd else None), cmd, rest


@click.group("pods", cls=AliasedGroup)
def pods_group() -> None:
    """List, inspect, wait for and stop your pods."""


@pods_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_state
def pods_list(state: AppState, as_json: bool) -> None:
    """Your running pods and Confidential VMs."""
    pods = state.client().list_pods()
    if as_json:
        echo_json([p.to_dict() for p in pods])
        return
    if not pods:
        info("No running pod.")
        hint("Deploy one: voltagegpu deploy standard --cheapest   or   voltagegpu deploy confidential --tier TIER")
        return
    table(
        "Your pods",
        [("POD ID", {"style": "cyan", "no_wrap": True}), ("TYPE",), ("STATUS",), ("GPU",),
         ("$/HOUR", {"justify": "right", "style": "green"}), ("SSH",)],
        [
            (p.id, "confidential VM" if p.confidential else "standard", styled(p.provider_status or p.status, status_style(p.status)),
             f"{p.gpu_count}x {p.gpu_type}" if p.gpu_count and p.gpu_type else (p.resource_name or p.gpu_type),
             money(p.hourly_price), "ready" if p.reachable else "not yet")
            for p in pods
        ],
    )
    hint("SSH into one: voltagegpu ssh POD_ID     Stop one: voltagegpu pods stop POD_ID")


def _show(pod: Pod) -> None:
    status = f"[{status_style(pod.status)}]{esc(pod.status)}[/]" + (f" ({esc(pod.provider_status)})" if pod.provider_status else "")
    ssh = "rejected (unexpected form from the API)" if pod.ssh_command_rejected else (pod.ssh_command if pod.reachable else "not ready yet")
    rows = [
        ("Pod", esc(pod.id)),
        ("Type", "Confidential VM (Intel TDX)" if pod.confidential else "Standard pod (no enclave)"),
        ("Status", status),
        ("Name", esc(pod.name) if pod.name else None),
        ("Tier", esc(pod.resource_name) if pod.confidential and pod.resource_name else None),
        ("GPU", esc(f"{pod.gpu_count}x {pod.gpu_type}" if pod.gpu_count and pod.gpu_type else pod.gpu_type or "") or None),
        ("Price", f"{money(pod.hourly_price)}/hour" if pod.hourly_price is not None else None),
        ("SSH", esc(ssh)),
        ("Jupyter", esc(pod.jupyter_url) if pod.jupyter_url else None),
        ("Created", esc(pod.created_at) if pod.created_at else None),
    ]
    for label, value in rows:
        if value:
            console.print(f"[bold]{label:<8}[/bold] {value}")


@pods_group.command("show")
@click.argument("pod_id")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_state
def pods_show(state: AppState, pod_id: str, as_json: bool) -> None:
    """Details of one pod, including its SSH command."""
    pod = state.client().get_pod(pod_id)
    if as_json:
        echo_json(pod.to_dict())
    else:
        _show(pod)


@pods_group.command("wait")
@click.argument("pod_id")
@click.option("--timeout", type=click.IntRange(10, 3600), default=900, show_default=True, help="Seconds before giving up.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_state
def pods_wait(state: AppState, pod_id: str, timeout: int, as_json: bool) -> None:
    """Wait until a pod accepts SSH, then print the command."""
    last = {"s": ""}

    def on_poll(pod: Pod) -> None:
        label = pod.provider_status or pod.status
        if pod.ssh_command_rejected:
            fail(REJECTED_SSH)
        if not as_json and label != last["s"]:
            last["s"] = label
            console.print(f"  status: [{status_style(pod.status)}]{esc(label)}[/]")

    try:
        pod = state.client().wait_until_reachable(pod_id, timeout=timeout, interval=10.0, on_poll=on_poll)
    except TimeoutError as exc:
        fail(str(exc))
    if as_json:
        echo_json(pod.to_dict())
    else:
        console.print(f"[green]Ready.[/green] {esc(pod.ssh_command)}")


@pods_group.command("stop")
@click.argument("pod_id")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_state
def pods_stop(state: AppState, pod_id: str, yes: bool, as_json: bool) -> None:
    """Stop and release a pod. Billing stops; data on the pod is deleted."""
    if not yes:
        if not is_interactive():
            fail("Stopping deletes the pod and its data. Re-run with --yes to confirm in a script.", code=2)
        if not click.confirm(f"Stop and release {pod_id}? Its disk is deleted.", default=False):
            raise click.exceptions.Exit(1)
    result = state.client().release_pod(pod_id)
    if as_json:
        echo_json(result.to_dict())
        return
    console.print(f"[green]Released.[/green] {esc(pod_id)}")
    if result.prepaid_refund is not None:
        info(f"Unused prepaid time refunded: {money(result.prepaid_refund, 4)}")


@click.command("ssh", context_settings={"ignore_unknown_options": False})
@click.argument("pod_id")
@click.argument("remote_command", nargs=-1)
@click.option("-i", "--identity", type=click.Path(dir_okay=False), help="Private key file passed to ssh -i.")
@click.option("-o", "--ssh-option", "ssh_options", multiple=True, metavar="KEY=VALUE",
              help="Extra ssh -o option, repeatable, e.g. StrictHostKeyChecking=accept-new.")
@click.option("--print", "print_only", is_flag=True, help="Print the ssh command instead of running it.")
@pass_state
def ssh_cmd(state: AppState, pod_id: str, remote_command: Tuple[str, ...], identity: Optional[str],
            ssh_options: Tuple[str, ...], print_only: bool) -> None:
    """Open an SSH session to a pod (or run REMOTE_COMMAND on it).

    \b
    Examples:
      voltagegpu ssh POD_ID
      voltagegpu ssh POD_ID -- nvidia-smi
      voltagegpu ssh POD_ID -o StrictHostKeyChecking=accept-new -- nvidia-smi -L
      voltagegpu ssh POD_ID --print
    """
    for opt in ssh_options:
        if "=" not in opt or opt.startswith("-"):
            fail(f"--ssh-option expects KEY=VALUE, got {opt!r}", code=2)
    pod = state.client().get_pod(pod_id)
    if pod.ssh_command_rejected:
        fail("Refusing to run it: " + REJECTED_SSH)
    if not pod.reachable or not pod.ssh_command:
        fail(f"SSH is not ready on {pod_id} yet (status {pod.provider_status or pod.status}). "
             f"Wait with: voltagegpu pods wait {pod_id}")
    try:
        target = parse_ssh_command(pod.ssh_command)
    except UnsafeSshCommand as exc:
        fail(f"Refusing to run the SSH command returned by the API ({exc}).")
    if print_only:
        click.echo(target.display(identity))
        return
    binary = ssh_lookup.find_ssh_binary()  # PATH only, never the current directory
    if not binary:
        fail("No ssh client found on PATH. Install OpenSSH, or use: voltagegpu ssh POD_ID --print")
    argv = target.argv(identity_file=identity, remote_command=remote_command, options=ssh_options)
    argv[0] = binary
    # A list, never a shell string: nothing from the API is interpreted by a shell.
    code = subprocess.call(argv)  # noqa: S603
    raise click.exceptions.Exit(code)
