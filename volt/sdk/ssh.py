"""Turn the ``ssh_command`` returned by the API into a safe ``ssh`` argv.

The command string comes from the network. Handing it to a shell, or passing
its tokens straight to ``ssh``, would let a tampered answer run code on the
user's machine (``-o ProxyCommand=...``, ``-o LocalCommand=...``, ``$(...)``).
So it is parsed into exactly three values, user, host and port, each checked
against a strict pattern, and the argv is rebuilt from those values only.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import List, Optional, Sequence

_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_IPV6_RE = re.compile(r"^\[?[0-9A-Fa-f:]{2,39}\]?$")


class UnsafeSshCommand(ValueError):
    """The server-provided SSH command is not of the expected form."""


@dataclass(frozen=True)
class SshTarget:
    user: str
    host: str
    port: int = 22

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}"

    def argv(
        self,
        identity_file: Optional[str] = None,
        remote_command: Sequence[str] = (),
        options: Sequence[str] = (),
    ) -> List[str]:
        """``ssh -p PORT [-i KEY] [-o OPT]... -- user@host [command...]``.

        ``options`` and ``remote_command`` come from the user's own command
        line, never from the API. ``--`` ends option parsing, so neither the
        destination nor the remote command can ever be read as an ssh option.
        """
        args = ["ssh", "-p", str(self.port)]
        if identity_file:
            args += ["-i", identity_file]
        for opt in options:
            args += ["-o", opt]
        args += ["--", self.destination]
        args += list(remote_command)
        return args

    def display(self, identity_file: Optional[str] = None) -> str:
        parts = ["ssh", "-p", str(self.port)]
        if identity_file:
            parts += ["-i", identity_file]
        parts.append(self.destination)
        return " ".join(shlex.quote(p) for p in parts)


def _valid_host(host: str) -> bool:
    return bool(_HOST_RE.fullmatch(host) or _IPV6_RE.fullmatch(host))


def parse_ssh_command(command: str) -> SshTarget:
    """Parse ``ssh [-p PORT] user@host [-p PORT]`` and nothing else."""
    if not command or len(command) > 512:
        raise UnsafeSshCommand("empty or oversized SSH command")
    if any(ch in command for ch in "\n\r\x00;|&`$<>"):
        raise UnsafeSshCommand("SSH command contains shell metacharacters")
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise UnsafeSshCommand(f"unparseable SSH command ({exc})") from None
    if not tokens or tokens[0] != "ssh":
        raise UnsafeSshCommand("SSH command does not start with ssh")

    port: Optional[int] = None
    destination: Optional[str] = None
    rest = tokens[1:]
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "-p" and i + 1 < len(rest):  # noqa: S105 (an ssh flag, not a password)
            if port is not None:
                raise UnsafeSshCommand("port given twice")
            value = rest[i + 1]
            if not value.isdigit() or not 1 <= int(value) <= 65535:
                raise UnsafeSshCommand("invalid SSH port")
            port = int(value)
            i += 2
            continue
        if token.startswith("-"):
            raise UnsafeSshCommand(f"unexpected ssh option {token!r}")
        if destination is not None:
            raise UnsafeSshCommand("unexpected extra argument in SSH command")
        destination = token
        i += 1

    if destination is None or destination.count("@") != 1:
        raise UnsafeSshCommand("SSH command has no user@host destination")
    user, host = destination.split("@")
    if not _USER_RE.fullmatch(user) or not _valid_host(host):
        raise UnsafeSshCommand("SSH destination has an unexpected user or host")
    return SshTarget(user=user, host=host, port=port or 22)


def safe_ssh_command(command: Optional[str]) -> Optional[str]:
    """The normalised, validated form of a server SSH command, or None.

    Only this form is ever shown to the user: a tampered answer such as
    ``ssh -o ProxyCommand=... root@h`` must not end up on screen where it would
    be copied and pasted into a shell.
    """
    if not command:
        return None
    try:
        return parse_ssh_command(command).display()
    except UnsafeSshCommand:
        return None


def find_ssh_binary(env_path: Optional[str] = None, cwd: Optional[str] = None) -> Optional[str]:
    """Locate ``ssh`` on PATH, never in the current directory.

    ``shutil.which`` on Windows looks in the current directory first, so a
    ``ssh.exe`` or ``ssh.bat`` planted in a downloaded folder would run. Only
    absolute PATH entries other than the current directory are searched.
    """
    import os
    import sys

    here = os.path.normcase(os.path.realpath(cwd or os.getcwd()))
    raw = os.environ.get("PATH", "") if env_path is None else env_path
    if sys.platform.startswith("win"):
        exts = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        names = [f"ssh{e}" for e in exts if e in (".exe", ".com")]  # never a .bat/.cmd script
    else:
        names = ["ssh"]
    for entry in raw.split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        if os.path.normcase(os.path.realpath(entry)) == here:
            continue
        for name in names:
            candidate = os.path.join(entry, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


__all__ = ["SshTarget", "UnsafeSshCommand", "find_ssh_binary", "parse_ssh_command", "safe_ssh_command"]
