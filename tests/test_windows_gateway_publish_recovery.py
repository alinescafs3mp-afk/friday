"""Contracts for the fail-closed Windows gateway publication recovery watcher."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "deploy" / "secondary-brain" / "windows-sglang" / "scripts"
RECOVERY = SCRIPTS / "gateway-publish-recovery.ps1"
NATIVE_TEST = SCRIPTS / "test-gateway-publish-recovery.ps1"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_recovery_watcher_has_a_closed_exact_action_surface() -> None:
    source = RECOVERY.read_text(encoding="utf-8")
    normalized = _normalized(RECOVERY)

    assert all(byte < 128 for byte in RECOVERY.read_bytes())
    assert "$script:GatewayContainerName = 'friday-secondary-gateway'" in source
    assert "$script:GatewayContainerPort = '8443/tcp'" in source
    assert "$script:GatewayHostPort = '8443'" in source
    assert "$script:MissingConfirmations = 2" in source
    assert "name=^/" in source
    assert "Get-FridayGatewayRecoveryAssessment" in source
    assert "Configured gateway port binding" in source
    assert "Effective gateway port publication" in source
    assert "com.docker.compose.service" in source
    assert "-Arguments @('restart', '--time', '30', $script:GatewayContainerName)" in normalized
    assert normalized.count("-Arguments @('restart'") == 1

    forbidden = (
        "docker compose",
        "docker-compose",
        "friday-secondary-gptoss20b",
        "provision-secrets",
        ".env",
        "authorization:",
        "bearer ",
        "start-process",
    )
    folded = source.casefold()
    for token in forbidden:
        assert token not in folded


def test_recovery_is_bounded_and_fails_closed_on_ambiguous_evidence() -> None:
    source = RECOVERY.read_text(encoding="utf-8")
    normalized = _normalized(RECOVERY)

    assert "[ValidateRange(2, 90)][int]$ReadinessAttempts" in normalized
    assert "[ValidateRange(1, 60)][int]$PostRestartAttempts" in normalized
    assert "[ValidateRange(1, 30)][int]$RetryDelaySeconds" in normalized
    assert "for ($attempt = 1; $attempt -le $ReadinessAttempts; $attempt += 1)" in normalized
    assert "for ($postAttempt = 1; $postAttempt -le $PostRestartAttempts;" in normalized
    assert "if ($missingConfirmations -ge $script:MissingConfirmations)" in normalized
    assert "Gateway publication evidence is inconsistent; refusing recovery." in source
    assert "without an authorized action" in source
    assert "did not recover inside the bounded verification window" in source
    assert "restarts = 1" in source
    assert "restarts = 0" in source
    assert "ConvertFrom-Json -ErrorAction Stop" in source
    assert "2>$null" in source


def test_at_logon_installation_is_explicit_non_overwriting_and_non_elevated() -> None:
    source = RECOVERY.read_text(encoding="utf-8")
    normalized = _normalized(RECOVERY)

    assert "[switch]$InstallAtLogon" in source
    assert "New-ScheduledTaskTrigger -AtLogOn -User $currentUser" in normalized
    assert "-LogonType Interactive" in source
    assert "-RunLevel Limited" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "-StartWhenAvailable" in source
    assert "Recovery scheduled task already exists; refusing to overwrite it." in source
    assert "Register-ScheduledTask" in source
    assert "-Force" not in source
    assert "-ExecutionPolicy" not in source
    assert "-InstallAtLogon" not in normalized.split("$taskArguments = @(", 1)[1]


def test_native_projection_covers_noop_recovery_and_failure_boundaries() -> None:
    source = NATIVE_TEST.read_text(encoding="utf-8")

    assert all(byte < 128 for byte in NATIVE_TEST.read_bytes())
    for case in (
        "exact publication is healthy",
        "empty effective publication is recoverable evidence",
        "listener without publication is inconsistent",
        "wrong container name",
        "wrong compose service ownership",
        "wrong configured bind IP",
        "extra configured binding",
        "two missing snapshots are required",
        "healthy endpoint is never restarted",
        "inconsistent initial evidence never restarts",
        "LAN wait is bounded",
        "post-restart verification is bounded without a second restart",
    ):
        assert case in source
    assert "-LibraryOnly" in source
    assert "gateway publish recovery projection: PASS" in source


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="PowerShell is unavailable on this host",
)
def test_native_powershell_projection_passes() -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and repository-owned test
        [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(NATIVE_TEST),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "gateway publish recovery projection: PASS" in completed.stdout
