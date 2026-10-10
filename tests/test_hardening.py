"""Regression tests for the 1.3.1 security review: hostile server text,
current-directory hijacks, money safety and secret handling."""

import os
import sys

import httpx
import pytest
from click.testing import CliRunner

from volt.cli.main import cli, main
from volt.sdk.config import Config, is_valid_api_key, save_api_key, validate_base_url
from volt.sdk.errors import ConfigError
from volt.sdk.models import Pod, strip_controls
from volt.sdk.ssh import find_ssh_binary, safe_ssh_command

from .conftest import CONFIDENTIAL_MACHINES, STANDARD_MACHINES, STANDARD_POD, TEST_KEY

ESC = "\x1b"


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input, catch_exceptions=False)


HOSTILE = {**STANDARD_MACHINES[0], "machine_id": "aaaaaaaa-0000-0000-0000-000000000001", "price_per_hour": 0.1,
           "gpu_type": "[/x][link=https://evil.example]RTX[/link] :smile:",
           "location": {"city": f"Evil{ESC}]0;pwned\x07{ESC}[8mhidden", "country_code": "XX"}}


def test_hostile_machine_text_is_shown_literally_and_cannot_drive_the_terminal(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=[HOSTILE])
    result = run("machines", "--standard")
    assert result.exit_code == 0, result.output
    assert ESC not in result.output and "\x07" not in result.output
    assert "[/x]" in result.output and ":smile:" in result.output


def test_hostile_text_in_the_deploy_summary_does_not_crash_after_charging(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=[HOSTILE])
    cli_env.on("POST", "/api/volt/pods", 201, {"success": True, "pod": {
        "id": "pod-[/x]", "provider": "standard", "hourly_price": 0.1, "prepaid_amount": 0.1}})
    result = run("deploy", "standard", "--cheapest", "--max-price", "1", "--yes")
    assert result.exit_code == 0, result.output
    assert "pod-[/x]" in result.output


def test_api_error_text_with_markup_is_printed_literally(cli_env, capsys):
    cli_env.on("GET", "/api/volt/pods", 500, {"success": False, "error": "boom [/red] [bold]x"})
    with pytest.raises(SystemExit) as exit_info:
        main(["pods", "list"])
    assert exit_info.value.code == 1
    assert "boom [/red] [bold]x" in capsys.readouterr().err


def test_sudo_password_is_printed_exactly(cli_env):
    password = "Ab[b]c[/b]:smile:9!"
    cli_env.on("GET", "/api/volt/machines", body=CONFIDENTIAL_MACHINES)
    cli_env.on("POST", "/api/volt/pods", body={"success": True, "podId": "r", "workloadUid": "wrk-9", "resourceName": "h100-small",
                                               "hourlyPrice": 6.95, "sudoPassword": password})
    result = run("deploy", "confidential", "--tier", "h100-small", "--yes")
    assert result.exit_code == 0, result.output
    assert password in result.output


def test_tampered_ssh_command_is_never_displayed(cli_env):
    evil = "ssh -o ProxyCommand=calc.exe root@1.2.3.4"
    cli_env.on("GET", "/api/volt/pods", body=[{**STANDARD_POD, "ssh_command": evil}])
    shown = run("pods", "show", STANDARD_POD["id"])
    assert "ProxyCommand" not in shown.output and "rejected" in shown.output
    listed = run("pods", "list", "--json")
    assert "ProxyCommand" not in listed.output
    waited = run("pods", "wait", STANDARD_POD["id"], "--timeout", "10")
    assert waited.exit_code == 1 and "ProxyCommand" not in waited.output


def test_pod_model_keeps_only_the_normalised_command():
    pod = Pod.from_api({**STANDARD_POD, "ssh_command": "ssh root@147.135.114.178 -p 40400"})
    assert pod.ssh_command == "ssh -p 40400 root@147.135.114.178"
    bad = Pod.from_api({**STANDARD_POD, "ssh_command": "ssh root@h; id"})
    assert bad.ssh_command is None and bad.ssh_command_rejected and not bad.reachable
    assert safe_ssh_command(None) is None


