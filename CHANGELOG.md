# Changelog

All notable changes to `voltagegpu-cli` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [2.0.1] - 2026-10-27

Found by an end-to-end test of the agent offer in production.

### Fixed
- `vgpu attest` names the NVIDIA GPU in its verdict when the GPU checks passed:
  `VERIFIED: Intel TDX + NVIDIA GPU, challenge ...` (the JSON `tee` field is unchanged).
- `vgpu whoami` shows budgets and spend to the cent (it printed `0.18999999999999995`); `--json`
  keeps the exact values.
- `vgpu scp` explains a Windows remote path: Git Bash rewrites an absolute remote path such as
  `/tmp/out.tgz` into `C:/.../Temp/out.tgz`. Use a path relative to the remote home, or
  `MSYS_NO_PATHCONV=1`.

## [2.0.0] - 2026-10-27

### Added
- `vgpu`, a second command for scripts and AI agents: `ls`, `up`, `ps`,
  `exec` (with `-d`), `scp`, `rm`, `attest`, `whoami`. One catalogue for
  Confidential VMs (Intel TDX), Confidential CPU (AMD SEV-SNP), standard GPU
  pods, dedicated machines and sandboxes, cheapest first.
- `--ttl` on every `vgpu up`, translated to the auto-stop of each product and
  checked against its bounds before anything is sent.
- `vgpu attest`: challenge drawn locally, embedded proof scripts pinned by
  SHA-256, archive extracted safely and verified locally. For Confidential
  CPU the SEV-SNP report is requested at `--vmpl` (0 by default), and its VMPL
  field must be the level requested (VMPL 1 is no longer required: it was
  snpguest's default request, not the guest's level).
- `vgpu mcp`, a local MCP server over stdio with the same tools and guards
  (`up` needs `ttl` and `max_hourly_price`, `rm` needs `confirm`), plus
  `inference` on the OpenAI-compatible API with a `vgpu_` key.
- Documented exit codes, including 6 for HTTP 402 (`API_KEY_BUDGET_EXCEEDED`)
  and 4 for `API_KEY_SCOPE_MISSING`.
- `vgpu up --ssh-key-id` (`ssh_key_ids` in `vgpu mcp`) on a standard GPU too:
  one key id, needed when the account has several SSH keys. Without it, the
  API's 400 becomes a clear message (exit 2, `SSH_KEY_ID_REQUIRED`).

### Changed
- `voltagegpu balance` no longer shows `$0.00` when the key cannot read the
  balance (agent key without the billing scope): it says the balance is hidden.
- A `vgpu_` inference key in `VOLTAGEGPU_API_KEY` is no longer taken for the
  pod API key; the next source (config file) is used.
- `voltagegpu` and `volt` are otherwise unchanged. The major version marks the new
  command and its exit-code contract.

## [1.3.1] - 2026-10-10

Security hardening after an independent review of 1.3.0. Upgrade recommended.

### Security
- The SSH command returned by the API is only ever displayed in its validated,
  rebuilt form (`ssh -p PORT user@host`). A command of any other form is
  neither shown nor run, so a tampered answer cannot end up copied into a
  shell from `pods show`, `pods wait` or `deploy --wait`.
- Server text (machine names, locations, error messages, ids) can no longer
  inject terminal escape sequences or rich markup: control characters are
  stripped and the text is printed literally. Before, an odd string could also
  crash the CLI right after a deploy.
- `voltagegpu verify` calls voltage-verify in-process instead of
  `python -m voltage_verify`, which put the current directory first on the
  module path (a `voltage_verify.py` next to a downloaded bundle would run).
- `voltagegpu ssh` looks for `ssh` on PATH only, never in the current
  directory (Windows `shutil.which` checks it first), and never runs a
  `.bat`/`.cmd` script.
- The one-time sudo password of a Confidential VM is printed exactly as
  received (no markup or emoji parsing could alter it).
- The API key is only sent to `voltagegpu.com` hosts (or localhost) unless
  `VOLT_ALLOW_CUSTOM_BASE_URL=1` is set.
- A malformed `~/.volt/config.ini` no longer prints its content (which may be
  the key) in an error; an existing file is set to 0600 before the key is
  written.

### Changed
- `deploy standard --cheapest --yes` requires `--max-price`, so a script never
  rents an expensive machine by surprise.
