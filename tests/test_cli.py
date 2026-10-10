import json
import subprocess

import pytest
from click.testing import CliRunner

import volt.cli.cmd_deploy as deploy_mod
import volt.cli.cmd_pods as pods_mod
from volt.cli.cmd_misc import verify_argv
from volt.cli.main import cli, main

from .conftest import CONFIDENTIAL_MACHINES, STANDARD_MACHINES, STANDARD_POD, STANDARD_TEMPLATES, TEST_KEY, VM_POD


def run(*args, input=None):
    result = CliRunner().invoke(cli, list(args), input=input, catch_exceptions=False)
    return result


def standard_deploy_answer(request):
    import httpx

    body = json.loads(request.content)
    return httpx.Response(201, json={"success": True, "pod": {
        "id": "pod-new", "name": body["name"], "machine_id": body["machine_id"], "hourly_price": 0.59, "prepaid_amount": 0.59,
        "status": "RUNNING", "provider": "standard", "confidential_compute": False, "ssh_command": None}})


def test_help_lists_the_commands_and_no_internal_names():
    result = run("--help")
    assert result.exit_code == 0
    for command in ("machines", "deploy", "pods", "ssh", "ssh-keys", "balance", "verify", "login"):
        assert command in result.output
    assert " cc " not in result.output  # retired group is hidden


def test_machines_table_and_json(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=CONFIDENTIAL_MACHINES + STANDARD_MACHINES)
    result = run("machines")
    assert result.exit_code == 0
    assert "Confidential VMs" in result.output and "Standard machines" in result.output and "rtx6000b-small" in result.output
    data = json.loads(run("machines", "--standard", "--json").output)
    assert all(m["kind"] == "standard" for m in data) or cli_env.sent("GET", "/api/volt/machines")[-1].url.params["provider"] == "standard"


def test_machines_filters(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=STANDARD_MACHINES)
    data = json.loads(run("machines", "--standard", "--gpu", "a6000", "--json").output)
    assert [m["gpu_type"] for m in data] == ["RTX A6000"]
    data = json.loads(run("machines", "--standard", "--max-price", "0.65", "--json").output)
    assert [m["price_per_hour"] for m in data] == [0.59]


def test_templates(cli_env):
    cli_env.on("GET", "/api/volt/templates", body=STANDARD_TEMPLATES)
    assert "bb50bd64-e363-4adc-8111-1d44c8e36807" in run("templates").output


def test_deploy_standard_refuses_to_charge_without_confirmation_in_a_script(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=STANDARD_MACHINES)
    cli_env.on("POST", "/api/volt/pods", handler=standard_deploy_answer)
    result = run("deploy", "standard", "--cheapest")
    assert result.exit_code == 2
    assert "--yes" in result.output
    assert cli_env.sent("POST", "/api/volt/pods") == []


def test_deploy_standard_cheapest_with_yes(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=list(reversed(STANDARD_MACHINES)))
    cli_env.on("POST", "/api/volt/pods", handler=standard_deploy_answer)
    result = run("deploy", "standard", "--cheapest", "--max-price", "1", "--name", "e2e-1", "--ssh-key", "key_1", "--yes", "--json")
    assert result.exit_code == 0, result.output
    body = cli_env.json_of("POST", "/api/volt/pods")
    assert body == {"provider": "standard", "name": "e2e-1", "machine_id": STANDARD_MACHINES[0]["machine_id"], "ssh_key_id": "key_1"}
    assert json.loads(result.output)["deploy"]["pod_id"] == "pod-new"


def test_deploy_standard_interactive_confirmation(cli_env, monkeypatch):
    monkeypatch.setattr(deploy_mod, "is_interactive", lambda: True)
    cli_env.on("GET", "/api/volt/machines", body=STANDARD_MACHINES)
    cli_env.on("POST", "/api/volt/pods", handler=standard_deploy_answer)
    declined = run("deploy", "standard", "--machine", STANDARD_MACHINES[1]["machine_id"], input="n\n")
    assert declined.exit_code == 1 and cli_env.sent("POST", "/api/volt/pods") == []
    accepted = run("deploy", "standard", "--machine", STANDARD_MACHINES[1]["machine_id"], input="y\n")
    assert accepted.exit_code == 0 and "Deployed." in accepted.output
    assert "voltagegpu pods stop pod-new" in accepted.output


