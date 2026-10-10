"""`voltagegpu verify` (delegates to voltage-verify) and retired commands."""

from __future__ import annotations

import importlib
import importlib.util
from typing import List, Sequence, Tuple

import click

from .output import fail, info
from .state import AppState, pass_state

VERIFY_SUBCOMMANDS = ("manifest", "attest", "verify", "quote", "inspect", "selftest")


def verify_argv(args: Sequence[str]) -> List[str]:
    """`voltagegpu verify bundle.json --offline` -> voltage-verify's `verify bundle.json --offline`.

    Any voltage-verify subcommand (manifest, attest, quote, ...) passes through
    unchanged; with no argument, voltage-verify prints its own help.
    """
    rest = list(args)
    if rest and rest[0] not in VERIFY_SUBCOMMANDS and not rest[0].startswith("-"):
        rest = ["verify", *rest]
    return rest


def run_voltage_verify(argv: List[str]) -> int:
    """Call voltage-verify's entry point in this process.

    Not `python -m voltage_verify`: that puts the current directory first on
    sys.path, so a `voltage_verify.py` planted next to a downloaded bundle
    would run instead of the real verifier.
    """
    module = importlib.import_module("voltage_verify.cli")
    try:
        return int(module.main(argv) or 0)
    except SystemExit as exc:  # argparse --help / usage errors
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)


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
    # Arguments reach voltage-verify exactly as typed; no shell, no subprocess.
    raise click.exceptions.Exit(run_voltage_verify(verify_argv(args)))


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
