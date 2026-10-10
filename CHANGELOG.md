# Changelog

All notable changes to `voltagegpu-cli` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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
