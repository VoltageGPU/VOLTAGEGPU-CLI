import pytest

from volt.sdk.ssh import SshTarget, UnsafeSshCommand, parse_ssh_command


@pytest.mark.parametrize(
    "command, expected",
    [
        ("ssh root@147.135.114.178 -p 40400", SshTarget("root", "147.135.114.178", 40400)),  # standard pod
        ("ssh -p 30022 ubuntu@203.0.113.7", SshTarget("ubuntu", "203.0.113.7", 30022)),  # Confidential VM
        ("ssh wrk-abc123@ssh.voltagegpu.com", SshTarget("wrk-abc123", "ssh.voltagegpu.com", 22)),
        ("  ssh   root@host.example   ", SshTarget("root", "host.example", 22)),
    ],
)
def test_parses_the_shapes_the_api_returns(command, expected):
    assert parse_ssh_command(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "ssh -o ProxyCommand=calc.exe root@1.2.3.4",
        "ssh -oProxyCommand=sh root@1.2.3.4",
        "ssh -o LocalCommand=id -o PermitLocalCommand=yes root@1.2.3.4",
        "ssh root@1.2.3.4; rm -rf ~",
        "ssh root@1.2.3.4 && curl evil",
        "ssh root@$(id).example",
        "ssh root@`id`",
        "ssh root@1.2.3.4 | sh",
        "ssh root@1.2.3.4 whoami",
        "ssh root@1.2.3.4 -p 0",
        "ssh root@1.2.3.4 -p 70000",
        "ssh root@1.2.3.4 -p 22 -p 23",
        "ssh -p abc root@1.2.3.4",
        "ssh -- -oProxyCommand=x@h",
        "ssh 'ro ot'@1.2.3.4",
        "ssh root@-evil.example",
        "ssh root@host@other",
        "ssh 1.2.3.4",
        "scp root@1.2.3.4",
        "",
        "ssh root@1.2.3.4\nrm -rf /",
        "ssh " + "a" * 600 + "@h",
    ],
)
def test_refuses_anything_else(command):
    with pytest.raises(UnsafeSshCommand):
        parse_ssh_command(command)


def test_argv_ends_options_before_the_destination():
    target = SshTarget("root", "1.2.3.4", 40400)
    argv = target.argv(identity_file="~/.ssh/key", remote_command=["nvidia-smi", "-L"], options=["StrictHostKeyChecking=accept-new"])
    assert argv == ["ssh", "-p", "40400", "-i", "~/.ssh/key", "-o", "StrictHostKeyChecking=accept-new",
                    "--", "root@1.2.3.4", "nvidia-smi", "-L"]


def test_display_is_shell_quoted():
    assert SshTarget("ubuntu", "203.0.113.7", 30022).display("my key") == "ssh -p 30022 -i 'my key' ubuntu@203.0.113.7"