def test_control_characters_are_stripped():
    assert strip_controls(f"a{ESC}[31mb\x07c\x9bd") == "a[31mbcd"


def test_ssh_binary_is_never_taken_from_the_current_directory(tmp_path):
    here = tmp_path / "downloads"
    elsewhere = tmp_path / "bin"
    here.mkdir()
    elsewhere.mkdir()
    name = "ssh.exe" if sys.platform.startswith("win") else "ssh"
    for folder in (here, elsewhere):
        exe = folder / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    found = find_ssh_binary(env_path=os.pathsep.join([str(here), ".", "", str(elsewhere)]), cwd=str(here))
    assert found == str(elsewhere / name)
    assert find_ssh_binary(env_path=os.pathsep.join([str(here), "."]), cwd=str(here)) is None


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows script extensions")
def test_ssh_batch_scripts_are_ignored_on_windows(tmp_path):
    (tmp_path / "ssh.bat").write_text("@echo pwned")
    assert find_ssh_binary(env_path=str(tmp_path), cwd=str(tmp_path / "elsewhere")) is None


def test_cheapest_with_yes_needs_a_price_cap(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=STANDARD_MACHINES)
    result = run("deploy", "standard", "--cheapest", "--yes")
    assert result.exit_code == 2 and "--max-price" in result.output
    assert cli_env.sent("POST", "/api/volt/pods") == []


def test_lost_deploy_answer_says_maybe_charged_not_try_again(cli_env):
    def lost(request):
        raise httpx.ReadTimeout("timed out", request=request)

    cli_env.on("GET", "/api/volt/machines", body=STANDARD_MACHINES)
    cli_env.on("POST", "/api/volt/pods", handler=lost)
    result = run("deploy", "standard", "--machine", STANDARD_MACHINES[0]["machine_id"], "--yes")
    assert result.exit_code == 1
    assert "pods list" in result.output and "may have gone through" in result.output


def test_price_rise_between_list_and_deploy_is_flagged(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=STANDARD_MACHINES)
    cli_env.on("POST", "/api/volt/pods", 201, {"success": True, "pod": {
        "id": "pod-up", "provider": "standard", "hourly_price": 0.95, "prepaid_amount": 0.95}})
    result = run("deploy", "standard", "--machine", STANDARD_MACHINES[0]["machine_id"], "--yes")
    assert result.exit_code == 0
    assert "changed from $0.59 to $0.95" in result.output


def test_a_broken_config_file_never_echoes_the_key(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(f"api_key = {TEST_KEY}\n")  # no [api] header
    with pytest.raises(ConfigError) as info:
        Config.load(env={}, config_path=path)
    assert TEST_KEY not in str(info.value)
    with pytest.raises(ConfigError) as info:
        save_api_key(TEST_KEY, path)
    assert TEST_KEY not in str(info.value)


@pytest.mark.parametrize("url", ["https://evil.example/api", "https://voltagegpu.com.evil.example/api", "https://xvoltagegpu.com/api"])
def test_foreign_hosts_need_an_explicit_opt_in(url, tmp_path):
    with pytest.raises(ConfigError, match="VOLT_ALLOW_CUSTOM_BASE_URL"):
        validate_base_url(url)
    assert validate_base_url(url, allow_custom=True) == url
    cfg = Config.load(env={"VOLT_API_KEY": TEST_KEY, "VOLT_BASE_URL": url, "VOLT_ALLOW_CUSTOM_BASE_URL": "1"},
                      config_path=tmp_path / "x.ini")
    assert cfg.base_url == url


def test_our_hosts_are_accepted():
    for url in ("https://api.voltagegpu.com/api", "https://voltagegpu.com/api", "http://127.0.0.1:3000/api"):
        assert validate_base_url(url) == url


def test_key_with_trailing_newline_is_refused():
    assert not is_valid_api_key(TEST_KEY + "\n")
    assert is_valid_api_key(TEST_KEY)
