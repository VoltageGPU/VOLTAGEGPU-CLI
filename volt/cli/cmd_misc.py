"""`voltagegpu verify` (delegates to voltage-verify) and retired commands."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from typing import List, Sequence, Tuple

import click

from .output import fail, info
from .state import AppState, pass_state

VERIFY_SUBCOMMANDS = ("manifest", "attest", "verify", "quote", "inspect", "selftest")


def verify_argv(args: Sequence[str]) -> List[str]:
    """`voltagegpu verify bundle.json --offline` -> `voltage-verify verify bundle.json --offline`.

    Any voltage-verify subcommand (manifest, attest, quote, ...) passes through
    unchanged; with no argument, voltage-verify prints its own help.
    """
    rest = list(args)
    if rest and rest[0] not in VERIFY_SUBCOMMANDS and not rest[0].startswith("-"):
        rest = ["verify", *rest]
    return [sys.executable, "-m", "voltage_verify", *rest]


@click.command("verify", context_settings={"ignore_unknown_options": True, "allow_extra_args": True, "help_option_names": []})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def verify_cmd(args: Tuple[str, ...]) -> None:
    """Verify a Confidential VM attestation bundle offline (runs voltage-verify).

    \b
    Examples:
      voltagegpu verify bundle.json --offline
      voltagegpu verify bundle.json --challenge <hex> --gpus 1
      voltagegpu verify quote quote.bin
      voltagegpu verify --help        # voltage-verify's own help
    \b
    Needs: pip install voltage-verify   (or: pip install "voltagegpu-cli[verify]")
    """
    if importlib.util.find_spec("voltage_verify") is None:
        fail('voltage-verify is not installed in this environment. Run: pip install voltage-verify', code=2)
    # A list, never a shell: arguments reach voltage-verify exactly as typed.
    raise click.exceptions.Exit(subprocess.call(verify_argv(args)))  # noqa: S603


_RETIRED = (
    "Confidential containers were retired on 2026-10-01. Use a Confidential VM instead:\n"
    "  voltagegpu machines --confidential\n"
    "  voltagegpu deploy confidential --tier rtx6000b-small"
)


@click.group("cc", hidden=True)
def cc_group() -> None:
    """Retired: Confidential containers. Use `voltagegpu deploy confidential`."""


@cc_group.command("inventory", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def cc_inventory() -> None:
    fail(_RETIRED, code=2)


@cc_group.command("deploy", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def cc_deploy() -> None:
    fail(_RETIRED, code=2)


@click.group("account", hidden=True)
def account_group() -> None:
    """Earlier releases: use `voltagegpu balance`."""


@account_group.command("balance")
@click.pass_context
def account_balance(ctx: click.Context) -> None:
    from .cmd_account import balance_cmd

    ctx.invoke(balance_cmd, as_json=False)


@account_group.command("info")
@pass_state
def account_info(state: AppState) -> None:
    info("Account details are on https://voltagegpu.com/settings. Balance: voltagegpu balance")
