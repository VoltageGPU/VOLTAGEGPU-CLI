#!/usr/bin/env bash
# Generate an AMD SEV-SNP attestation report from INSIDE a VoltageGPU Confidential CPU VM
# (cpu-*-amd tiers), bound to a challenge YOU choose, then pack it with checksums so you
# can check it on your own machine. No GPU on these tiers, so no GPU step.
#
# Run it from your laptop against the VM (the API returns ssh_command):
#   CHALLENGE=$(python3 -c 'import secrets;print(secrets.token_hex(64))')
#   ssh -p <port> ubuntu@<ip> "CHALLENGE=$CHALLENGE bash -s" < snp-proof-in-the-vm.sh
#
# The check that counts is the one on YOUR machine. Everything printed inside the VM, and
# every certificate in the archive, comes from the VM itself: a fake VM could ship its own
# root. So the AMD chain and the VCEK are fetched again from AMD, on your machine, in a new
# directory, and you use those, never the certs from the archive:
#   scp -P <port> ubuntu@<ip>:/tmp/snp-proof.tgz .
#   D=$(mktemp -d) && tar xzf snp-proof.tgz -C "$D" && (cd "$D" && sha256sum -c SHA256SUMS)
#   CHALLENGE=$CHALLENGE bash snp-proof-in-the-vm.sh --check-report-data "$D/report.bin"
#   bash snp-proof-in-the-vm.sh --check-report-policy "$D/report.bin"   # no debug, VMPL 1
#   A=$(mktemp -d)        # new directory, fed by AMD only (snpguest 0.11.0 on your machine)
#   (cd "$D" && snpguest fetch ca pem "$A" -r report.bin && snpguest fetch vcek pem "$A" report.bin)
#   bash snp-proof-in-the-vm.sh --check-ark "$A/ark.pem"               # pinned AMD root
#   (cd "$D" && snpguest verify certs "$A" && snpguest verify attestation "$A" report.bin)
#
# What this does NOT prove: the launch measurement is not compared with any published
# reference image (it proves a genuine SEV-SNP guest on an AMD chip, not which exact image
# runs), and the guest runs at VMPL 1 under the provider's paravisor (VMPL 0).
set -euo pipefail

SNPGUEST_VERSION=0.11.0
SNPGUEST_SHA256=51b4d3eafd4f5a68bc3b2dc2fb858253b37790ea691b927266bde5ebfdc2a9a8
SNPGUEST_URL="https://github.com/virtee/snpguest/releases/download/v${SNPGUEST_VERSION}/snpguest"

# SHA-256 fingerprints (of the DER certificate) of the AMD root keys (ARK), self-signed.
# Source: the AMD key server, https://kdsintf.amd.com/vcek/v1/<Milan|Genoa|Turin>/cert_chain
# (second certificate of each chain, CN=ARK-<family>), fetched and checked on 2026-10-27.
ARK_MILAN_SHA256=69d063b45344d26a2e94e1f4210de49ef555308287d4c174445c95639a540bcd
ARK_GENOA_SHA256=4c6598d19c18719c5dfd4a7d335f674e5bfe1d8f800cea2cf270c10d103db2f1
ARK_TURIN_SHA256=1f084161a44bb6d93778a904877d4819cafa5d05ef4193b2ded9dd9c73dd3f6a

# The guest runs at VMPL 1 under the provider's paravisor (seen on 2026-10-27).
EXPECTED_VMPL=1

# REPORT_DATA is 64 bytes at offset 0x50 of the report (AMD SEV-SNP ABI, ATTESTATION_REPORT).
report_data_hex() {
  od -An -tx1 -v -j 80 -N 64 "$1" | tr -d ' \n'
}

# The challenge: exactly 64 bytes as 128 hex characters, chosen outside the VM.
check_challenge() {
  CHALLENGE=$(printf '%s' "${CHALLENGE:-}" | tr 'A-F' 'a-f')
  if ! printf '%s' "$CHALLENGE" | grep -Eq '^[0-9a-f]{128}$'; then
    echo "CHALLENGE must be 64 bytes in hex (128 characters), chosen on YOUR machine:" >&2
    echo "  CHALLENGE=\$(python3 -c 'import secrets;print(secrets.token_hex(64))')" >&2
    exit 2
  fi
}

check_report_data() {
  local got
  got=$(report_data_hex "$1")
  if [ "$got" != "$CHALLENGE" ]; then
    echo "REPORT_DATA does not match CHALLENGE" >&2
    echo "  report:    $got" >&2
    echo "  challenge: $CHALLENGE" >&2
    exit 3
  fi
  echo "REPORT_DATA = CHALLENGE: OK"
}

