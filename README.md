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

## `vgpu`: the short command for scripts and AI agents

Installed with the package, next to `voltagegpu`. The key comes from
`VOLT_API_KEY` or `VOLTAGEGPU_API_KEY` (or `voltagegpu login`) and is never
printed. Give an agent a limited key (scopes, its own machines only, a
daily budget) from https://voltagegpu.com/api-keys, never your full key.

```bash
vgpu ls --json                                   # every offer, cheapest first
vgpu ls --kind vm --gpu H100                     # vm | cpu | standard | baremetal | sandbox
vgpu up rtx6000b-small --name train1 --ttl 2h -y --json
vgpu ps --json
vgpu exec train1 "nvidia-smi"                    # returns the remote exit code
vgpu exec train1 -d "python train.py"            # background: prints PID and log path
vgpu scp train1 /home/ubuntu/out.tgz ./out.tgz   # --up to copy the other way
vgpu attest train1                               # Confidential VM or Confidential CPU only
vgpu rm train1 -y
vgpu whoami                                      # scopes, budgets, spend, balance
vgpu mcp                                         # local MCP server (stdio) for agents
```

`--ttl` is mandatory and becomes the auto-stop of each product, rounded up,
refused locally when out of bounds: Confidential VM and CPU up to 720h,
standard pods 1h to 720h (whole hours), dedicated machines 1h to 72h (whole
hours), sandboxes up to 7d. `up` asks for confirmation unless `-y` is given,
and fails without `-y` when not run in a terminal. A deploy that gets no
answer is never retried: run `vgpu ps` first.

`exec`, `scp` and `attest` use your local SSH key (`-i`, or `VGPU_SSH_KEY`).
The first connection to a machine records its host key in
`~/.volt/known_hosts.d/<id>` (trust on first use); a changed key is refused.
Names given at `up` are kept in `~/.volt/vgpu-names.json`; ids work too.

Confidential VMs and CPU tiers bind the SSH keys already on the account.
Dedicated machines and sandboxes take key ids: by default every key of the
account, but a key limited to its own machines cannot read them, so pass
`--ssh-key-id ID` (repeatable). For such a key `whoami` and `voltagegpu
balance` show the balance as hidden: only a full key reads it.

`vgpu attest` draws a challenge on your machine, runs the published proof
script in the machine (`two-proofs-in-the-vm.sh` for a Confidential VM,
`snp-proof-in-the-vm.sh` for a Confidential CPU tier, both embedded and pinned
by SHA-256), copies the archive back and verifies it here: `voltage-verify`
for Intel TDX + NVIDIA (`pip install 'voltagegpu-cli[verify]'`, `--offline`
for no network). For SEV-SNP: the report is requested at `--vmpl` (0 by
default, sent as `SNP_VMPL`; if VMPL 0 is refused, the script says so and asks
again at 1, and the result reports it), REPORT_DATA equals the challenge, the
report policy forbids debug and its VMPL field is the level requested (the
guest kernel uses VMPCK0, consistent with VMPL 0, no SVSM), then the AMD chain and the VCEK are
fetched again from AMD into a new folder (`snpguest 0.11.0 fetch ca` / `fetch
vcek` on your machine), the ARK must be one of the AMD roots pinned in the
script, and `snpguest verify certs` / `verify attestation` run with those
certificates, never the ones in the archive (they come from the VM). Without
a local `snpguest`, or when the AMD key server cannot be reached, the result
is `INCOMPLETE`, never `VERIFIED`. Standard pods, dedicated machines and sandboxes have no tenant
attestation and are refused.

| Exit code | Meaning |
|---|---|
| 0 | ok |
| 1 | error (including a deploy that got no answer) |
| 2 | usage: bad option, `--ttl` out of bounds, missing `-y` |
| 3 | API key missing, malformed or refused |
| 4 | API key scope missing (`API_KEY_SCOPE_MISSING`), or account resource refused to a key limited to its own machines (`API_KEY_ACCOUNT_RESOURCE`) |
| 5 | not found (machine or tier) |
| 6 | balance too low or API key budget exceeded (`API_KEY_BUDGET_EXCEEDED`, HTTP 402): stop, nothing was charged |
| 7 | unavailable: sold out, over `--max-price`, rate limited, offer closed |
| 8 | attestation failed |
| 9 | attestation incomplete, or a local tool is missing (ssh, scp, voltage-verify) |

With `--json`, an error is also printed on stdout as
`{"error": {"code", "message", "exit_code", ...}}`.

### Local MCP server

`vgpu mcp` serves the tools `ls`, `up`, `ps`, `exec`, `scp`, `rm`, `attest`,
`inference` and `whoami` over stdio. It runs on your machine because `exec`,
`scp` and `attest` use your SSH private key, which never leaves it. Same
guards as the command line: `up` requires `ttl` and `max_hourly_price`, `rm`
requires `confirm: true`, a 402 comes back as `API_KEY_BUDGET_EXCEEDED` with
"do not retry", the key is never part of an answer, and the sudo password of
a Confidential VM is never handed to the agent. `inference` calls the
OpenAI-compatible API with an inference key (`vgpu_...`) in
`VOLTAGEGPU_API_KEY`; a `vgpu_` value there is never sent to the pod API,
which takes the `volt_` key from `VOLT_API_KEY`.

```bash
claude mcp add voltagegpu -e VOLT_API_KEY=volt_... -- vgpu mcp    # Claude Code
```

```toml
# Codex: ~/.codex/config.toml
[mcp_servers.voltagegpu]
command = "vgpu"
args = ["mcp"]
env = { VOLT_API_KEY = "volt_..." }
```

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
