# voltagegpu-cli

Deploy and manage VoltageGPU machines from your terminal or from Python:

- **Confidential VMs**: Intel TDX trust domains with an NVIDIA GPU, only your SSH
  keys on the machine, and an attestation you generate and verify yourself.
- **Standard pods**: whole GPU machines without an enclave, for workloads that
  are not sensitive.

Billing is per second. The first hour is charged when you deploy; when you stop
a standard pod, the unused part of that hour goes back to your balance.

```bash
pip install voltagegpu-cli
```

Python 3.10 or newer. The package installs the `voltagegpu` command (`volt` is
an alias kept from earlier releases).

## 1. Log in

Create an API key at <https://voltagegpu.com/api-keys> (it is shown once), then:

```bash
voltagegpu login                 # prompts for the key, hidden, checks it, saves it to ~/.volt/config.ini (mode 0600)
# or, for CI and scripts:
export VOLT_API_KEY=volt_...
```

Add the SSH key you will connect with (defaults to `~/.ssh/id_ed25519.pub`):

```bash
voltagegpu ssh-keys add
voltagegpu ssh-keys list
```

## 2. Pick a machine

```bash
voltagegpu machines                          # Confidential VM tiers, then standard machines, cheapest first
voltagegpu machines --standard --gpu A6000   # filter by GPU model
voltagegpu machines --standard --max-price 1 --json
voltagegpu machines --confidential
voltagegpu templates                         # images for standard pods (PyTorch by default)
```

## 3a. Deploy a standard pod

```bash
voltagegpu deploy standard --machine <machine_id>
voltagegpu deploy standard --cheapest --gpu 4090 --wait     # cheapest matching machine, wait for SSH
```

The CLI shows the price and asks before charging. In scripts, add `--yes`.
If your account has several SSH keys, choose one with `--ssh-key <key_id>`.
Other options: `--template <template_id>`, `--jupyter`, `--ports 3`,
`--auto-terminate 8` (hours), `--name my-pod`.

## 3b. Deploy a Confidential VM

```bash
voltagegpu deploy confidential --tier rtx6000b-small --wait
voltagegpu deploy confidential --tier h100-small --hardstop 4 --port 8000 --port 8443/tcp
```

Every SSH key on your account is bound to the VM when it is created; a VM
cannot be given a key afterwards, so add yours first. The sudo password is
printed once and is not kept anywhere. The command also prints how to generate
the attestation inside the VM.

## 4. Connect, inspect, stop

```bash
voltagegpu pods list
voltagegpu pods wait <pod_id>                 # until SSH answers
voltagegpu ssh <pod_id>                       # interactive shell
voltagegpu ssh <pod_id> -- nvidia-smi -L      # run one command
voltagegpu ssh <pod_id> --print               # just print the ssh command
voltagegpu pods show <pod_id>
voltagegpu pods stop <pod_id>                 # releases it: billing stops, the disk is deleted
voltagegpu balance
```

Fresh pods present a new host key. In non-interactive scripts you can accept it
on first use with `-o StrictHostKeyChecking=accept-new`.

## 5. Verify a Confidential VM

Inside the VM you produce an evidence bundle (the deploy output prints the exact
commands). On your own machine:

```bash
pip install "voltagegpu-cli[verify]"          # installs voltage-verify
voltagegpu verify bundle.json --offline
voltagegpu verify bundle.json --challenge <hex-you-issued> --gpus 1
```

`voltagegpu verify` runs [voltage-verify](https://pypi.org/project/voltage-verify/),
which checks the Intel TDX quote up to Intel's root and the NVIDIA GPU
attestation tokens, without trusting VoltageGPU.

## Scripting

Every list command takes `--json`; deploys and stops take `--yes`.

```bash
# Deploy the cheapest standard machine, wait for SSH, run a job, release it.
POD=$(voltagegpu deploy standard --cheapest --max-price 1 --yes --json | jq -r .deploy.pod_id)
voltagegpu pods wait "$POD"
voltagegpu ssh "$POD" -o StrictHostKeyChecking=accept-new -- nvidia-smi
voltagegpu pods stop "$POD" --yes
```

Exit codes: `0` success, `1` API or runtime error, `2` usage error or missing
confirmation.

## Python SDK

```python
from volt.sdk import VoltageGPUClient

with VoltageGPUClient() as client:            # VOLT_API_KEY or ~/.volt/config.ini
    machine = client.list_machines("standard")[0]          # cheapest first
    result = client.deploy_standard(machine.id, name="my-pod")
    pod = client.wait_until_reachable(result.pod_id)
    print(pod.ssh_command)
    print(client.release_pod(result.pod_id).prepaid_refund)
```

A complete example is in [`examples/deploy_standard_pod.py`](examples/deploy_standard_pod.py).

## REST API

The CLI is a thin client of the public API, documented at
<https://docs.voltagegpu.com/api-reference>. The same deploy with curl:

```bash
curl -X POST https://api.voltagegpu.com/api/volt/pods \
  -H "X-API-Key: $VOLT_API_KEY" -H "Content-Type: application/json" \
  -d '{"provider":"standard","name":"my-pod","machine_id":"<machine_id>"}'

curl https://api.voltagegpu.com/api/volt/pods -H "X-API-Key: $VOLT_API_KEY"
```

## Configuration

| Setting | Where |
| --- | --- |
| API key | `--api-key` of `login`, `VOLT_API_KEY` (or `VOLTAGEGPU_API_KEY`), then `~/.volt/config.ini` |
| API base URL | `VOLT_BASE_URL` (https only, voltagegpu.com hosts unless `VOLT_ALLOW_CUSTOM_BASE_URL=1`; default `https://api.voltagegpu.com/api`) |
| Debug | `VOLT_DEBUG=1` shows tracebacks |

`voltagegpu config` shows where the key comes from, masked.

## Security

- The key is sent only over HTTPS, in the `X-API-Key` header; redirects are
  never followed and certificates are always verified.
- The key is never printed. `~/.volt/config.ini` is created readable by you
  only, and `voltagegpu config` warns if its permissions are too open.
- The SSH command returned by the API is parsed into user, host and port, only
  that rebuilt form is ever shown, and ssh is started without a shell (and never
  from the current directory), so a tampered answer cannot run a command on
  your machine. Server text is printed literally, without terminal escapes.
- `ssh-keys add` refuses private keys.

Report a vulnerability to contact@voltagegpu.com.

## Upgrading from 1.2

- `volt cc inventory` / `volt cc deploy`: Confidential containers were retired
  on 2026-10-01. Use `voltagegpu machines --confidential` and
  `voltagegpu deploy confidential --tier <tier>`.
- `volt pods stop` now releases the pod and reports the refund.
- `volt account balance` is now `voltagegpu balance`.

## Links

- Docs: <https://docs.voltagegpu.com/cli>
- API reference: <https://docs.voltagegpu.com/api-reference>
- API keys: <https://voltagegpu.com/api-keys>
- Changelog: [CHANGELOG.md](CHANGELOG.md)

MIT License.
