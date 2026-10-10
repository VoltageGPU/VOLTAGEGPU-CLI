import httpx
import pytest

from volt import __version__
from volt.sdk.client import VoltageGPUClient
from volt.sdk.config import Config
from volt.sdk.errors import ApiError

from .conftest import CONFIDENTIAL_MACHINES, STANDARD_MACHINES, STANDARD_POD, STANDARD_TEMPLATES, TEST_KEY, VM_POD


def test_every_request_carries_the_key_and_user_agent(api, client):
    api.on("GET", "/api/volt/machines", body=STANDARD_MACHINES)
    client.list_machines("standard")
    req = api.sent("GET", "/api/volt/machines")[0]
    assert req.headers["X-API-Key"] == TEST_KEY
    assert req.headers["User-Agent"] == f"voltagegpu-cli/{__version__}"
    assert req.url.params["provider"] == "standard"
    assert req.url.scheme == "https"


def test_machines_confidential_first_then_cheapest(api, client):
    api.on("GET", "/api/volt/machines", body=list(reversed(STANDARD_MACHINES)) + CONFIDENTIAL_MACHINES)
    machines = client.list_machines()
    assert [m.id for m in machines][:2] == ["rtx6000b-small", "h100-small"]
    assert [m.price_per_hour for m in machines if not m.confidential] == [0.59, 0.7, 0.78]


def test_unknown_machine_kind_is_a_programming_error(client):
    with pytest.raises(ValueError):
        client.list_machines("cheap")


def test_templates_use_the_standard_catalogue(api, client):
    api.on("GET", "/api/volt/templates", body=STANDARD_TEMPLATES)
    templates = client.list_templates()
    assert api.sent("GET", "/api/volt/templates")[0].url.params["provider"] == "standard"
    assert templates[0].default and templates[0].id.startswith("bb50")


def test_deploy_standard_sends_only_what_was_given(api, client):
    api.on("POST", "/api/volt/pods", 201, {"success": True, "pod": {
        "id": "pod-1", "name": "my-pod", "machine_id": "m-1", "template_id": "t", "gpu_type": "RTX A5000", "gpu_count": 2,
        "hourly_price": 0.59, "prepaid_amount": 0.59, "status": "RUNNING", "provider": "standard",
        "confidential_compute": False, "ssh_command": None}, "message": "Deployment started."})
    result = client.deploy_standard("m-1", "my-pod", ssh_key_id="key_1")
    assert api.json_of("POST", "/api/volt/pods") == {"provider": "standard", "name": "my-pod", "machine_id": "m-1", "ssh_key_id": "key_1"}
    assert (result.pod_id, result.kind, result.hourly_price, result.prepaid_amount) == ("pod-1", "standard", 0.59, 0.59)


def test_deploy_confidential_returns_the_one_time_password(api, client):
    api.on("POST", "/api/volt/pods", 200, {
        "success": True, "via": "/api/confidential/vm/deploy", "podId": "row_1", "workloadUid": "wrk-1",
        "resourceName": "rtx6000b-small", "hourlyPrice": 3.8, "sudoPassword": "S3cret!pass", "hardStopAt": None,
        "ports": [8000], "attestation": {"verified": True, "verifyInsideTheVm": ["voltage-verify attest ..."]},
        "message": "Booting."})
    result = client.deploy_confidential("rtx6000b-small", "vm-1", hardstop_hours=2, ports=[{"port": 8000, "protocol": "tcp"}])
    assert api.json_of("POST", "/api/volt/pods") == {
        "provider": "confidential", "name": "vm-1", "resource_name": "rtx6000b-small", "hardstop_hours": 2,
        "ports": [{"port": 8000, "protocol": "tcp"}]}
    assert result.pod_id == "wrk-1" and result.record_id == "row_1"
    assert result.sudo_password == "S3cret!pass"
    assert "S3cret" not in repr(result)
    assert "sudo_password" not in result.to_dict(include_secret=False)


def test_api_errors_surface_the_server_message(api, client):
    api.on("POST", "/api/volt/pods", 402, {"success": False, "error": "Insufficient balance. One hour costs $0.59.", "charged": False})
    with pytest.raises(ApiError) as info:
        client.deploy_standard("m-1", "x")
    assert info.value.status_code == 402
    assert "Insufficient balance" in info.value.message
    assert info.value.payload["charged"] is False


