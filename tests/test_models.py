from volt.sdk.errors import ApiError, scrub
from volt.sdk.models import Balance, DeployResult, Machine, Pod, SshKey, clean_gpu_name

from .conftest import CONFIDENTIAL_MACHINES, SSH_KEYS, STANDARD_MACHINES, STANDARD_POD, VM_POD


def test_standard_machine():
    m = Machine.from_api(STANDARD_MACHINES[0])
    assert (m.id, m.kind, m.gpu_type, m.gpu_count, m.price_per_hour) == (
        "a292aae7-2767-44b0-803b-12cc3ab47012", "standard", "RTX A5000", 2, 0.59)
    assert m.location == "Oakton, US" and not m.confidential
    assert Machine.from_api(STANDARD_MACHINES[2]).location is None


def test_confidential_tier_units_are_converted():
    m = Machine.from_api(CONFIDENTIAL_MACHINES[0])
    assert (m.id, m.kind, m.gpu_type, m.vcpu, m.ram_gb, m.available) == ("h100-small", "confidential", "H100", 12, 175, 8)
    assert m.name == "NVIDIA H100 - Small" and m.confidential


def test_gpu_names():
    assert clean_gpu_name("NVIDIA-H100") == "H100"
    assert clean_gpu_name("NVIDIA GeForce RTX 3090") == "GeForce RTX 3090"
    assert clean_gpu_name(None) == "GPU"


def test_pods():
    std = Pod.from_api(STANDARD_POD)
    assert std.kind == "standard" and std.reachable and std.gpu_type == "RTX A5000"
    vm = Pod.from_api(VM_POD)
    assert vm.confidential and vm.reachable and vm.resource_name == "rtx6000b-small"
    booting = Pod.from_api({**VM_POD, "ssh_ready": False})
    assert not booting.reachable


def test_deploy_result_shapes():
    std = DeployResult.from_api({"success": True, "pod": {"id": "p", "provider": "standard", "hourly_price": 0.7, "prepaid_amount": 0.7}})
    assert std.kind == "standard" and std.prepaid_amount == 0.7
    vm = DeployResult.from_api({"success": True, "podId": "row", "workloadUid": "wrk", "hourlyPrice": 3.8, "sudoPassword": None})
    assert vm.kind == "confidential" and vm.pod_id == "wrk" and vm.sudo_password is None


def test_ssh_key_summary_and_balance():
    key = SshKey.from_api(SSH_KEYS[0])
    assert key.summary.startswith("ssh-ed25519 ...") and key.id == "key_1"
    assert Balance.from_api({"balance": "12.5", "activePods": 1}).active_pods == 1


def test_scrub_removes_internal_supplier_names():
    text = scrub("Error from Lium: targon rejected the decentralized request (celium)")
    assert "lium" not in text.lower() and "targon" not in text.lower() and "decentral" not in text.lower()
    assert scrub("Helium cooling") == "Helium cooling"


def test_api_error_messages_are_scrubbed():
    err = ApiError.from_response(502, {"error": "Targon said no"})
    assert "Targon" not in str(err)
