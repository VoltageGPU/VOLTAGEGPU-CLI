"""`login`, `logout`, `config`, `balance` and `ssh-keys`."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ..sdk.config import (
    API_KEYS_PAGE,
    Config,
    default_config_path,
    is_valid_api_key,
    permissions_warning,
    remove_api_key,
    save_api_key,
)
from ..sdk.errors import ApiError, ConfigError
from .output import console, echo_json, fail, hint, info, is_interactive, money, table, warn
from .state import AppState, pass_state

_PUBLIC_KEY_PREFIXES = ("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-", "sk-ssh-ed25519@openssh.com ", "sk-ecdsa-sha2-")
_DEFAULT_PUBLIC_KEYS = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")


def read_public_key(text: str) -> str:
    """Validate an OpenSSH public key; refuse private keys loudly."""
    key = (text or "").strip()
    if "PRIVATE KEY" in key:
        raise click.BadParameter("this is a PRIVATE key. Never upload it: pass the matching .pub file instead.")
    lines = [line for line in key.splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0].startswith(_PUBLIC_KEY_PREFIXES) or len(lines[0]) > 16384:
        raise click.BadParameter("expected one OpenSSH public key line, e.g. 'ssh-ed25519 AAAA... you@laptop'.")
    return lines[0].strip()


def default_public_key_file() -> Optional[Path]:
    ssh_dir = Path.home() / ".ssh"
    for name in _DEFAULT_PUBLIC_KEYS:
        candidate = ssh_dir / name
        if candidate.is_file():
            return candidate
    return None


@click.command("login")
@click.option("--api-key", envvar=None, help="Key to store (prompted, hidden, if omitted). Prefer the prompt: "
              "a key on the command line lands in your shell history.")
@pass_state
def login_cmd(state: AppState, api_key: Optional[str]) -> None:
    """Store your API key in ~/.volt/config.ini (readable by you only)."""
    if not api_key:
        if not is_interactive():
            fail("No terminal to prompt on. Export VOLT_API_KEY instead, or pass --api-key.", code=2)
        info(f"Create a key at {API_KEYS_PAGE} (it is shown once).")
        api_key = click.prompt("API key", hide_input=True).strip()
    if not is_valid_api_key(api_key):
        fail(f"That does not look like a VoltageGPU API key (volt_...). Create one at {API_KEYS_PAGE}.")
    try:
        with state.client_with_key(api_key) as client:
            client.list_ssh_keys()  # cheapest authenticated call: proves the key works
    except ApiError as exc:
        if exc.is_auth_error:
            fail("The API refused this key. Check it on " + API_KEYS_PAGE)
        raise
    path = save_api_key(api_key)
    console.print(f"[green]Logged in.[/green] Key saved to {path}")
    hint("Environment variables (VOLT_API_KEY) take precedence over this file.")


@click.command("logout")
def logout_cmd() -> None:
    """Remove the stored API key from ~/.volt/config.ini."""
    if remove_api_key():
        info(f"Removed the key from {default_config_path()}. Revoke it too on {API_KEYS_PAGE} if it may have leaked.")
    else:
        info("No stored key.")


@click.command("config")
@pass_state
def config_cmd(state: AppState) -> None:
    """Show where the API key comes from (masked) and the API base URL."""
    try:
        cfg = Config.load(base_url=state.base_url)
    except ConfigError as exc:
        fail(str(exc))
    info(f"API base URL  {cfg.base_url}")
    info(f"API key       {cfg.masked_key}  from {cfg.source}")
    warning = permissions_warning(default_config_path())
    if warning:
        warn(warning)


@click.command("balance")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_state
def balance_cmd(state: AppState, as_json: bool) -> None:
    """Your balance and number of running pods."""
    try:
        bal = state.client().get_balance()
    except ApiError as exc:
        if exc.is_not_found:
            fail("This API server does not expose the balance yet. See it on https://voltagegpu.com/billing")
        raise
    if as_json:
        echo_json(bal.to_dict())
        return
    console.print(f"Balance  [bold green]{money(bal.balance)}[/bold green] {bal.currency}")
    if bal.active_pods is not None:
        info(f"Running  {bal.active_pods} pod{'s' if bal.active_pods != 1 else ''}")
    hint(f"Top up: {bal.top_up_url}")


@click.group("ssh-keys")
def ssh_keys_group() -> None:
    """Manage the SSH public keys injected into your pods."""


@ssh_keys_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_state
def ssh_keys_list(state: AppState, as_json: bool) -> None:
    """List your SSH keys."""
    keys = state.client().list_ssh_keys()
    if as_json:
        echo_json([k.to_dict() for k in keys])
        return
    if not keys:
        info("No SSH key yet. Add one: voltagegpu ssh-keys add")
        return
    table("SSH keys", [("KEY ID", {"style": "cyan", "no_wrap": True}), ("NAME",), ("KEY",), ("ADDED",)],
          [(k.id, k.name, k.summary, (k.created_at or "")[:10]) for k in keys])


@ssh_keys_group.command("add")
@click.option("--name", help="Label for the key (default: the file name).")
@click.option("--file", "key_file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Public key file (default: ~/.ssh/id_ed25519.pub, id_ecdsa.pub or id_rsa.pub).")
@click.option("--key", "key_text", help="Public key string instead of a file.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@pass_state
def ssh_keys_add(state: AppState, name: Optional[str], key_file: Optional[Path], key_text: Optional[str], as_json: bool) -> None:
    """Register an SSH public key."""
    if key_file and key_text:
        fail("Give --file or --key, not both.", code=2)
    if not key_text:
        path = key_file or default_public_key_file()
        if path is None:
            fail("No public key found in ~/.ssh. Create one with: ssh-keygen -t ed25519", code=2)
        if path.suffix != ".pub" and path.with_suffix(path.suffix + ".pub").exists():
            fail(f"{path} looks like a private key. Use {path}.pub", code=2)
        key_text = path.read_text(encoding="utf-8", errors="replace")
        name = name or path.name
    public_key = read_public_key(key_text)
    key = state.client().add_ssh_key(name or "cli-key", public_key)
    if as_json:
        echo_json(key.to_dict())
    else:
        console.print(f"[green]Added.[/green] {key.name} ({key.id})")


@ssh_keys_group.command("delete")
@click.argument("key_id")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_state
def ssh_keys_delete(state: AppState, key_id: str, yes: bool) -> None:
    """Delete an SSH key (running pods keep the keys they were created with)."""
    if not yes:
        if not is_interactive():
            fail("Re-run with --yes to confirm in a script.", code=2)
        if not click.confirm(f"Delete SSH key {key_id}?", default=False):
            raise click.exceptions.Exit(1)
    state.client().delete_ssh_key(key_id)
    console.print(f"[green]Deleted.[/green] {key_id}")