def test_error_and_message_are_merged_once(api, client):
    api.on("GET", "/api/volt/pods", 401, {"success": False, "error": "Unauthorized", "message": "Missing X-API-Key header"})
    with pytest.raises(ApiError) as info:
        client.list_pods()
    assert info.value.message == "Unauthorized: Missing X-API-Key header"
    assert info.value.is_auth_error


def test_redirects_are_never_followed(api, client):
    api.on("GET", "/api/volt/pods", handler=lambda r: httpx.Response(302, headers={"Location": "https://evil.example/steal"}))
    with pytest.raises(ApiError, match="redirect"):
        client.list_pods()
    assert all(r.url.host != "evil.example" for r in api.requests)


def test_network_errors_become_api_errors(api, client):
    def boom(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    api.on("GET", "/api/volt/pods", handler=boom)
    with pytest.raises(ApiError) as info:
        client.list_pods()
    assert info.value.status_code == 0


def test_html_error_pages_are_not_dumped(api, client):
    api.on("GET", "/api/volt/pods", handler=lambda r: httpx.Response(502, text="<html><body>Bad gateway</body></html>"))
    with pytest.raises(ApiError) as info:
        client.list_pods()
    assert info.value.message == "HTTP 502"


def test_release_reports_the_refund(api, client):
    api.on("DELETE", "/api/volt/pods/pod-1", body={"success": True, "prepaidRefund": 0.5851})
    assert client.release_pod("pod-1").prepaid_refund == 0.5851
    api.on("DELETE", "/api/volt/pods/wrk-1", body={"success": True})
    assert client.release_pod("wrk-1").prepaid_refund is None


@pytest.mark.parametrize("bad", ["../admin", "a/b", "", "x" * 200, "..", "id?x=1"])
def test_ids_cannot_change_the_url_path(client, bad):
    with pytest.raises(ValueError):
        client.release_pod(bad)


def test_get_pod_prefers_the_list_then_falls_back(api, client):
    api.on("GET", "/api/volt/pods", body=[VM_POD])
    assert client.get_pod("wrk-abc123").ssh_command == "ssh -p 30022 ubuntu@203.0.113.7"
    api.on("GET", "/api/volt/pods/old-1", body={"state": {"status": "stopped"}})
    pod = client.get_pod("old-1")
    assert pod.status == "STOPPED" and pod.confidential
    api.on("GET", "/api/volt/pods/std-1", body={**STANDARD_POD, "id": "std-1"})
    assert client.get_pod("std-1").kind == "standard"


def test_wait_until_reachable_polls_then_returns(api, client):
    answers = iter([[{**STANDARD_POD, "ssh_command": None, "status": "PENDING"}], [STANDARD_POD]])
    api.on("GET", "/api/volt/pods", handler=lambda r: httpx.Response(200, json=next(answers)))
    slept = []
    pod = client.wait_until_reachable(STANDARD_POD["id"], sleep=slept.append, interval=1.0)
    assert pod.reachable and slept == [1.0]


def test_wait_until_reachable_times_out(api, client):
    api.on("GET", "/api/volt/pods", body=[{**STANDARD_POD, "ssh_command": None}])
    ticks = iter([0.0, 5.0, 11.0])
    with pytest.raises(TimeoutError):
        client.wait_until_reachable(STANDARD_POD["id"], timeout=10, sleep=lambda s: None, clock=lambda: next(ticks))


def test_ssh_key_add_uses_the_documented_field(api, client):
    api.on("POST", "/api/volt/ssh-keys", 201, {"id": "k2", "name": "laptop", "publicKey": "ssh-ed25519 AAAA x"})
    key = client.add_ssh_key("laptop", "ssh-ed25519 AAAA x")
    assert api.json_of("POST", "/api/volt/ssh-keys") == {"name": "laptop", "public_key": "ssh-ed25519 AAAA x"}
    assert key.id == "k2"


def test_balance(api, client):
    api.on("GET", "/api/volt/balance", body={"success": True, "balance": 11.0314, "currency": "USD", "active_pods": 0})
    bal = client.get_balance()
    assert (bal.balance, bal.active_pods) == (11.0314, 0)


def test_tls_is_verified_and_redirects_off(monkeypatch):
    seen = {}
    real = httpx.Client

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy)
    VoltageGPUClient(Config(api_key=TEST_KEY)).close()
    assert seen.get("verify", True) is True
    assert seen["follow_redirects"] is False
    assert seen["base_url"].startswith("https://")