- If the deploy request gets no answer, the CLI says the deploy may have gone
  through and to check `voltagegpu pods list`, instead of suggesting a retry.
- A warning is printed if the hourly price rose between the listing and the
  deploy.

## [1.3.0] - 2026-10-10

Realigns the CLI with the product as it is today: Confidential VMs (Intel TDX)
and standard pods, both deployable from the public API. Every command was run
against the production API before release, including a paid deploy, SSH and
release of a standard pod.

### Added
- `voltagegpu` command (the `volt` command still works, same program).
- `voltagegpu machines [--standard | --confidential] [--gpu] [--max-price] [--min-gpus] [--json]`:
  standard machines and Confidential VM tiers with the hourly price charged.
- `voltagegpu deploy standard --machine ID | --cheapest`: deploy a standard pod
  (no enclave), with `--template`, `--ssh-key`, `--jupyter`, `--ports`,
  `--auto-terminate`, `--wait`. Asks for confirmation before charging; scripts
  pass `--yes`.
- `voltagegpu deploy confidential --tier TIER`: deploy a Confidential VM, with
  `--hardstop`, repeatable `--port 8000[/tcp|udp]` and `--wait`. Prints the
  one-time sudo password and the in-VM attestation commands.
- `voltagegpu templates`: images available for standard pods.
- `voltagegpu pods wait POD_ID`: wait until SSH is ready.
- `voltagegpu ssh POD_ID [-- COMMAND]`: run ssh directly (or `--print` it).
- `voltagegpu balance`: balance and running pods (`GET /api/volt/balance`).
- `voltagegpu login` / `logout`: store the API key in `~/.volt/config.ini`,
  created with mode 0600, after checking that the API accepts it.
- `voltagegpu verify ...`: verifies a Confidential VM attestation bundle by
  running voltage-verify (`pip install "voltagegpu-cli[verify]"`).
- `VOLTAGEGPU_API_KEY` is accepted as an alias of `VOLT_API_KEY`.
- SDK: `list_machines`, `list_templates`, `deploy_standard`,
  `deploy_confidential`, `release_pod`, `wait_until_reachable`, `get_balance`;
  typed immutable models; `parse_ssh_command`.
- Test suite (unit, CLI and opt-in live checks) and a CI workflow.

### Changed
- `pods stop` now releases the pod (`DELETE /api/volt/pods/:id`) and prints
  the refunded unused prepaid time. Standard pods cannot be paused, so the old
  `POST .../stop` call failed for them. `pods delete` and `pods get` remain as
  aliases of `pods stop` and `pods show`.
- Errors show the API's own message (for example "Insufficient balance. One
  hour costs $0.59...") instead of a raw HTTP exception, and never a traceback
  unless `VOLT_DEBUG=1`.
- Python 3.10 or newer is required (3.9 reached end of life in October 2025).

### Removed
- `volt cc inventory`, `volt cc deploy` and `volt templates get`: Confidential
  containers were retired on 2026-10-01. The commands now print how to deploy a
  Confidential VM instead.
- `volt account info`, which called an endpoint that does not exist.

### Fixed
- `ssh-keys add` sent `publicKey`; the API expects `public_key`, so every key
  upload failed with HTTP 400. It also refuses a private key file now.
- `account balance` called a dashboard-only endpoint and always failed with
  HTTP 401.
- `cc inventory` called a dashboard-only endpoint and reported "no GPUs
  available" instead of an error.
- Pod ids are validated before they are put into a URL path.

### Security
- The SSH command returned by the API is parsed into user, host and port, each
  checked against a strict pattern, and ssh is started from a list of
  arguments, never through a shell. A tampered answer such as
  `ssh -o ProxyCommand=...` is refused.
- Plain `http://` base URLs are refused (except localhost), redirects are never
  followed, and TLS certificates are always verified, so the API key cannot be
  sent in clear or to another host.
- The API key is never printed (only `volt_****` and its last four
  characters), including in `repr()` of the configuration.

## [1.2.1] - 2026-04-11

- Confidential container templates (`volt templates`), the machines list and
  `volt cc deploy --template`.

## [1.2.0] - 2026-04-11

- `volt cc` command group for Confidential containers; default API base URL
  `https://api.voltagegpu.com/api`; JSON output on list commands.

## [1.1.0] and [1.0.0]

- First releases of the CLI and Python SDK.
