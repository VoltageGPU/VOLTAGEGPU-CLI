"""vgpu attest, local side: challenge, embedded scripts, safe archive extraction and the two verifiers.
The GPU bundle is a real one (RTX PRO 6000 Blackwell VM, 2026-10-09), verified offline."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from typing import List

import pytest

from volt.sdk import attest
from volt.sdk.attest import AttestError

FIXTURES = Path(__file__).parent / "fixtures"
GOOD_CHALLENGE = "627ba9c51d4719038e917c7431675b183ac576c819d712178dc8a100f4409a45"


def _tgz(path: Path, files: dict) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _sums(files: dict) -> bytes:
    return "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in files.items()).encode()


def _gpu_archive(tmp_path: Path, bundle: bytes | None = None) -> Path:
    files = {"manifest.json": (FIXTURES / "manifest.json").read_bytes(), "bundle.json": bundle or (FIXTURES / "bundle.json").read_bytes()}
    return _tgz(tmp_path / "proofs.tgz", {**files, "SHA256SUMS": _sums(files)})


def test_challenge_sizes_and_remote_command() -> None:
    gpu, cpu = attest.new_challenge("vm"), attest.new_challenge("cpu")
    assert len(gpu) == 64 and len(cpu) == 128 and gpu != attest.new_challenge("vm")
    assert attest.remote_command(gpu) == f"CHALLENGE={gpu} bash -s"
    # SEV-SNP: the VMPL level is named, never left to snpguest 0.11.0 (which asks for 1 by default).
    assert attest.remote_command(cpu, vmpl=0) == f"CHALLENGE={cpu} SNP_VMPL=0 bash -s"
    assert attest.remote_command(cpu, vmpl=3) == f"CHALLENGE={cpu} SNP_VMPL=3 bash -s"
    with pytest.raises(AttestError):
        attest.remote_command("abc; rm -rf ~")
    for bad in (4, -1, True, "0", 0.0):
        with pytest.raises(AttestError):
            attest.remote_command(cpu, vmpl=bad)


def test_embedded_scripts_match_their_pinned_hash() -> None:
    for kind in ("vm", "cpu"):
        script = attest.load_script(kind)
        assert script.startswith(b"#!/usr/bin/env bash")
        assert hashlib.sha256(script).hexdigest() == attest.SCRIPT_SHA256[attest.SCRIPTS[kind]]
    assert attest.ARCHIVES == {"vm": "/tmp/proofs.tgz", "cpu": "/tmp/snp-proof.tgz"}


def test_embedded_cpu_script_names_the_vmpl_and_no_longer_requires_1() -> None:
    # 2026-10-27: the "VMPL: 1" of the first report was snpguest's default request, not the guest's level.
    text = attest.load_script("cpu").decode("utf-8")
    assert './snpguest report report.bin request.bin --vmpl "$SNP_VMPL"' in text
    assert "SNP_VMPL=${SNP_VMPL:-0}" in text
    assert "EXPECTED_VMPL" not in text and "paravisor" not in text


def test_kind_without_attestation_is_refused() -> None:
    for kind in ("standard", "baremetal", "sandbox"):
        with pytest.raises(AttestError, match="no tenant attestation"):
            attest.load_script(kind)


def test_extract_refuses_path_escape_and_links(tmp_path: Path) -> None:
    bad = _tgz(tmp_path / "bad.tgz", {"../evil": b"x"})
    with pytest.raises(AttestError):
        attest.extract_archive(bad, tmp_path / "out")
    link = tmp_path / "link.tgz"
    with tarfile.open(link, "w:gz") as tar:
        info = tarfile.TarInfo("certs")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc"
        tar.addfile(info)
    with pytest.raises(AttestError):
        attest.extract_archive(link, tmp_path / "out2")


def test_gpu_bundle_with_our_challenge_is_verified(tmp_path: Path) -> None:
    out = attest.extract_archive(_gpu_archive(tmp_path), tmp_path / "out")
    result = attest.verify_gpu(out, GOOD_CHALLENGE, offline=True)
    assert result.verified and result.status == "VERIFIED" and result.failed == []


def test_gpu_bundle_with_another_challenge_fails(tmp_path: Path) -> None:
    out = attest.extract_archive(_gpu_archive(tmp_path), tmp_path / "out")
    result = attest.verify_gpu(out, "00" * 32, offline=True)
    assert not result.verified and "manifest.challenge" in result.failed


def test_gpu_bundle_with_a_modified_byte_fails(tmp_path: Path) -> None:
    raw = bytearray((FIXTURES / "bundle.json").read_bytes())
    i = raw.index(b'"quote_b64"') + 400
    raw[i] = ord("A") if raw[i] != ord("A") else ord("B")
    out = attest.extract_archive(_gpu_archive(tmp_path, bytes(raw)), tmp_path / "out")
    assert not attest.verify_gpu(out, GOOD_CHALLENGE, offline=True).verified


def test_gpu_checksum_mismatch_fails(tmp_path: Path) -> None:
    out = attest.extract_archive(_gpu_archive(tmp_path), tmp_path / "out")
    (out / "manifest.json").write_bytes(b"{}")
    result = attest.verify_gpu(out, GOOD_CHALLENGE, offline=True)
    assert not result.verified and "sha256sums" in result.failed


# ------------------------------------------------------------------ CPU (AMD SEV-SNP)
# The check that counts runs here: chain and VCEK fetched again from AMD into a new folder,
# pinned ARK, never the certificates of the archive (they come from the VM).

ARK_MILAN = (FIXTURES / "ark-milan.pem").read_bytes()


def _report(challenge: str, *, report_data: str | None = None, debug: bool = False, vmpl: int = 0) -> bytes:
    report = bytearray(1184)
    report[10] = 8 if debug else 0  # GUEST_POLICY bit 19 (DEBUG) is bit 3 of byte 0x0A
    report[48:52] = vmpl.to_bytes(4, "little")
    report[80:144] = bytes.fromhex(report_data or challenge)
    return bytes(report)


def _snp_dir(tmp_path: Path, challenge: str, **report_kw) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    files = {"report.bin": _report(challenge, **report_kw), "challenge.txt": (challenge + "\n").encode(),
             "verify.txt": b"VEK signed the Attestation Report!\n",
             "certs/ark.pem": b"FAKE ROOT FROM THE VM", "certs/ask.pem": b"ASK", "certs/vcek.pem": b"VCEK"}
    archive = _tgz(tmp_path / "snp-proof.tgz", {**files, "SHA256SUMS": _sums(files)})
    return attest.extract_archive(archive, tmp_path / "snp")


class FakeSnpguest:
    """Simulates snpguest: `fetch ca` writes the ARK given here into the requested folder."""

    def __init__(self, ark: bytes = ARK_MILAN, verify_code: int = 0, fetch_code: int = 0) -> None:
        self.ark, self.verify_code, self.fetch_code = ark, verify_code, fetch_code
        self.calls: List[List[str]] = []
        self.cwds: List[str] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.cwds.append(str(kwargs.get("cwd")))
        code = self.verify_code if argv[1] == "verify" else self.fetch_code
        if argv[1:3] == ["fetch", "ca"] and code == 0:
            Path(argv[4], "ark.pem").write_bytes(self.ark)
            Path(argv[4], "ask.pem").write_bytes(b"ASK FROM AMD")
        if argv[1:3] == ["fetch", "vcek"] and code == 0:
            Path(argv[4], "vcek.pem").write_bytes(b"VCEK FROM AMD")

        class R:
            returncode = code
            stdout = "ok"
            stderr = ""
        return R()


def test_ark_pins_come_from_the_embedded_script() -> None:
    pins = attest.ark_pins()
    assert set(pins) == {"Milan", "Genoa", "Turin"}
    assert pins["Milan"] == "69d063b45344d26a2e94e1f4210de49ef555308287d4c174445c95639a540bcd"


def test_cpu_verified_with_a_chain_fetched_from_amd_in_a_new_folder(tmp_path: Path) -> None:
    ch = "ab" * 64
    folder = _snp_dir(tmp_path, ch)
    run = FakeSnpguest()
    result = attest.verify_cpu(folder, ch, snpguest="/usr/bin/snpguest", runner=run)
    assert result.verified and result.status == "VERIFIED", result.failed
    fetch_ca, fetch_vcek, verify_certs, verify_att = run.calls
    amd = fetch_ca[4]
    assert fetch_ca == ["/usr/bin/snpguest", "fetch", "ca", "pem", amd, "-r", "report.bin"]
    assert fetch_vcek == ["/usr/bin/snpguest", "fetch", "vcek", "pem", amd, "report.bin"]
    assert verify_certs == ["/usr/bin/snpguest", "verify", "certs", amd]
    assert verify_att == ["/usr/bin/snpguest", "verify", "attestation", amd, "report.bin"]
    assert Path(amd) != folder / "certs" and "certs" not in Path(amd).name
    assert set(run.cwds) == {str(folder)}
    assert result.facts["ark_family"] == "Milan"
    # Requested at VMPL 0 (the default), and the report says 0.
    assert result.facts["vmpl"] == 0 and result.facts["vmpl_requested"] == 0 and result.facts["vmpl_fallback"] is False


def test_cpu_unknown_ark_from_amd_fetch_fails(tmp_path: Path) -> None:
    ch = "ab" * 64
    foreign = b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
    result = attest.verify_cpu(_snp_dir(tmp_path, ch), ch, snpguest="snpguest", runner=FakeSnpguest(ark=foreign))
    assert not result.verified and "amd.ark" in result.failed


def test_cpu_report_data_mismatch_fails(tmp_path: Path) -> None:
    ch = "ab" * 64
    result = attest.verify_cpu(_snp_dir(tmp_path, ch, report_data="cd" * 64), ch, snpguest="snpguest", runner=FakeSnpguest())
    assert not result.verified and result.status == "NOT_VERIFIED" and "report_data" in result.failed


def test_cpu_debug_policy_or_vmpl_other_than_requested_fails(tmp_path: Path) -> None:
    ch = "ab" * 64
    debug = attest.verify_cpu(_snp_dir(tmp_path / "a", ch, debug=True), ch, snpguest="snpguest", runner=FakeSnpguest())
    assert not debug.verified and "report.debug" in debug.failed
    # The VMPL field is the level requested: 2 for a request at 0, or 0 for a request at 1, is refused.
    two = attest.verify_cpu(_snp_dir(tmp_path / "b", ch, vmpl=2), ch, snpguest="snpguest", runner=FakeSnpguest())
    assert not two.verified and "report.vmpl" in two.failed
    zero = attest.verify_cpu(_snp_dir(tmp_path / "c", ch, vmpl=0), ch, snpguest="snpguest", runner=FakeSnpguest(), vmpl=1)
    assert not zero.verified and "report.vmpl" in zero.failed


def test_cpu_vmpl_1_is_no_longer_required_any_level_requested_is_checked(tmp_path: Path) -> None:
    ch = "ab" * 64
    one = attest.verify_cpu(_snp_dir(tmp_path, ch, vmpl=1), ch, snpguest="snpguest", runner=FakeSnpguest(), vmpl=1)
    assert one.verified, one.failed
    assert one.facts["vmpl"] == 1 and one.facts["vmpl_fallback"] is False


def test_cpu_vmpl_1_after_a_refused_vmpl_0_is_accepted_and_reported(tmp_path: Path) -> None:
    # The script asks again at VMPL 1, saying so, when VMPL 0 is refused: a weaker claim, reported as such.
    ch = "ab" * 64
    result = attest.verify_cpu(_snp_dir(tmp_path, ch, vmpl=1), ch, snpguest="snpguest", runner=FakeSnpguest())
    assert result.verified, result.failed
    assert result.facts["vmpl"] == 1 and result.facts["vmpl_requested"] == 0 and result.facts["vmpl_fallback"] is True
    check = next(c for c in result.checks if c["name"] == "report.vmpl")
    assert check["ok"] and "not at VMPL 0" in check["detail"]


def test_cpu_chain_refused_by_snpguest_fails(tmp_path: Path) -> None:
    ch = "ab" * 64
    result = attest.verify_cpu(_snp_dir(tmp_path, ch), ch, snpguest="snpguest", runner=FakeSnpguest(verify_code=1))
    assert not result.verified and result.status == "NOT_VERIFIED" and "amd.chain" in result.failed


def test_cpu_amd_unreachable_is_incomplete(tmp_path: Path) -> None:
    ch = "ab" * 64
    result = attest.verify_cpu(_snp_dir(tmp_path, ch), ch, snpguest="snpguest", runner=FakeSnpguest(fetch_code=1))
    assert not result.verified and result.status == "INCOMPLETE"


def test_cpu_without_local_snpguest_is_incomplete_not_verified(tmp_path: Path) -> None:
    ch = "ab" * 64
    result = attest.verify_cpu(_snp_dir(tmp_path, ch), ch, snpguest=None, runner=FakeSnpguest())
    assert not result.verified and result.status == "INCOMPLETE"
    assert "VEK signed" in result.in_vm_chain_report


def test_in_vm_script_exit_codes_have_names() -> None:
    assert attest.SNP_SCRIPT_EXIT == {2: "challenge.format", 3: "report_data", 4: "report.debug", 5: "report.vmpl", 6: "amd.ark",
                                      7: "report.request"}
