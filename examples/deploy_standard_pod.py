"""Deploy the cheapest standard pod, wait for SSH, run nvidia-smi, release it.

    export VOLT_API_KEY=volt_...
    python examples/deploy_standard_pod.py --max-price 1.0

This charges your balance: one hour up front, refunded down to the seconds used
when the pod is released (the script always releases it, even on error).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from volt.sdk import ApiError, VoltageGPUClient, parse_ssh_command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-price", type=float, default=1.0, help="maximum price per hour (USD)")
    parser.add_argument("--ssh-key", help="SSH key id, if your account has several")
    args = parser.parse_args()

    with VoltageGPUClient() as client:
        machines = [m for m in client.list_machines("standard") if m.price_per_hour and m.price_per_hour <= args.max_price]
        if not machines:
            print(f"No standard machine under ${args.max_price}/hour right now.")
            return 1
        machine = machines[0]
        print(f"Deploying on {machine.gpu_count}x {machine.gpu_type} at ${machine.price_per_hour}/hour")
        result = client.deploy_standard(machine.id, name="sdk-example", ssh_key_id=args.ssh_key)
        try:
            pod = client.wait_until_reachable(result.pod_id, timeout=600)
            print("SSH:", pod.ssh_command)
            if shutil.which("ssh") and pod.ssh_command:
                argv = parse_ssh_command(pod.ssh_command).argv(
                    options=["StrictHostKeyChecking=accept-new", "ConnectTimeout=15"],
                    remote_command=["nvidia-smi", "-L"],
                )
                subprocess.call(argv)  # noqa: S603 (argv rebuilt from validated user, host, port)
        except (ApiError, TimeoutError) as exc:
            print("Pod did not become reachable:", exc)
        finally:
            released = client.release_pod(result.pod_id)
            print(f"Released {result.pod_id}, refunded ${released.prepaid_refund or 0:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
