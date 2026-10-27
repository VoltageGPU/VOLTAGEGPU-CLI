"""The `vgpu` command against a simulated API: catalogue, up with --ttl, ps, exec, scp, rm,
attest, whoami, and the documented exit codes. No network, no ssh binary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import httpx
import pytest

import volt.cli.vgpu as vgpu_mod
from volt.cli.vgpu import main

from .conftest import TEST_KEY

VM_TIERS = {"tiers": [
    {"resourceName": "rtx6000b-small", "displayName": "RTX PRO 6000 Small", "kind": "gpu", "tee": "Intel TDX", "gpuType": "RTX6000B",
     "gpuCount": 1, "vcpu": 32, "memoryGb": 128, "pricePerHour": 3.8, "available": 4, "gpuAttestation": True},
    {"resourceName": "h100-small", "displayName": "H100 Small", "kind": "gpu", "tee": "Intel TDX", "gpuType": "H100",
     "gpuCount": 1, "vcpu": 12, "memoryGb": 175, "pricePerHour": 6.95, "available": 0, "gpuAttestation": True},
    {"resourceName": "cpu-small-amd", "displayName": "CPU Server - Small", "kind": "cpu", "tee": "AMD SEV-SNP", "gpuType": "",
     "gpuCount": 0, "vcpu": 4, "memoryGb": 54, "pricePerHour": 0.27, "available": 3, "gpuAttestation": False},
]}
STANDARD = [{"machine_id": "a292aae7-2767-44b0-803b-12cc3ab47012", "name": "2x NVIDIA RTX A5000", "provider": "standard",
             "confidential_compute": False, "gpu_type": "NVIDIA RTX A5000", "gpu_count": 2, "price_per_hour": 0.59,
             "available_gpu_count": 2, "cpu_cores": 16, "ram_gb": 125}]
DEDICATED = [{"resourceName": "h200-bm-8", "machine_id": "h200-bm-8", "name": "8x H200", "gpuModel": "H200", "gpuCount": 8,
              "vcpu": 192, "pricePerHour": 40.0, "available": 1, "tier": "dedicated", "enclave": "none"}]
SNAPSHOT = {"sandboxes": {"templates": [
    {"resourceName": "sandbox-small", "name": "Small", "vcpu": 1, "memoryGb": 2, "diskGb": 10, "images": ["ubuntu-24.04"],
     "pricePerHour": 0.09, "pausedPricePerHour": 0.01, "available": 5}]}}
VM_LISTED = {"id": "wrk-1", "name": "wrk-1", "status": "RUNNING", "provider_status": "running", "resource_name": "rtx6000b-small",
             "tier": "confidential", "hourly_price": 3.8, "ssh_ready": True, "ssh_command": "ssh -p 30022 ubuntu@203.0.113.7",
             "provider": "confidential", "confidential_compute": True}
CPU_LISTED = {**VM_LISTED, "id": "wrk-2", "resource_name": "cpu-small-amd", "hourly_price": 0.27,
              "ssh_command": "ssh -p 30023 ubuntu@203.0.113.8"}


def machines(request: httpx.Request) -> httpx.Response:
    provider = request.url.params.get("provider")
    return httpx.Response(200, json={"standard": STANDARD, "dedicated": DEDICATED}.get(provider, []))


@pytest.fixture
def api(cli_env, monkeypatch, tmp_path):
    cli_env.on("GET", "/api/confidential/vm/tiers", body=VM_TIERS)
    cli_env.on("GET", "/api/volt/machines", handler=machines)
    cli_env.on("GET", "/api/pricing/snapshot", body=SNAPSHOT)
    cli_env.on("GET", "/api/volt/sandboxes", body={"sandboxes": []})
    monkeypatch.setattr(vgpu_mod, "_sleep", lambda s: None)
    monkeypatch.setattr(vgpu_mod, "_tool", lambda name: name)
    return cli_env


def run(capsys, *args: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exc:
        main(list(args))
    out = capsys.readouterr()
    code = exc.value.code
    return (code if isinstance(code, int) else 0), out.out, out.err


class FakeRun:
    def __init__(self, codes: List[int] | None = None, on_call=None) -> None:
        self.calls: List[List[str]] = []
        self.inputs: List[Any] = []
        self.codes = list(codes or [])
        self.on_call = on_call

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.inputs.append(kwargs.get("input"))
        if self.on_call:
            self.on_call(list(argv))

        class R:
            returncode = self.codes.pop(0) if self.codes else 0
            stdout = b"4242\n"
            stderr = b""
        return R()


# ------------------------------------------------------------------ ls

def test_ls_json_cheapest_first_and_sold_out_hidden(api, capsys) -> None:
    code, out, _ = run(capsys, "ls", "--json")
    assert code == 0
    offers = json.loads(out)
    prices = [o["price_per_hour"] for o in offers]
    assert prices == sorted(prices)
    assert {o["kind"] for o in offers} == {"vm", "cpu", "standard", "baremetal", "sandbox"}
    assert "h100-small" not in [o["tier"] for o in offers]
    vm = next(o for o in offers if o["tier"] == "rtx6000b-small")
    assert vm["confidential"] is True and vm["tee"] == "Intel TDX"
    assert next(o for o in offers if o["kind"] == "standard")["confidential"] is False


def test_ls_filters(api, capsys) -> None:
    _, out, _ = run(capsys, "ls", "--kind", "vm", "--json", "--all")
    assert [o["tier"] for o in json.loads(out)] == ["rtx6000b-small", "h100-small"]
    _, out, _ = run(capsys, "ls", "--gpu", "a5000", "--json")
    assert [o["kind"] for o in json.loads(out)] == ["standard"]


def test_ls_human_output_never_shows_the_key(api, capsys) -> None:
    code, out, err = run(capsys, "ls")
    assert code == 0 and "rtx6000b-small" in out and TEST_KEY not in out + err


# ------------------------------------------------------------------ up

def vm_deploy(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "podId": "row-1", "workloadUid": "wrk-1", "resourceName": "rtx6000b-small",
                                     "hourlyPrice": 3.8, "sudoPassword": "s3cret-pass", "hardStopAt": "2026-10-27T15:00:00.000Z"})


def test_up_vm_translates_ttl_to_hardstop_and_waits(api, capsys) -> None:
    api.on("POST", "/api/confidential/vm/deploy", handler=vm_deploy)
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    code, out, _ = run(capsys, "up", "rtx6000b-small", "--name", "train1", "--ttl", "1h", "-y", "--json")
    assert code == 0
    body = api.json_of("POST", "/api/confidential/vm/deploy")
    assert body == {"name": "train1", "resource_name": "rtx6000b-small", "hardstop_hours": 1.0}
    data = json.loads(out)
    assert data["id"] == "wrk-1" and data["kind"] == "vm" and data["ssh"] == "ssh -p 30022 ubuntu@203.0.113.7"
    assert "s3cret-pass" not in out  # the sudo password is never printed unless asked


def test_up_standard_sandbox_baremetal_bodies(api, capsys) -> None:
    api.on("POST", "/api/volt/pods", handler=lambda r: httpx.Response(201, json={"success": True, "pod": {"id": "pod-9", "hourly_price": 0.59}}))
    api.on("POST", "/api/volt/sandboxes", body={"success": True, "podId": "sbx-row", "hourlyPrice": 0.09, "sandbox": {"id": "sbx-row"}})
    assert run(capsys, "up", "a292aae7-2767-44b0-803b-12cc3ab47012", "--name", "s1", "--ttl", "90m", "-y", "--no-wait", "--json")[0] == 0
    assert api.json_of("POST", "/api/volt/pods") == {"provider": "standard", "name": "s1",
                                                    "machine_id": "a292aae7-2767-44b0-803b-12cc3ab47012", "auto_terminate_hours": 2}
    assert run(capsys, "up", "sandbox-small", "--name", "b1", "--ttl", "2h", "-y", "--no-wait", "--json")[0] == 0
    assert api.json_of("POST", "/api/volt/sandboxes") == {"templateName": "sandbox-small", "image": "ubuntu-24.04", "name": "b1",
                                                         "deleteTimeoutSec": 7200, "idleTimeoutSec": 300, "sshKeyIds": ["key_1"]}
    assert run(capsys, "up", "h200-bm-8", "--name", "m1", "--ttl", "3h", "-y", "--no-wait", "--json")[0] == 0
    assert api.json_of("POST", "/api/volt/pods") == {"provider": "dedicated", "name": "m1", "machine_id": "h200-bm-8",
                                                    "hardstop_hours": 3, "ssh_key_ids": ["key_1"]}


STANDARD_TIER = "a292aae7-2767-44b0-803b-12cc3ab47012"
# The answer of POST /api/volt/pods (standard) when the account has several SSH keys and none was named.
SEVERAL_KEYS = {"success": False, "status_code": 400,
                "error": "ssh_key_id is required when your account has more than one SSH key. List your keys with GET /api/volt/ssh-keys."}


def test_up_standard_passes_the_ssh_key_id(api, capsys) -> None:
    api.on("POST", "/api/volt/pods", handler=lambda r: httpx.Response(201, json={"success": True, "pod": {"id": "pod-9", "hourly_price": 0.59}}))
    code, _, _ = run(capsys, "up", STANDARD_TIER, "--name", "s1", "--ttl", "1h", "-y", "--no-wait", "--json", "--ssh-key-id", "key_7")
    assert code == 0
    assert api.json_of("POST", "/api/volt/pods") == {"provider": "standard", "name": "s1", "machine_id": STANDARD_TIER,
                                                    "auto_terminate_hours": 1, "ssh_key_id": "key_7"}
    assert api.sent("GET", "/api/volt/ssh-keys") == []  # an own key cannot read them: never asked


def test_up_standard_takes_a_single_ssh_key_id(api, capsys) -> None:
    code, _, err = run(capsys, "up", STANDARD_TIER, "--name", "s1", "--ttl", "1h", "-y", "--ssh-key-id", "key_7", "--ssh-key-id", "key_8")
    assert code == 2 and "one SSH key" in err
    assert api.sent("POST", "/api/volt/pods") == []


def test_up_standard_several_ssh_keys_says_pass_ssh_key_id(api, capsys) -> None:
    api.on("POST", "/api/volt/pods", status=400, body=SEVERAL_KEYS)
    code, out, err = run(capsys, "up", STANDARD_TIER, "--name", "s1", "--ttl", "1h", "-y", "--no-wait", "--json")
    assert code == 2
    error = json.loads(out)["error"]
    assert error["code"] == "SSH_KEY_ID_REQUIRED" and error["exit_code"] == 2 and error["http_status"] == 400
    assert "several SSH keys" in error["message"] and "--ssh-key-id" in error["message"]
    assert "voltagegpu ssh-keys list" in error["message"] and "Nothing was charged" in error["message"]
    code, _, err = run(capsys, "up", STANDARD_TIER, "--name", "s1", "--ttl", "1h", "-y", "--no-wait")
    assert code == 2 and "--ssh-key-id" in err


def test_up_standard_other_400_is_left_as_is(api, capsys) -> None:
    api.on("POST", "/api/volt/pods", status=400,
           body={"success": False, "error": "ssh_key_id is not a valid key id. List your keys with GET /api/volt/ssh-keys."})
    code, out, _ = run(capsys, "up", STANDARD_TIER, "--name", "s1", "--ttl", "1h", "-y", "--no-wait", "--json", "--ssh-key-id", "bad key")
    assert code == 2 and json.loads(out)["error"]["code"] == "HTTP_400"


def test_up_requires_ttl_and_yes_when_not_interactive(api, capsys) -> None:
    code, _, err = run(capsys, "up", "rtx6000b-small", "--name", "x")
    assert code == 2 and "--ttl" in err
    code, _, err = run(capsys, "up", "rtx6000b-small", "--name", "x", "--ttl", "1h")
    assert code == 2 and "-y" in err
    assert api.sent("POST", "/api/confidential/vm/deploy") == []


def test_up_ttl_out_of_bounds_is_usage_error(api, capsys) -> None:
    code, _, err = run(capsys, "up", "h200-bm-8", "--name", "m", "--ttl", "4d", "-y")
    assert code == 2 and "72h" in err
    assert api.sent("POST", "/api/volt/pods") == []


def test_up_unknown_or_sold_out_tier(api, capsys) -> None:
    assert run(capsys, "up", "nope-xl", "--name", "x", "--ttl", "1h", "-y")[0] == 5
    assert run(capsys, "up", "h100-small", "--name", "x", "--ttl", "1h", "-y")[0] == 7


def test_up_max_price_guard(api, capsys) -> None:
    code, _, err = run(capsys, "up", "rtx6000b-small", "--name", "x", "--ttl", "1h", "-y", "--max-price", "1")
    assert code == 7 and "max-price" in err


def test_up_budget_exceeded_is_exit_6_with_code(api, capsys) -> None:
    api.on("POST", "/api/confidential/vm/deploy", status=402, body={
        "success": False, "error": "API_KEY_BUDGET_EXCEEDED", "message": "This API key reached its daily budget of $20.00",
        "window": "daily", "budget_usd": 20, "spent_usd": 19, "requested_usd": 3.8, "charged": False})
    code, out, _ = run(capsys, "up", "rtx6000b-small", "--name", "x", "--ttl", "1h", "-y", "--json")
    assert code == 6
    err = json.loads(out)["error"]
    assert err["code"] == "API_KEY_BUDGET_EXCEEDED" and err["exit_code"] == 6 and err["charged"] is False


def test_up_scope_missing_is_exit_4(api, capsys) -> None:
    api.on("POST", "/api/confidential/vm/deploy", status=403,
           body={"success": False, "error": "API_KEY_SCOPE_MISSING", "required_scope": "rent"})
    code, out, _ = run(capsys, "up", "rtx6000b-small", "--name", "x", "--ttl", "1h", "-y", "--json")
    assert code == 4 and json.loads(out)["error"]["code"] == "API_KEY_SCOPE_MISSING"


def test_up_timeout_never_retries(api, capsys) -> None:
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    api.on("POST", "/api/confidential/vm/deploy", handler=boom)
    code, _, err = run(capsys, "up", "rtx6000b-small", "--name", "x", "--ttl", "1h", "-y")
    assert code == 1 and "vgpu ps" in err
    assert len(api.sent("POST", "/api/confidential/vm/deploy")) == 1


def test_no_key_is_exit_3(api, capsys, monkeypatch) -> None:
    monkeypatch.delenv("VOLT_API_KEY")
    assert run(capsys, "ps")[0] == 3


def test_invalid_key_is_exit_3(api, capsys) -> None:
    api.on("GET", "/api/volt/pods", status=401, body={"success": False, "error": "Invalid API key"})
    assert run(capsys, "ps")[0] == 3


# ------------------------------------------------------------------ ps / exec / scp / rm

def test_ps_shows_local_names(api, capsys) -> None:
    api.on("POST", "/api/confidential/vm/deploy", handler=vm_deploy)
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    run(capsys, "up", "rtx6000b-small", "--name", "train1", "--ttl", "1h", "-y", "--json")
    code, out, _ = run(capsys, "ps", "--json")
    rows = json.loads(out)
    assert code == 0 and rows[0]["name"] == "train1" and rows[0]["id"] == "wrk-1" and rows[0]["ssh_ready"] is True


def test_exec_runs_ssh_with_a_list_and_returns_the_remote_code(api, capsys, monkeypatch) -> None:
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    fake = FakeRun(codes=[3])
    monkeypatch.setattr(vgpu_mod, "_run", fake)
    code, _, _ = run(capsys, "exec", "wrk-1", "nvidia-smi && echo $HOME")
    assert code == 3
    argv = fake.calls[0]
    assert argv[0] == "ssh" and argv[-2:] == ["ubuntu@203.0.113.7", "nvidia-smi && echo $HOME"]


def test_exec_detached_prints_pid_and_log(api, capsys, monkeypatch) -> None:
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    monkeypatch.setattr(vgpu_mod, "_run", FakeRun())
    code, out, _ = run(capsys, "exec", "wrk-1", "-d", "python train.py", "--json")
    data = json.loads(out)
    assert code == 0 and data["pid"] == 4242 and data["log"].startswith("~/.vgpu/")


def test_exec_unknown_machine_is_exit_5(api, capsys, monkeypatch) -> None:
    monkeypatch.setattr(vgpu_mod, "_run", FakeRun())
    assert run(capsys, "exec", "ghost", "true")[0] == 5


def test_exec_not_reachable_yet_is_exit_7(api, capsys, monkeypatch) -> None:
    api.on("GET", "/api/volt/pods", body=[{**VM_LISTED, "ssh_ready": False, "ssh_command": None, "provider_status": "provisioning"}])
    monkeypatch.setattr(vgpu_mod, "_run", FakeRun())
    assert run(capsys, "exec", "wrk-1", "true")[0] == 7


def test_scp_downloads(api, capsys, monkeypatch) -> None:
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    fake = FakeRun()
    monkeypatch.setattr(vgpu_mod, "_run", fake)
    assert run(capsys, "scp", "wrk-1", "/tmp/out.txt", "out.txt")[0] == 0
    assert fake.calls[0][-2:] == ["ubuntu@203.0.113.7:/tmp/out.txt", "out.txt"]
    assert run(capsys, "scp", "wrk-1", "/tmp/a;id", "x")[0] == 2


def test_rm_requires_yes_then_releases(api, capsys) -> None:
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    api.on("DELETE", "/api/volt/pods/wrk-1", body={"success": True})
    assert run(capsys, "rm", "wrk-1")[0] == 2
    assert api.sent("DELETE", "/api/volt/pods/wrk-1") == []
    code, out, _ = run(capsys, "rm", "wrk-1", "-y", "--json")
    assert code == 0 and json.loads(out)["released"] is True


def test_rm_sandbox_uses_its_endpoint(api, capsys) -> None:
    api.on("GET", "/api/volt/pods", body=[])
    api.on("GET", "/api/volt/sandboxes", body={"sandboxes": [{"id": "sbx-row", "name": "b1", "status": "RUNNING", "pricePerHour": 0.09,
                                                              "sshCommand": "ssh -p 2201 root@198.51.100.4"}]})
    api.on("DELETE", "/api/volt/sandboxes/sbx-row", body={"success": True})
    assert run(capsys, "rm", "b1", "-y")[0] == 0
    assert len(api.sent("DELETE", "/api/volt/sandboxes/sbx-row")) == 1


# ------------------------------------------------------------------ attest

def test_attest_refuses_a_machine_without_attestation(api, capsys) -> None:
    api.on("GET", "/api/volt/pods", body=[{"id": "pod-9", "status": "RUNNING", "provider": "standard", "confidential_compute": False,
                                          "ssh_command": "ssh root@198.51.100.9 -p 40400", "hourly_price": 0.59}])
    code, _, err = run(capsys, "attest", "pod-9")
    assert code == 2 and "no tenant attestation" in err


def test_attest_gpu_end_to_end_with_a_real_bundle(api, capsys, monkeypatch, tmp_path) -> None:
    import tarfile
    fixtures = Path(__file__).parent / "fixtures"
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])
    good = "627ba9c51d4719038e917c7431675b183ac576c819d712178dc8a100f4409a45"
    monkeypatch.setattr(vgpu_mod.attest, "new_challenge", lambda kind: good)

    def on_call(argv: List[str]) -> None:
        if argv[0] == "scp":
            import hashlib
            files = {n: (fixtures / n).read_bytes() for n in ("manifest.json", "bundle.json")}
            sums = "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in files.items()).encode()
            src = tmp_path / "src"
            src.mkdir(exist_ok=True)
            for n, d in {**files, "SHA256SUMS": sums}.items():
                (src / n).write_bytes(d)
            with tarfile.open(argv[-1], "w:gz") as tar:
                for n in ("manifest.json", "bundle.json", "SHA256SUMS"):
                    tar.add(src / n, arcname=n)

    fake = FakeRun(on_call=on_call)
    monkeypatch.setattr(vgpu_mod, "_run", fake)
    code, out, _ = run(capsys, "attest", "wrk-1", "--offline", "--out", str(tmp_path / "proof"), "--json")
    data = json.loads(out)
    assert code == 0 and data["verified"] is True and data["challenge"] == good and data["tee"] == "Intel TDX"
    assert fake.calls[0][-1] == f"CHALLENGE={good} bash -s"
    assert fake.inputs[0].startswith(b"#!/usr/bin/env bash")  # the embedded script goes on stdin
    assert fake.calls[1][-2] == "ubuntu@203.0.113.7:/tmp/proofs.tgz"


def test_attest_wrong_challenge_is_exit_8(api, capsys, monkeypatch, tmp_path) -> None:
    import tarfile
    fixtures = Path(__file__).parent / "fixtures"
    api.on("GET", "/api/volt/pods", body=[VM_LISTED])

    def on_call(argv: List[str]) -> None:
        if argv[0] == "scp":
            import hashlib
            files = {n: (fixtures / n).read_bytes() for n in ("manifest.json", "bundle.json")}
            sums = "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in files.items()).encode()
            src = tmp_path / "src"
            src.mkdir(exist_ok=True)
            for n, d in {**files, "SHA256SUMS": sums}.items():
                (src / n).write_bytes(d)
            with tarfile.open(argv[-1], "w:gz") as tar:
                for n in ("manifest.json", "bundle.json", "SHA256SUMS"):
                    tar.add(src / n, arcname=n)

    monkeypatch.setattr(vgpu_mod, "_run", FakeRun(on_call=on_call))
    code, out, _ = run(capsys, "attest", "wrk-1", "--offline", "--out", str(tmp_path / "proof"), "--json")
    assert code == 8 and json.loads(out)["verified"] is False


def test_attest_script_failure_in_the_vm_is_exit_8(api, capsys, monkeypatch, tmp_path) -> None:
    api.on("GET", "/api/volt/pods", body=[CPU_LISTED])
    monkeypatch.setattr(vgpu_mod, "_run", FakeRun(codes=[3]))
    code, out, _ = run(capsys, "attest", "wrk-2", "--out", str(tmp_path / "p"), "--json")
    assert code == 8 and json.loads(out)["status"] == "NOT_VERIFIED"


# ------------------------------------------------------------------ whoami

def test_whoami_masks_the_key(api, capsys) -> None:
    api.on("GET", "/api/volt/whoami", body={"success": True, "key": {"name": "agent", "scopes": ["read", "rent"], "pod_visibility": "own",
                                                                    "budgets_usd": {"daily": 20}, "spent_usd": {"daily": 1.5}},
                                           "balance": 42.0, "currency": "USD"})
    code, out, err = run(capsys, "whoami", "--json")
    data = json.loads(out)
    assert code == 0 and data["key"]["masked"] == "volt_****" + TEST_KEY[-4:] and data["key"]["scopes"] == ["read", "rent"]
    assert TEST_KEY not in out + err


def test_exit_codes_are_documented() -> None:
    assert vgpu_mod.EXIT_CODES == {0: "ok", 1: "error", 2: "usage", 3: "api key missing, malformed or refused", 4: "api key scope missing",
                                   5: "not found", 6: "balance or api key budget exceeded (402)", 7: "unavailable, sold out or over --max-price",
                                   8: "attestation failed", 9: "attestation incomplete or local tool missing"}


def test_attest_cpu_in_vm_exit_code_is_named(api, capsys, monkeypatch, tmp_path) -> None:
    api.on("GET", "/api/volt/pods", body=[CPU_LISTED])
    for rc, name in ((4, "report.debug"), (5, "report.vmpl"), (6, "amd.ark"), (7, "report.request")):
        monkeypatch.setattr(vgpu_mod, "_run", FakeRun(codes=[rc]))
        code, out, _ = run(capsys, "attest", "wrk-2", "--out", str(tmp_path / f"p{rc}"), "--json")
        assert code == 8 and json.loads(out)["failed"] == [name]


def test_attest_cpu_names_the_vmpl_level(api, capsys, monkeypatch, tmp_path) -> None:
    # 2026-10-27: SNP_VMPL is always sent (0 by default, or --vmpl), never left to snpguest's default (1).
    api.on("GET", "/api/volt/pods", body=[CPU_LISTED])
    ch = "cd" * 64
    monkeypatch.setattr(vgpu_mod.attest, "new_challenge", lambda kind: ch)
    for args, level in (((), 0), (("--vmpl", "1"), 1)):
        fake = FakeRun(codes=[3])
        monkeypatch.setattr(vgpu_mod, "_run", fake)
        code, out, _ = run(capsys, "attest", "wrk-2", *args, "--out", str(tmp_path / f"v{level}"), "--json")
        assert code == 8 and fake.calls[0][-1] == f"CHALLENGE={ch} SNP_VMPL={level} bash -s"
        assert f"SNP_VMPL={level}" in json.loads(out)["detail"]
    assert run(capsys, "attest", "wrk-2", "--vmpl", "4", "--out", str(tmp_path / "v4"))[0] == 2


def test_attest_human_output_reports_the_vmpl_fallback(api, capsys, monkeypatch) -> None:
    data = {"name": "c1", "id": "wrk-2", "verified": True, "status": "VERIFIED", "tee": "AMD SEV-SNP", "challenge": "ab" * 64,
            "failed": [], "facts": {"vmpl": 1, "vmpl_requested": 0, "vmpl_fallback": True}}
    seen: dict = {}

    def fake_attest(client, ref, **kwargs):
        seen.update(kwargs)
        return data, 0

    monkeypatch.setattr(vgpu_mod, "attest_machine", fake_attest)
    code, out, _ = run(capsys, "attest", "wrk-2")
    assert code == 0 and "VMPL 0 refused" in out and "not at VMPL 0" in out
    assert seen["vmpl"] == 0


# ------------------------------------------------------------------ own keys (API since fbadd6ac)

OWN_REFUSAL = {"success": False, "error": "API_KEY_ACCOUNT_RESOURCE", "message": "This key only sees its own machines."}


def test_own_key_cannot_list_ssh_keys_sandbox_needs_ssh_key_id(api, capsys) -> None:
    api.on("GET", "/api/volt/ssh-keys", status=403, body=OWN_REFUSAL)
    api.on("POST", "/api/volt/sandboxes", body={"success": True, "podId": "sbx-row", "hourlyPrice": 0.09})
    code, out, _ = run(capsys, "up", "sandbox-small", "--name", "b1", "--ttl", "2h", "-y", "--no-wait", "--json")
    assert code == 4 and json.loads(out)["error"]["code"] == "API_KEY_ACCOUNT_RESOURCE"
    assert api.sent("POST", "/api/volt/sandboxes") == []
    code, _, _ = run(capsys, "up", "sandbox-small", "--name", "b1", "--ttl", "2h", "-y", "--no-wait", "--json", "--ssh-key-id", "key_9")
    assert code == 0 and api.json_of("POST", "/api/volt/sandboxes")["sshKeyIds"] == ["key_9"]


def test_vm_deploy_never_reads_ssh_keys(api, capsys) -> None:
    api.on("GET", "/api/volt/ssh-keys", status=403, body=OWN_REFUSAL)
    api.on("POST", "/api/confidential/vm/deploy", handler=vm_deploy)
    assert run(capsys, "up", "rtx6000b-small", "--name", "v1", "--ttl", "1h", "-y", "--no-wait")[0] == 0
    assert api.sent("GET", "/api/volt/ssh-keys") == []


def test_whoami_with_hidden_balance(api, capsys) -> None:
    api.on("GET", "/api/volt/whoami", body={"success": True, "key": {"name": "agent", "scopes": ["read"]}, "balance": None,
                                           "balance_hidden": "This agent key cannot read the account balance (billing scope, full keys only)."})
    code, out, _ = run(capsys, "whoami", "--json")
    data = json.loads(out)
    assert code == 0 and data["balance"] is None and data["balance_hidden"].startswith("This agent key")
    code, out, _ = run(capsys, "whoami")
    assert code == 0 and "hidden" in out


# ------------------------------------------------------------------ 2.0.1 (end-to-end agent test of 27/10/2026)


def test_whoami_rounds_spend_to_cents_for_humans(api, capsys) -> None:
    api.on("GET", "/api/volt/whoami", body={"success": True, "key": {"name": "agent", "scopes": ["read"], "budgets_usd": {"daily": 20},
                                                                    "spent_usd": {"daily": 0.18999999999999995, "monthly": None}},
                                           "balance": None, "balance_hidden": "hidden"})
    code, out, _ = run(capsys, "whoami")
    assert code == 0 and '"daily": 0.19' in out and "0.18999" not in out
    code, out, _ = run(capsys, "whoami", "--json")
    assert json.loads(out)["key"]["spent_usd"]["daily"] == 0.18999999999999995


def test_attest_human_output_names_the_gpu_when_its_checks_passed(api, capsys, monkeypatch) -> None:
    data = {"name": "job1", "id": "wrk-1", "verified": True, "status": "VERIFIED", "tee": "Intel TDX", "challenge": "ab" * 32,
            "failed": [], "checks": [{"name": "tdx.signature", "ok": True}, {"name": "nvidia.policy", "ok": True}]}
    monkeypatch.setattr(vgpu_mod, "attest_machine", lambda client, ref, **kwargs: (data, 0))
    code, out, _ = run(capsys, "attest", "job1")
    assert code == 0 and out.startswith("VERIFIED: Intel TDX + NVIDIA GPU, challenge")


def test_attest_human_output_keeps_the_tee_alone_without_gpu_checks(api, capsys, monkeypatch) -> None:
    data = {"name": "c1", "id": "wrk-2", "verified": True, "status": "VERIFIED", "tee": "AMD SEV-SNP", "challenge": "ab" * 64,
            "failed": [], "checks": [{"name": "snp.report", "ok": True}]}
    monkeypatch.setattr(vgpu_mod, "attest_machine", lambda client, ref, **kwargs: (data, 0))
    code, out, _ = run(capsys, "attest", "c1")
    assert code == 0 and out.startswith("VERIFIED: AMD SEV-SNP, challenge") and "NVIDIA" not in out