def test_deploy_standard_unknown_machine_and_bad_name(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=STANDARD_MACHINES)
    result = run("deploy", "standard", "--machine", "nope", "--yes")
    assert result.exit_code == 1 and "not in the deployable list" in result.output
    assert run("deploy", "standard", "--cheapest", "--max-price", "1", "--name", "Bad Name;rm", "--yes").exit_code == 2
    assert run("deploy", "standard", "--yes").exit_code == 2  # neither --machine nor --cheapest


def test_deploy_confidential_shows_the_password_once(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=CONFIDENTIAL_MACHINES)
    cli_env.on("POST", "/api/volt/pods", body={
        "success": True, "podId": "row_1", "workloadUid": "wrk-1", "resourceName": "rtx6000b-small", "hourlyPrice": 3.8,
        "sudoPassword": "S3cret!pass", "attestation": {"verifyInsideTheVm": ["voltage-verify attest --manifest m.json"]}})
    result = run("deploy", "confidential", "--tier", "rtx6000b-small", "--port", "8000", "--port", "53/udp", "--yes")
    assert result.exit_code == 0, result.output
    assert result.output.count("S3cret!pass") == 1
    assert "voltage-verify attest" in result.output
    assert cli_env.json_of("POST", "/api/volt/pods")["ports"] == [{"port": 8000, "protocol": "tcp"}, {"port": 53, "protocol": "udp"}]


def test_deploy_confidential_guards(cli_env):
    cli_env.on("GET", "/api/volt/machines", body=CONFIDENTIAL_MACHINES)
    assert run("deploy", "confidential", "--tier", "b300-mega", "--yes").exit_code == 1
    assert run("deploy", "confidential", "--tier", "h100-small", "--port", "22", "--yes").exit_code == 2
    cli_env.on("GET", "/api/volt/ssh-keys", body=[])
    no_key = run("deploy", "confidential", "--tier", "h100-small", "--yes")
    assert no_key.exit_code == 1 and "ssh-keys add" in no_key.output
    assert cli_env.sent("POST", "/api/volt/pods") == []


def test_pods_list_show_and_aliases(cli_env):
    cli_env.on("GET", "/api/volt/pods", body=[STANDARD_POD, VM_POD])
    listing = run("pods", "list")
    assert STANDARD_POD["id"] in listing.output and "confidential VM" in listing.output
    shown = run("pods", "get", VM_POD["id"])
    assert "ssh -p 30022 ubuntu@203.0.113.7" in shown.output
    assert json.loads(run("pods", "show", STANDARD_POD["id"], "--json").output)["kind"] == "standard"


def test_pods_stop_requires_yes_in_scripts_and_reports_refund(cli_env):
    cli_env.on("DELETE", f"/api/volt/pods/{STANDARD_POD['id']}", body={"success": True, "prepaidRefund": 0.5851})
    refused = run("pods", "stop", STANDARD_POD["id"])
    assert refused.exit_code == 2 and cli_env.sent("DELETE", f"/api/volt/pods/{STANDARD_POD['id']}") == []
    done = run("pods", "delete", STANDARD_POD["id"], "--yes")
    assert done.exit_code == 0 and "$0.5851" in done.output


def test_ssh_print_rebuilds_the_command(cli_env):
    cli_env.on("GET", "/api/volt/pods", body=[STANDARD_POD])
    result = run("ssh", STANDARD_POD["id"], "--print")
    assert result.output.strip() == "ssh -p 40400 root@147.135.114.178"


def test_ssh_runs_ssh_without_a_shell(cli_env, monkeypatch):
    cli_env.on("GET", "/api/volt/pods", body=[STANDARD_POD])
    calls = []
    monkeypatch.setattr(pods_mod.ssh_lookup, "find_ssh_binary", lambda: "/usr/bin/ssh")
    monkeypatch.setattr(pods_mod.subprocess, "call", lambda argv, **kw: calls.append((argv, kw)) or 0)
    result = run("ssh", STANDARD_POD["id"], "-o", "StrictHostKeyChecking=accept-new", "--", "nvidia-smi", "-L")
    assert result.exit_code == 0
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/ssh", "-p", "40400", "-o", "StrictHostKeyChecking=accept-new", "--", "root@147.135.114.178", "nvidia-smi", "-L"]
    assert "shell" not in kwargs


def test_ssh_refuses_a_tampered_command(cli_env, monkeypatch):
    cli_env.on("GET", "/api/volt/pods", body=[{**STANDARD_POD, "ssh_command": "ssh -o ProxyCommand=calc.exe root@1.2.3.4"}])
    monkeypatch.setattr(pods_mod.subprocess, "call", lambda *a, **k: pytest.fail("ssh must not run"))
    result = run("ssh", STANDARD_POD["id"])
    assert result.exit_code == 1 and "Refusing" in result.output


