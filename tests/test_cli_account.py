import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import volt.cli.cmd_account as account_mod
from volt.cli.main import cli, main

from .conftest import STANDARD_POD, TEST_KEY


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input, catch_exceptions=False)


def config_file(tmp_home: Path) -> Path:
    return tmp_home / ".volt" / "config.ini"


def test_login_checks_the_key_then_saves_it(cli_env, tmp_path, monkeypatch):
    monkeypatch.delenv("VOLT_API_KEY")
    result = run("login", "--api-key", TEST_KEY)
    assert result.exit_code == 0, result.output
    assert cli_env.sent("GET", "/api/volt/ssh-keys")[0].headers["X-API-Key"] == TEST_KEY
    assert TEST_KEY in config_file(tmp_path).read_text()
    assert TEST_KEY not in result.output


def test_login_prompts_hidden_when_interactive(cli_env, tmp_path, monkeypatch):
    monkeypatch.delenv("VOLT_API_KEY")
    monkeypatch.setattr(account_mod, "is_interactive", lambda: True)
    result = run("login", input=TEST_KEY + "\n")
    assert result.exit_code == 0 and config_file(tmp_path).exists()
    assert TEST_KEY not in result.output  # hidden input is not echoed


def test_login_refusals(cli_env, tmp_path, monkeypatch):
    monkeypatch.delenv("VOLT_API_KEY")
    assert run("login").exit_code == 2  # no terminal, no key
    assert run("login", "--api-key", "not-a-key").exit_code == 1
    cli_env.on("GET", "/api/volt/ssh-keys", 401, {"success": False, "error": "Invalid API key"})
    refused = run("login", "--api-key", TEST_KEY)
    assert refused.exit_code == 1 and "refused" in refused.output
    assert not config_file(tmp_path).exists()


def test_logout(cli_env, tmp_path, monkeypatch):
    monkeypatch.delenv("VOLT_API_KEY")
    run("login", "--api-key", TEST_KEY)
    assert "Removed" in run("logout").output
    assert "No stored key" in run("logout").output


def test_config_without_key(monkeypatch, tmp_path):
    for name in ("VOLT_API_KEY", "VOLTAGEGPU_API_KEY", "VOLT_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = run("config")
    assert result.exit_code == 1 and "api-keys" in result.output


def test_ssh_keys_delete(cli_env):
    cli_env.on("DELETE", "/api/volt/ssh-keys/key_1", body={"success": True})
    assert run("ssh-keys", "delete", "key_1").exit_code == 2
    assert run("ssh-keys", "delete", "key_1", "--yes").exit_code == 0
    assert len(cli_env.sent("DELETE", "/api/volt/ssh-keys/key_1")) == 1


def test_ssh_keys_add_from_string_and_conflicts(cli_env, tmp_path):
    cli_env.on("POST", "/api/volt/ssh-keys", 201, {"id": "k3", "name": "ci", "publicKey": "ssh-ed25519 AAAA ci"})
    ok = run("ssh-keys", "add", "--name", "ci", "--key", "ssh-ed25519 AAAAC3Nza ci@runner", "--json")
    assert ok.exit_code == 0 and json.loads(ok.output)["id"] == "k3"
    pub = tmp_path / "k.pub"
    pub.write_text("ssh-ed25519 AAAA x")
    assert run("ssh-keys", "add", "--file", str(pub), "--key", "ssh-ed25519 AAAA").exit_code == 2
    assert run("ssh-keys", "add", "--key", "hello world").exit_code == 2


def test_ssh_keys_add_without_any_key_file(cli_env, monkeypatch, tmp_path):
    monkeypatch.setattr(account_mod, "default_public_key_file", lambda: None)
    result = run("ssh-keys", "add")
    assert result.exit_code == 2 and "ssh-keygen" in result.output


def test_pods_wait(cli_env):
    cli_env.on("GET", "/api/volt/pods", body=[STANDARD_POD])
    result = run("pods", "wait", STANDARD_POD["id"], "--timeout", "10")
    assert result.exit_code == 0 and "ssh -p 40400 root@147.135.114.178" in result.output


def test_pods_list_empty(cli_env):
    assert "No running pod" in run("pods", "list").output


def test_invalid_id_is_a_usage_error(cli_env, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["pods", "stop", "../../admin", "--yes"])
    assert exit_info.value.code == 2
    assert "Invalid id" in capsys.readouterr().err


def test_piped_output_survives_characters_outside_the_console_codepage():
    import io

    from volt.cli.main import configure_stream

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    configure_stream(stream)  # not a TTY: switches to UTF-8
    stream.write("Ploiești, RO\n")
    stream.flush()
    assert raw.getvalue().decode("utf-8").rstrip("\r\n") == "Ploiești, RO"


def test_piped_cli_run_in_a_cp1252_process(tmp_path):
    """End to end: a real subprocess whose stdout codec is cp1252."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    code = ("import sys; from volt.cli.main import configure_stream; configure_stream(sys.stdout); "
            "print('Ploie\\u0219ti')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env)
    assert out.returncode == 0
    assert out.stdout.decode("utf-8").strip() == "Ploiești"


def test_network_error_message(cli_env, capsys):
    import httpx

    def boom(request):
        raise httpx.ConnectError("dns", request=request)

    cli_env.on("GET", "/api/volt/pods", handler=boom)
    with pytest.raises(SystemExit) as exit_info:
        main(["pods", "list"])
    assert exit_info.value.code == 1
    assert "Could not reach" in capsys.readouterr().err


def test_balance_hidden_for_an_agent_key(cli_env):
    """Since 27/10/2026 a limited key without billing gets balance: null and balance_hidden."""
    hidden = "This agent key cannot read the account balance (billing scope, full keys only)."
    cli_env.on("GET", "/api/volt/balance", body={"success": True, "balance": None, "currency": "USD",
                                                 "active_pods": 1, "balance_hidden": hidden})
    from click.testing import CliRunner

    from volt.cli.main import cli

    result = CliRunner().invoke(cli, ["balance"], catch_exceptions=False)
    assert result.exit_code == 0 and "$0.00" not in result.output and "cannot read the account balance" in result.output
    data = json.loads(CliRunner().invoke(cli, ["balance", "--json"], catch_exceptions=False).output)
    assert data["balance"] is None and data["balance_hidden"].startswith("This agent key")
