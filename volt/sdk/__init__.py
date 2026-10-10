"""VoltageGPU Python SDK.

    from volt.sdk import VoltageGPUClient

    with VoltageGPUClient() as client:          # reads VOLT_API_KEY or ~/.volt/config.ini
        cheapest = client.list_machines("standard")[0]
        result = client.deploy_standard(cheapest.id, name="my-pod")
        pod = client.wait_until_reachable(result.pod_id)
        print(pod.ssh_command)
        client.release_pod(result.pod_id)
"""

from .client import VoltageGPUClient
from .config import DEFAULT_BASE_URL, Config
from .errors import ApiError, ConfigError, VoltageGPUError
from .models import Balance, DeployResult, Machine, Pod, ReleaseResult, SshKey, Template
from .ssh import SshTarget, UnsafeSshCommand, parse_ssh_command

__all__ = [
    "ApiError",
    "Balance",
    "Config",
    "ConfigError",
    "DEFAULT_BASE_URL",
    "DeployResult",
    "Machine",
    "Pod",
    "ReleaseResult",
    "SshKey",
    "SshTarget",
    "Template",
    "UnsafeSshCommand",
    "VoltageGPUClient",
    "VoltageGPUError",
    "parse_ssh_command",
]