# The ARK must be one of the pinned AMD roots, whatever the chain check says about it.
check_ark() {
  local fp family=""
  fp=$(openssl x509 -in "$1" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f')
  case "$fp" in
    "$ARK_MILAN_SHA256") family=Milan ;;
    "$ARK_GENOA_SHA256") family=Genoa ;;
    "$ARK_TURIN_SHA256") family=Turin ;;
  esac
  if [ -z "$family" ]; then
    echo "ARK is not one of the AMD roots (Milan, Genoa, Turin): $fp" >&2
    exit 6
  fi
  echo "ARK matches the AMD $family root: OK"
}

# One byte of the report, as a decimal number.
report_byte() {
  od -An -tu1 -v -j "$2" -N 1 "$1" | tr -d ' 
'
}

# GUEST_POLICY is 8 bytes at 0x08 (bit 19 = DEBUG, in byte 0x0A), VMPL 4 bytes at 0x30.
check_report_policy() {
  local b vmpl
  b=$(report_byte "$1" 10)
  if [ $(( b & 8 )) -ne 0 ]; then
    echo "report policy allows debug (GUEST_POLICY bit 19): the host could read the guest memory" >&2
    exit 4
  fi
  vmpl=$(( $(report_byte "$1" 48) + 256 * $(report_byte "$1" 49) + 65536 * $(report_byte "$1" 50) + 16777216 * $(report_byte "$1" 51) ))
  if [ "$vmpl" -ne "$EXPECTED_VMPL" ]; then
    echo "report VMPL is $vmpl, expected $EXPECTED_VMPL" >&2
    exit 5
  fi
  echo "report policy: debug not allowed, VMPL $EXPECTED_VMPL: OK"
}

# Offline modes, no VM needed (run them on your machine):
#   bash snp-proof-in-the-vm.sh --check-report-policy report.bin
#   bash snp-proof-in-the-vm.sh --check-ark ark.pem
case "${1:-}" in
  --check-report-policy) check_report_policy "${2:?report.bin path}"; exit 0 ;;
  --check-ark) check_ark "${2:?ark.pem path}"; exit 0 ;;
esac

# Offline mode, no VM needed: check the REPORT_DATA of an existing report against CHALLENGE.
#   CHALLENGE=<hex> bash snp-proof-in-the-vm.sh --check-report-data report.bin
if [ "${1:-}" = "--check-report-data" ]; then
  check_challenge
  check_report_data "${2:?report.bin path}"
  exit 0
fi

check_challenge
WORK=$(mktemp -d /tmp/snp-proof.XXXXXX)
cd "$WORK"

echo "== environment"
uname -r; systemd-detect-virt || true
sudo dmesg | grep -E "Memory Encryption Features active|sev-guest" || true
ls -l /dev/sev-guest

echo "== snpguest ${SNPGUEST_VERSION}, pinned by SHA-256"
curl -fsSL "$SNPGUEST_URL" -o snpguest
echo "${SNPGUEST_SHA256}  snpguest" | sha256sum -c -
chmod +x snpguest
./snpguest --version

echo "== 1. attestation report bound to your challenge"
python3 -c 'import sys;sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))' "$CHALLENGE" > request.bin
sudo ./snpguest report report.bin request.bin
sudo chown "$(id -u):$(id -g)" report.bin
./snpguest display report report.bin > report.txt
grep -E "^Version|^VMPL|Debug Allowed" report.txt || true
check_report_data report.bin
check_report_policy report.bin

echo "== 2. AMD certificates (ARK, ASK, VCEK) from the AMD key server"
mkdir -p certs
./snpguest fetch ca pem ./certs -r report.bin \
  || ./snpguest fetch ca pem ./certs genoa \
  || ./snpguest fetch ca pem ./certs milan
./snpguest fetch vcek pem ./certs report.bin \
  || ./snpguest fetch vcek pem ./certs -r report.bin
ls -l certs/ark.pem certs/ask.pem certs/vcek.pem
check_ark certs/ark.pem

echo "== 3. chain check INSIDE the VM (the one that counts is done outside)"
./snpguest verify certs ./certs | tee verify.txt
./snpguest verify attestation ./certs report.bin | tee -a verify.txt
grep -q "VEK signed the Attestation Report" verify.txt

printf '%s\n' "$CHALLENGE" > challenge.txt
sha256sum report.bin report.txt challenge.txt verify.txt certs/ark.pem certs/ask.pem certs/vcek.pem > SHA256SUMS
tar czf /tmp/snp-proof.tgz report.bin report.txt challenge.txt verify.txt certs SHA256SUMS
echo "== archive ready: /tmp/snp-proof.tgz"
echo "Now check it on YOUR machine with a chain fetched from AMD there (see the top of this script)."
