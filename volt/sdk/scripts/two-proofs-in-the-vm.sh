#!/usr/bin/env bash
# Generate the two proofs (Intel TDX quote + NVIDIA GPU attestation) from INSIDE
# a VoltageGPU Confidential VM, with a challenge you choose, then pack them with
# checksums so you can verify them on your own machine.
#
# Run it from your laptop against the VM (the API returns ssh_command):
#   ssh -p <port> ubuntu@<ip> 'bash -s' < two-proofs-in-the-vm.sh
# Better, pick the challenge on YOUR machine so the VM cannot replay an old bundle:
#   CHALLENGE=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
#   ssh -p <port> ubuntu@<ip> "CHALLENGE=$CHALLENGE bash -s" < two-proofs-in-the-vm.sh
# Then copy the archive off the VM and verify it outside:
#   scp -P <port> ubuntu@<ip>:/tmp/proofs.tgz . && tar xzf proofs.tgz
#   pip install voltage-verify
#   voltage-verify verify bundle.json --challenge "$(python3 -c 'import json;print(json.load(open("manifest.json"))["challenge"])')"
#   (add --offline if you want no network at all)
#
# Same commands as GET /api/confidential/vm/tiers -> attestation.verifyInsideTheVm,
# plus one thing the image does not say: it ships without pip.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME"

echo "== environment"
uname -r; ls -l /dev/tdx_guest; systemd-detect-virt || true
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
nvidia-smi conf-compute -q | grep -E "CC State|Multi-GPU|Ready State" || true

echo "== pip (not on the image)"
if ! python3 -m pip --version >/dev/null 2>&1; then
  curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  python3 /tmp/get-pip.py --user --break-system-packages -q
fi
python3 -m pip install --user --break-system-packages -q "voltage-verify[attest]"
voltage-verify --version || true

echo "== 1. manifest with a random challenge (yours, not the provider's)"
# CHALLENGE: 64 hex characters you generated outside the VM; without it, one is drawn here.
voltage-verify manifest --challenge "${CHALLENGE:-auto}" -o manifest.json
python3 -c 'import json;m=json.load(open("manifest.json"));print("challenge:",m["challenge"])'

echo "== 2. attestation (TDX quote + NRAS), as root"
sudo env PATH="$PATH" PYTHONPATH="$(python3 -c 'import site;print(site.getusersitepackages())')" \
  voltage-verify attest --manifest manifest.json --mode single-gpu -o bundle.json
ls -l bundle.json

echo "== 3. verification INSIDE the VM (the one that counts is done outside)"
voltage-verify verify bundle.json | tail -4

sha256sum manifest.json bundle.json > SHA256SUMS
tar czf /tmp/proofs.tgz manifest.json bundle.json SHA256SUMS
echo "== archive ready: /tmp/proofs.tgz"
