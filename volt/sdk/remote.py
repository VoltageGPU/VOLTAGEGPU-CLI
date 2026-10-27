"""ssh and scp argv for ``vgpu exec``, ``vgpu scp`` and ``vgpu attest``.

Same rules as ``ssh.py``: the destination comes from a validated SshTarget,
everything is a list argument (never a local shell), ``--`` ends option
parsing, and a path that could be read as an option is refused.

Host keys: a new machine cannot be known in advance, so the first connection
accepts its key (trust on first use) and records it in a file of its own,
``~/.volt/known_hosts.d/<machine id>``. A later change of key for the same
machine is refused by ssh. One file per machine, because addresses and ports
are reused by other machines after a release.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .ssh import SshTarget

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")
# Remote paths: no whitespace, quotes or shell metacharacters. scp hands the
# remote path to the remote shell in its legacy mode.
_REMOTE_PATH_RE = re.compile(r"^[A-Za-z0-9_./~+@=,%-]{1,1024}$")
_JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RemotePathError(ValueError):
    """A path given to scp is empty, too long, or could change the command."""


def known_hosts_path(pod_id: str, home: Optional[Path] = None) -> Path:
    name = _SAFE_ID.sub("_", pod_id or "unknown").lstrip(".") or "unknown"
    return (home or Path.home()) / ".volt" / "known_hosts.d" / name[:128]


def ssh_options(pod_id: str, home: Optional[Path] = None) -> List[str]:
    path = known_hosts_path(pod_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    return [
        "BatchMode=yes",  # never a password prompt in an agent's terminal
        "StrictHostKeyChecking=accept-new",
        f"UserKnownHostsFile={path}",
        "ConnectTimeout=15",
        "ServerAliveInterval=30",
    ]


_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\/]")


def validate_remote_path(path: str) -> str:
    if path and _WINDOWS_PATH_RE.match(path):
        # Git Bash (MSYS) rewrites an absolute remote path such as /tmp/out.tgz into C:/.../Temp/out.tgz.
        hint = "Git Bash rewrote an absolute remote path into a Windows one; " if os.environ.get("MSYSTEM") else ""
        raise RemotePathError(
            f"Refusing remote path {path!r}: it is a Windows path. {hint}"
            "Use a path relative to the remote home (out.tgz), or run with MSYS_NO_PATHCONV=1."
        )
    if not path or path.startswith("-") or not _REMOTE_PATH_RE.fullmatch(path):
        raise RemotePathError(
            f"Refusing remote path {path!r}: use letters, digits and ./_~-+@=,% only (no spaces or shell characters)."
        )
    return path


def _validate_local_path(path: str) -> str:
    if not path or path.startswith("-") or any(ord(c) < 32 for c in path):
        raise RemotePathError(f"Refusing local path {path!r}: it must not be empty or start with '-'.")
    return path


def _with_option_args(base: List[str], identity: Optional[str], options: Sequence[str]) -> List[str]:
    args = list(base)
    if identity:
        args += ["-i", _validate_local_path(identity)]
    for opt in options:
        args += ["-o", opt]
    return args


def exec_argv(
    target: SshTarget,
    pod_id: str,
    command: str,
    *,
    identity: Optional[str] = None,
    home: Optional[Path] = None,
    ssh_bin: str = "ssh",
) -> List[str]:
    """``ssh ... -- user@host COMMAND``: COMMAND is one argument, run by the remote shell."""
    if not command or "\x00" in command:
        raise ValueError("The remote command is empty.")
    args = _with_option_args([ssh_bin, "-p", str(target.port)], identity, ssh_options(pod_id, home))
    return args + ["--", target.destination, command]


def detached_command(command: str, job_id: str) -> Tuple[str, str]:
    """A remote command that starts ``command`` in the background and prints its PID.

    The user command is quoted as ONE word for ``sh -c``: it is run by the
    remote shell exactly as typed, nothing of it reaches the outer command.
    """
    if not _JOB_RE.fullmatch(job_id):
        raise ValueError("invalid job id")
    log = f"~/.vgpu/{job_id}.log"
    remote = f"mkdir -p ~/.vgpu && nohup sh -c {shlex.quote(command)} > {log} 2>&1 < /dev/null & echo $!"
    return remote, log


def _remote_spec(target: SshTarget, path: str) -> str:
    host = f"[{target.host.strip('[]')}]" if ":" in target.host else target.host
    return f"{target.user}@{host}:{validate_remote_path(path)}"


def scp_argv(
    target: SshTarget,
    pod_id: str,
    source: str,
    destination: str,
    *,
    upload: bool = False,
    recursive: bool = False,
    identity: Optional[str] = None,
    home: Optional[Path] = None,
    scp_bin: str = "scp",
) -> List[str]:
    """Download ``source`` (remote) to ``destination`` (local), or the reverse with ``upload``."""
    args = _with_option_args([scp_bin, "-P", str(target.port)], identity, ssh_options(pod_id, home))
    if recursive:
        args.append("-r")
    if upload:
        return args + ["--", _validate_local_path(source), _remote_spec(target, destination)]
    return args + ["--", _remote_spec(target, source), _validate_local_path(destination)]


__all__ = ["RemotePathError", "detached_command", "exec_argv", "known_hosts_path", "scp_argv", "ssh_options", "validate_remote_path"]
