"""``vgpu attest``, local side: the challenge, the scripts run in the machine, the archive and its verification.

The challenge is drawn HERE, on the user's machine, never in the VM: a VM that
picked its own challenge could replay an old bundle. The archive is verified
here too. ``verified`` is True only when every required check passed:

    vm   (Intel TDX + NVIDIA GPU) ``voltage-verify`` on bundle.json, with the challenge we drew
    cpu  (AMD SEV-SNP) REPORT_DATA of report.bin equals our challenge, policy without
         debug at VMPL 1, and the AMD chain fetched again from AMD here (never the
         certificates of the archive, which come from the VM), pinned ARK, checked by
         a LOCAL ``snpguest``. Without snpguest or without AMD, the result is
         INCOMPLETE, never VERIFIED.

The two scripts are copies of the ones published on voltagegpu.com, pinned by
SHA-256: a modified copy is refused rather than run.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SCRIPTS = {"vm": "two-proofs-in-the-vm.sh", "cpu": "snp-proof-in-the-vm.sh"}
SCRIPT_SHA256 = {
    "two-proofs-in-the-vm.sh": "11cf09cf79ecb43924d9c3f3e6eb8120a4a7e80cd86e3a49679e95668092f9e3",
    "snp-proof-in-the-vm.sh": "cbc27967cf602249f3b09dfa54bf82c0b9d4bea004450095c9af803cd98ed8d5",
}
ARCHIVES = {"vm": "/tmp/proofs.tgz", "cpu": "/tmp/snp-proof.tgz"}  # noqa: S108 (written by the scripts, inside the VM)
TEE = {"vm": "Intel TDX", "cpu": "AMD SEV-SNP"}
# The GPU manifest takes a 32-byte challenge, SEV-SNP REPORT_DATA is 64 bytes.
_CHALLENGE_BYTES = {"vm": 32, "cpu": 64}
_HEX_RE = re.compile(r"^(?:[0-9a-f]{64}|[0-9a-f]{128})$")
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_MEMBERS = 64
# REPORT_DATA: 64 bytes at offset 0x50 of the SEV-SNP attestation report.
_REPORT_DATA = slice(80, 144)


class AttestError(Exception):
    """Attestation cannot be run or checked (wrong machine kind, bad script, unsafe archive)."""


class ToolMissing(AttestError):
    """A local tool needed to verify is not installed (voltage-verify, ssh, scp)."""


@dataclass
class AttestResult:
    verified: bool
    status: str  # VERIFIED, NOT_VERIFIED or INCOMPLETE
    tee: str
    challenge: str
    failed: List[str] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    bundle_path: Optional[str] = None
    in_vm_chain_report: Optional[str] = None
    facts: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def new_challenge(kind: str) -> str:
    return secrets.token_hex(_CHALLENGE_BYTES[kind])


def remote_command(challenge: str) -> str:
    """The command run over SSH; the script itself is sent on stdin."""
    if not _HEX_RE.fullmatch(challenge or ""):
        raise AttestError("The challenge must be lowercase hex (32 or 64 bytes).")
    return f"CHALLENGE={challenge} bash -s"


def load_script(kind: str) -> bytes:
    if kind not in SCRIPTS:
        raise AttestError(f"This machine ({kind}) has no tenant attestation: only Confidential VMs (vm) and Confidential CPU (cpu) do.")
    name = SCRIPTS[kind]
    data = resources.files("volt.sdk").joinpath("scripts", name).read_bytes()
    if hashlib.sha256(data).hexdigest() != SCRIPT_SHA256[name]:
        raise AttestError(f"The embedded {name} does not match its pinned SHA-256; reinstall voltagegpu-cli.")
    return data


def extract_archive(archive: Path, dest: Path) -> Path:
    """Extract regular files and directories only, inside ``dest``, with size limits."""
    if archive.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise AttestError("The proof archive is larger than expected; refusing it.")
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) > _MAX_MEMBERS:
                raise AttestError("The proof archive has too many entries; refusing it.")
            total = 0
            for m in members:
                target = (root / m.name).resolve()
                if m.name.startswith(("/", "\\")) or (target != root and root not in target.parents):
                    raise AttestError(f"Refusing archive entry outside the output folder: {m.name!r}")
                if not (m.isfile() or m.isdir()):
                    raise AttestError(f"Refusing archive entry that is not a plain file: {m.name!r}")
                total += m.size
                if total > _MAX_ARCHIVE_BYTES:
                    raise AttestError("The proof archive expands beyond the size limit; refusing it.")
            for m in members:
                target = (root / m.name).resolve()
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    raise AttestError(f"Unreadable archive entry {m.name!r}")
                target.write_bytes(src.read())
    except tarfile.TarError as exc:
        raise AttestError(f"The proof archive is not a valid .tgz ({type(exc).__name__}).") from None
    return root


def _check_sums(folder: Path, required: List[str]) -> Optional[str]:
    """None when SHA256SUMS lists every required file and every listed hash matches."""
    sums = folder / "SHA256SUMS"
    if not sums.is_file():
        return "SHA256SUMS missing"
    listed: Dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            listed[parts[1].lstrip("*")] = parts[0].lower()
    for name in required:
        if name not in listed:
            return f"{name} not listed in SHA256SUMS"
    for name, digest in listed.items():
        path = (folder / name).resolve()
        if folder not in path.parents or not path.is_file():
            return f"{name} listed in SHA256SUMS but missing"
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return f"{name} does not match SHA256SUMS"
    return None


def _result(tee: str, challenge: str, checks: List[Dict[str, Any]], **extra: Any) -> AttestResult:
    failed = [c["name"] for c in checks if c.get("required", True) and not c["ok"]]
    return AttestResult(verified=not failed, status="VERIFIED" if not failed else "NOT_VERIFIED", tee=tee,
                        challenge=challenge, failed=failed, checks=checks, **extra)


def verify_gpu(folder: Path, challenge: str, *, offline: bool = False) -> AttestResult:
    """voltage-verify on bundle.json with OUR challenge (online: Intel PCS and NVIDIA keys are fetched)."""
    try:
        from voltage_verify.bundle import Bundle, BundleError
        from voltage_verify.verify import verify_bundle
    except ImportError:
        raise ToolMissing("voltage-verify is not installed here: pip install 'voltagegpu-cli[verify]'") from None
    checks: List[Dict[str, Any]] = []
    problem = _check_sums(folder, ["manifest.json", "bundle.json"])
    checks.append({"name": "sha256sums", "ok": problem is None, "detail": problem or "archive checksums match"})
    bundle_path = folder / "bundle.json"
    try:
        bundle = Bundle.load(bundle_path)
    except (BundleError, OSError, ValueError) as exc:
        checks.append({"name": "bundle.format", "ok": False, "detail": f"unreadable bundle ({type(exc).__name__})"})
        return _result(TEE["vm"], challenge, checks, bundle_path=str(bundle_path))
    report = verify_bundle(bundle, expected_challenge=challenge, online=not offline)
    checks += [{"name": c.name, "ok": c.ok, "detail": c.detail, "required": c.required} for c in report.checks]
    facts = {k: v for k, v in report.facts.items() if k in ("tdx", "nvidia", "commitment_sha256")}
    return _result(TEE["vm"], challenge, checks, bundle_path=str(bundle_path), facts=facts)


Runner = Callable[..., Any]

# Exit codes of snp-proof-in-the-vm.sh, as check names (also used for the checks done here).
SNP_SCRIPT_EXIT = {2: "challenge.format", 3: "report_data", 4: "report.debug", 5: "report.vmpl", 6: "amd.ark"}
# The guest runs at VMPL 1 under the provider's paravisor (same value as EXPECTED_VMPL in the script).
_EXPECTED_VMPL = 1
_ARK_PIN_RE = re.compile(r"^ARK_([A-Z]+)_SHA256=([0-9a-f]{64})$", re.MULTILINE)
_PEM_RE = re.compile(rb"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.DOTALL)


def ark_pins() -> Dict[str, str]:
    """AMD root fingerprints, read from the embedded script (itself pinned): one source for both."""
    text = load_script("cpu").decode("utf-8")
    return {family.capitalize(): digest for family, digest in _ARK_PIN_RE.findall(text)}


def _ark_family(pem_path: Path) -> Optional[str]:
    """Same check as ``snp-proof-in-the-vm.sh --check-ark``: SHA-256 of the DER certificate."""
    try:
        match = _PEM_RE.search(pem_path.read_bytes())
        der = base64.b64decode(b"".join(match.group(1).split()), validate=True) if match else b""
    except (OSError, ValueError):
        return None
    digest = hashlib.sha256(der).hexdigest()
    return next((family for family, pin in ark_pins().items() if der and pin == digest), None)


def _policy_checks(data: bytes) -> List[Dict[str, Any]]:
    """Same checks as ``--check-report-policy``: GUEST_POLICY DEBUG bit off, VMPL as expected."""
    if len(data) < 52:
        return [{"name": "report.debug", "ok": False, "detail": "report too short"}]
    debug = bool(data[10] & 8)
    vmpl = int.from_bytes(data[48:52], "little")
    return [
        {"name": "report.debug", "ok": not debug,
         "detail": "debug not allowed" if not debug else "policy allows debug: the host could read the guest memory"},
        {"name": "report.vmpl", "ok": vmpl == _EXPECTED_VMPL, "detail": f"VMPL {vmpl}, expected {_EXPECTED_VMPL}"},
    ]


def _incomplete(result: AttestResult, detail: str) -> AttestResult:
    """Everything checkable passed but the AMD chain could not be checked here: never VERIFIED."""
    result.checks.append({"name": "amd.chain", "ok": False, "detail": detail})
    if result.verified:
        result.verified, result.status = False, "INCOMPLETE"
    else:
        result.failed.append("amd.chain")
    return result


def verify_cpu(folder: Path, challenge: str, *, snpguest: Optional[str], runner: Runner) -> AttestResult:
    """The client-side SEV-SNP check of SNP_VERIFY_COMMANDS (voltagegpu.com, 27/10/2026).

    REPORT_DATA == our challenge, policy (no debug, VMPL 1), then the AMD chain and
    the VCEK fetched again FROM AMD into a new folder, the pinned ARK, and
    ``snpguest verify certs`` / ``verify attestation`` with those certificates.
    The certificates of the archive come from the VM and are never used.
    """
    checks: List[Dict[str, Any]] = []
    problem = _check_sums(folder, ["report.bin", "challenge.txt"])
    checks.append({"name": "sha256sums", "ok": problem is None, "detail": problem or "archive checksums match"})
    report = folder / "report.bin"
    data = report.read_bytes() if report.is_file() else b""
    got = data[_REPORT_DATA].hex() if len(data) >= _REPORT_DATA.stop else ""
    match = got == challenge.lower()
    checks.append({"name": "report_data", "ok": match,
                   "detail": "REPORT_DATA is the challenge drawn here" if match
                   else "REPORT_DATA differs from our challenge (replay or foreign report)"})
    checks += _policy_checks(data)
    chain_txt = folder / "verify.txt"
    in_vm = chain_txt.read_text(encoding="utf-8", errors="replace")[:4000] if chain_txt.is_file() else None
    result = _result(TEE["cpu"], challenge, checks, bundle_path=str(report), in_vm_chain_report=in_vm)
    if snpguest is None:
        return _incomplete(result, "snpguest 0.11.0 not found on this machine: AMD chain not checked here")
    amd = Path(tempfile.mkdtemp(prefix="amd-chain-", dir=folder.parent))  # new folder, fed by AMD only
    for argv in ([snpguest, "fetch", "ca", "pem", str(amd), "-r", "report.bin"], [snpguest, "fetch", "vcek", "pem", str(amd), "report.bin"]):
        if runner(argv, cwd=str(folder), capture_output=True, text=True, timeout=120).returncode != 0:
            return _incomplete(result, "could not fetch the AMD chain or the VCEK from the AMD key server")
    family = _ark_family(amd / "ark.pem")
    checks.append({"name": "amd.ark", "ok": family is not None,
                   "detail": f"ARK matches the AMD {family} root" if family else "ARK is not one of the pinned AMD roots"})
    for name, argv in (("amd.chain", [snpguest, "verify", "certs", str(amd)]),
                       ("amd.report_signature", [snpguest, "verify", "attestation", str(amd), "report.bin"])):
        ok = runner(argv, cwd=str(folder), capture_output=True, text=True, timeout=120).returncode == 0
        checks.append({"name": name, "ok": ok, "detail": f"{' '.join(argv[1:3])} with the chain fetched from AMD: {'OK' if ok else 'FAILED'}"})
    return _result(TEE["cpu"], challenge, checks, bundle_path=str(report), in_vm_chain_report=in_vm,
                   facts={"ark_family": family, "amd_chain_folder": str(amd)})


__all__ = ["ARCHIVES", "AttestError", "AttestResult", "SCRIPTS", "SCRIPT_SHA256", "TEE", "ToolMissing", "extract_archive",
           "ark_pins", "load_script", "new_challenge", "SNP_SCRIPT_EXIT", "remote_command", "verify_cpu", "verify_gpu"]
