"""Entry point of the `voltagegpu` (and legacy `volt`) command."""

from __future__ import annotations

import logging
import os
import sys
from typing import List, Optional

import click
from rich.errors import MarkupError

from .. import __version__
from ..sdk.config import API_KEYS_PAGE
from ..sdk.errors import ApiError, ConfigError
from .cmd_account import balance_cmd, config_cmd, login_cmd, logout_cmd, ssh_keys_group
from .cmd_deploy import deploy_group
from .cmd_machines import machines_cmd, templates_cmd
from .cmd_misc import account_group, cc_group, verify_cmd
from .cmd_pods import pods_group, ssh_cmd
from .output import err_console, esc
from .state import AppState

EPILOG = """\b
Quick start:
  voltagegpu login                                 # paste a key from https://voltagegpu.com/api-keys
  voltagegpu machines --standard
  voltagegpu deploy standard --cheapest --wait
  voltagegpu ssh POD_ID
  voltagegpu pods stop POD_ID
\b
Docs: https://docs.voltagegpu.com/cli   API: https://docs.voltagegpu.com/api-reference
"""


@click.group(epilog=EPILOG, context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 110})
@click.version_option(__version__, "-V", "--version", prog_name="voltagegpu")
@click.option("--base-url", envvar="VOLT_BASE_URL", hidden=True, help="API base URL (https only).")
@click.pass_context
def cli(ctx: click.Context, base_url: Optional[str]) -> None:
    """VoltageGPU: Confidential VMs (Intel TDX) and standard GPU pods from your terminal."""
    state = ctx.ensure_object(AppState)
    state.base_url = base_url
    ctx.call_on_close(state.close)


for command in (machines_cmd, templates_cmd, deploy_group, pods_group, ssh_cmd, ssh_keys_group,
                balance_cmd, login_cmd, logout_cmd, config_cmd, verify_cmd, cc_group, account_group):
    cli.add_command(command)


def _report(exc: BaseException) -> int:
    if isinstance(exc, ConfigError):
        err_console.print(f"[red]Error:[/red] {esc(exc)}")
        return 2
    if isinstance(exc, ApiError):
        if exc.is_auth_error:
            err_console.print(
                f"[red]Error:[/red] the API refused your key (HTTP {exc.status_code}: {esc(exc.message)}).\n"
                f"Create or check a key at {API_KEYS_PAGE}, then run `voltagegpu login`."
            )
        elif exc.status_code:
            err_console.print(f"[red]Error (HTTP {exc.status_code}):[/red] {esc(exc.message)}")
        else:
            err_console.print(f"[red]Error:[/red] {esc(exc.message)}")
        return 1
    if isinstance(exc, ValueError):
        err_console.print(f"[red]Error:[/red] {esc(exc)}")
        return 2
    return 1


def configure_stream(stream: object) -> None:
    """Never crash on a character the console cannot encode.

    Piped output (``| jq``, ``> file``) is written as UTF-8. On a terminal the
    encoding is kept and unencodable characters are replaced: a Windows
    console in cp1252 otherwise raises on a city name such as "Ploiesti" with
    its cedilla.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        interactive = stream.isatty()  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        interactive = False
    try:
        if interactive:
            reconfigure(errors="replace")
        else:
            reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass


def main(argv: Optional[List[str]] = None) -> None:
    """Console-script entry point. Tracebacks only with VOLT_DEBUG=1."""
    configure_stream(sys.stdout)
    configure_stream(sys.stderr)
    debug = os.environ.get("VOLT_DEBUG") == "1"
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING)
    if not debug:
        logging.getLogger("httpx").setLevel(logging.WARNING)  # never log request headers
    try:
        # standalone_mode=False: click returns the exit code of `Exit` instead of exiting.
        code = cli.main(args=argv, prog_name="voltagegpu", standalone_mode=False)
        sys.exit(code if isinstance(code, int) else 0)
    except click.exceptions.Exit as exc:
        sys.exit(exc.exit_code)
    except click.exceptions.Abort:
        err_console.print("Aborted.")
        sys.exit(1)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except (ConfigError, ApiError, ValueError) as exc:
        if debug:
            raise
        sys.exit(_report(exc))
    except MarkupError as exc:  # last resort: never a traceback because of odd text
        if debug:
            raise
        print(f"Error: could not display a message ({type(exc).__name__}).", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        err_console.print("Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