def test_ssh_not_ready(cli_env):
    cli_env.on("GET", "/api/volt/pods", body=[{**STANDARD_POD, "ssh_command": None, "status": "PENDING"}])
    result = run("ssh", STANDARD_POD["id"])
    assert result.exit_code == 1 and "pods wait" in result.output


def test_ssh_keys_add_refuses_private_keys(cli_env, tmp_path):
    private = tmp_path / "id_test"
    private.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n")
    result = run("ssh-keys", "add", "--file", str(private))
    assert result.exit_code == 2 and "PRIVATE" in result.output
    assert cli_env.sent("POST", "/api/volt/ssh-keys") == []


def test_ssh_keys_add_and_list(cli_env, tmp_path):
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample me@laptop\n")
    cli_env.on("POST", "/api/volt/ssh-keys", 201, {"id": "k9", "name": "id_ed25519.pub", "publicKey": "ssh-ed25519 AAAA"})
    assert run("ssh-keys", "add", "--file", str(pub)).exit_code == 0
    assert cli_env.json_of("POST", "/api/volt/ssh-keys")["public_key"].startswith("ssh-ed25519 ")
    assert "laptop" in run("ssh-keys", "list").output


def test_balance_ok_and_not_deployed_yet(cli_env):
    cli_env.on("GET", "/api/volt/balance", body={"success": True, "balance": 11.0314, "currency": "USD", "active_pods": 0})
    assert "$11.03" in run("balance").output
    cli_env.on("GET", "/api/volt/balance", 404, {"success": False, "error": "Not found"})
    missing = run("balance")
    assert missing.exit_code == 1 and "voltagegpu.com/billing" in missing.output


def test_retired_container_commands_point_to_vms(cli_env):
    result = run("cc", "deploy", "--name", "x", "--resource", "h200-small")
    assert result.exit_code == 2 and "deploy confidential" in result.output


def test_config_masks_the_key(cli_env):
    result = run("config")
    assert TEST_KEY not in result.output and TEST_KEY[-4:] in result.output


def test_verify_argument_mapping():
    assert verify_argv(["bundle.json", "--offline"]) == ["verify", "bundle.json", "--offline"]
    assert verify_argv(["quote", "q.bin"]) == ["quote", "q.bin"]
    assert verify_argv(["--help"]) == ["--help"]
    assert verify_argv([]) == []


def test_verify_runs_voltage_verify_in_process(monkeypatch):
    import types

    import volt.cli.cmd_misc as misc

    seen = []
    fake = types.SimpleNamespace(main=lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(misc.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(misc.importlib, "import_module", lambda name: fake if name == "voltage_verify.cli" else None)
    assert run("verify", "bundle.json", "--offline").exit_code == 0
    assert seen == [["verify", "bundle.json", "--offline"]]
    monkeypatch.setattr(misc.importlib.util, "find_spec", lambda name: None)
    missing = run("verify", "bundle.json")
    assert missing.exit_code == 2 and "pip install voltage-verify" in missing.output


def test_verify_propagates_argparse_exits(monkeypatch):
    import types

    import volt.cli.cmd_misc as misc

    def help_then_exit(argv):
        raise SystemExit(0)

    monkeypatch.setattr(misc.importlib, "import_module", lambda name: types.SimpleNamespace(main=help_then_exit))
    assert misc.run_voltage_verify(["--help"]) == 0
    monkeypatch.setattr(misc.importlib, "import_module", lambda name: types.SimpleNamespace(main=lambda a: (_ for _ in ()).throw(SystemExit(2))))
    assert misc.run_voltage_verify(["bogus"]) == 2


def test_main_turns_api_errors_into_clean_exit_codes(cli_env, capsys):
    cli_env.on("GET", "/api/volt/pods", 401, {"success": False, "error": "Invalid API key"})
    with pytest.raises(SystemExit) as exit_info:
        main(["pods", "list"])
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "api-keys" in err and "Traceback" not in err


def test_main_without_a_key(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("VOLT_API_KEY", raising=False)
    monkeypatch.delenv("VOLTAGEGPU_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with pytest.raises(SystemExit) as exit_info:
        main(["pods", "list"])
    assert exit_info.value.code == 2
    assert "voltagegpu login" in capsys.readouterr().err


def test_version_and_module_entry():
    out = subprocess.run([__import__("sys").executable, "-m", "volt", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and "1.3.1" in out.stdout
