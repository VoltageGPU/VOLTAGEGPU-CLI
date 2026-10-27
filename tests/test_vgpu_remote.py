"""ssh/scp argv for vgpu exec, scp and attest: list arguments only, `--` before the destination,
a known_hosts file per machine (trust on first use), no path that could become an option."""

from __future__ import annotations

from pathlib import Path

import pytest

from volt.sdk.remote import RemotePathError, detached_command, exec_argv, known_hosts_path, scp_argv, validate_remote_path
from volt.sdk.ssh import SshTarget

TARGET = SshTarget(user="ubuntu", host="203.0.113.7", port=30022)


def test_exec_argv_is_a_list_with_double_dash(tmp_path: Path) -> None:
    argv = exec_argv(TARGET, "wrk-abc", "nvidia-smi; echo ok", home=tmp_path, ssh_bin="ssh")
    assert argv[0] == "ssh"
    assert argv[argv.index("--") + 1] == "ubuntu@203.0.113.7"
    assert argv[-1] == "nvidia-smi; echo ok"  # one argument, run by the remote shell only
    assert "BatchMode=yes" in argv and "StrictHostKeyChecking=accept-new" in argv
    assert f"UserKnownHostsFile={known_hosts_path('wrk-abc', tmp_path)}" in argv


def test_identity_file_is_passed(tmp_path: Path) -> None:
    argv = exec_argv(TARGET, "wrk-abc", "true", identity="/home/me/.ssh/id_ed25519", home=tmp_path, ssh_bin="ssh")
    assert argv[argv.index("-i") + 1] == "/home/me/.ssh/id_ed25519"
    assert argv.index("-i") < argv.index("--")


def test_known_hosts_is_per_machine_and_never_a_path_escape(tmp_path: Path) -> None:
    assert known_hosts_path("a", tmp_path) != known_hosts_path("b", tmp_path)
    assert known_hosts_path("../../etc/x", tmp_path).parent == known_hosts_path("a", tmp_path).parent


def test_detached_command_quotes_the_user_command() -> None:
    remote, log = detached_command("python train.py --lr '0.1' && echo $HOME", "job1")
    assert remote.startswith("mkdir -p ~/.vgpu && nohup sh -c ")
    assert "'python train.py --lr '\"'\"'0.1'\"'\"' && echo $HOME'" in remote
    assert remote.endswith("echo $!")
    assert log == "~/.vgpu/job1.log"


def test_scp_download_and_upload(tmp_path: Path) -> None:
    down = scp_argv(TARGET, "wrk-abc", "/tmp/proofs.tgz", "out.tgz", home=tmp_path, scp_bin="scp")
    assert down[down.index("-P") + 1] == "30022"
    assert down[-3:] == ["--", "ubuntu@203.0.113.7:/tmp/proofs.tgz", "out.tgz"]
    up = scp_argv(TARGET, "wrk-abc", "data", "~/data", upload=True, recursive=True, home=tmp_path, scp_bin="scp")
    assert "-r" in up and up[-2:] == ["data", "ubuntu@203.0.113.7:~/data"]


def test_scp_ipv6_host_is_bracketed(tmp_path: Path) -> None:
    six = SshTarget(user="root", host="2001:db8::1", port=22)
    assert scp_argv(six, "p", "/tmp/a", "a", home=tmp_path, scp_bin="scp")[-2] == "root@[2001:db8::1]:/tmp/a"


@pytest.mark.parametrize("path", ["", "-oProxyCommand=x", "/tmp/a;rm -rf /", "$(id)", "a`b`", "a b", "a\nb", "a|b", "x" * 1100])
def test_remote_path_refused(path: str) -> None:
    with pytest.raises(RemotePathError):
        validate_remote_path(path)


def test_local_path_starting_with_dash_refused(tmp_path: Path) -> None:
    with pytest.raises(RemotePathError):
        scp_argv(TARGET, "p", "/tmp/a", "-oProxyCommand=x", home=tmp_path, scp_bin="scp")


def test_windows_path_rewritten_by_git_bash_gets_a_clear_hint(monkeypatch) -> None:
    # 27/10/2026: Git Bash turned "/tmp/aller.txt" into "C:/Users/.../Temp/aller.txt" before vgpu saw it.
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    with pytest.raises(RemotePathError) as exc:
        validate_remote_path("C:/Users/User/AppData/Local/Temp/aller.txt")
    assert "Git Bash" in str(exc.value) and "MSYS_NO_PATHCONV=1" in str(exc.value) and "relative" in str(exc.value)


def test_relative_remote_path_is_accepted() -> None:
    assert validate_remote_path("out.tgz") == "out.tgz"
