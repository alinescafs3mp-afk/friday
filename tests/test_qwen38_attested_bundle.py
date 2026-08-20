"""Static and mutation gates for the canonical Qwen3.8 Windows bundle."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

_REMOTE = Path(__file__).resolve().parents[1] / "handoffs" / "SGLang-Qwen38-V12-Attested" / "remote"
_COMMON = _REMOTE / "AttestedBundle.Common.ps1"
_POWERSHELL_TEST = _REMOTE / "Test-AttestedBindMountProjection.ps1"
_CAPABILITY_TEST = _REMOTE / "Test-AttestedCapabilityProjection.ps1"
_PUBLISHER_TEST = _REMOTE / "Test-AttestedPublisherObservation.ps1"
_SWITCH = _REMOTE / "Switch-Qwen38V12Attested.ps1"
_WINDOWS_PATH = re.compile(r"(?P<drive>[A-Z]):\\(?P<tail>[A-Za-z0-9._-]+(?:\\[A-Za-z0-9._-]+)*)\Z")


def _powershell_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.find("\nfunction ", start + 1)
    return source[start:] if end == -1 else source[start:end]


def _assert_static_bind_contract(source: str) -> None:
    projection = _powershell_function(source, "Get-ExactDockerDesktopBindSource")
    selector = _powershell_function(source, "Test-ExactAttestedBindSource")
    assertion = _powershell_function(source, "Assert-BindMount")

    assert "$WindowsPath.Length -gt 240) {" in projection
    assert "'\\A(?<drive>[A-Z]):\\\\(?<tail>[A-Za-z0-9._-]+(?:\\\\[A-Za-z0-9._-]+)*)\\z'" in projection
    assert "[Text.RegularExpressions.RegexOptions]::CultureInvariant" in projection
    assert "$segments.Count -gt 32 -or" in projection
    assert "[string]$_ -ceq '.'" in projection
    assert "[string]$_ -ceq '..'" in projection
    assert "$match.Groups['drive'].Value.ToLowerInvariant()" in projection
    assert "'/run/desktop/mnt/host/{0}/{1}'" in projection

    assert "$dockerWindowsPath = $WindowsPath.Replace('\\', '/')" in selector
    assert "$Observed.Length -gt 512) {" in selector
    assert selector.count("[StringComparison]::Ordinal") == 3
    assert "[string]::Equals($Observed, $WindowsPath, [StringComparison]::Ordinal)" in selector
    assert "[string]::Equals($Observed, $dockerWindowsPath, [StringComparison]::Ordinal)" in selector
    assert "[string]::Equals($Observed, $dockerDesktopPath, [StringComparison]::Ordinal)" in selector
    assert "$Observed.Replace" not in selector

    assert "[string]$_.Destination -ceq $Destination" in assertion
    assert "[string]$matches[0].Type -cne 'bind'" in assertion
    assert "Test-ExactAttestedBindSource ([string]$matches[0].Source) $Source" in assertion
    assert "[string]$matches[0].Source.Replace" not in assertion


def _assert_static_capability_contract(source: str) -> None:
    selector = _powershell_function(source, "Test-ExactAttestedProxyCapabilitySet")
    assertion = _powershell_function(source, "Assert-CandidateContainers")

    assert "$values.Count -ne 4" in selector
    assert "[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)" in selector
    assert "$null -eq $entry -or $entry -isnot [string]" in selector
    assert "$value.Length -lt 1 -or $value.Length -gt 32 -or" in selector
    assert "-not $actual.Add($value)" in selector
    assert "@('CHOWN', 'DAC_OVERRIDE', 'SETGID', 'SETUID')" in selector
    assert "@('CAP_CHOWN', 'CAP_DAC_OVERRIDE', 'CAP_SETGID', 'CAP_SETUID')" in selector
    assert "$actual.Contains($composeSpelling[$index])" in selector
    assert "$actual.Contains($dockerRuntimeSpelling[$index])" in selector
    assert "return ($composeMatch -or $dockerRuntimeMatch)" in selector
    assert "OrdinalIgnoreCase" not in selector
    assert ".Replace(" not in selector
    assert ".ToUpper" not in selector
    assert ".ToLower" not in selector

    assert "Test-ExactAttestedProxyCapabilitySet -Observed @($Proxy.HostConfig.CapAdd)" in assertion
    assert "[string]::Join(',', @($Proxy.HostConfig.CapAdd | Sort-Object))" not in assertion


def _assert_static_publisher_contract(common: str, switch: str) -> None:
    classifier = _powershell_function(common, "Test-SolePublisherObservation")
    waiter = _powershell_function(common, "Wait-SolePublisher")

    assert "$script:Attested.StableProxyName" in classifier
    assert "$script:Attested.CandidateProxyName" in classifier
    assert "$publishers.Count -eq 0" in classifier
    assert "return $false" in classifier
    assert "$publishers.Count -eq 1" in classifier
    assert "$publishers[0] -is [string]" in classifier
    assert "[string]$publishers[0] -ceq $ExpectedName" in classifier
    assert "return $true" in classifier
    assert "publisher set is unsafe" in classifier
    assert "OrdinalIgnoreCase" not in classifier

    assert "$TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 120" in waiter
    assert "[DateTime]::UtcNow.AddSeconds($TimeoutSeconds)" in waiter
    assert "docker ps --filter 'publish=8001' --format '{{.Names}}'" in waiter
    assert "if ($LASTEXITCODE -ne 0)" in waiter
    assert "Test-SolePublisherObservation $publishers $ExpectedName" in waiter
    assert "[DateTime]::UtcNow -ge $deadline" in waiter
    assert "Start-Sleep -Milliseconds 250" in waiter

    candidate_wait = "Wait-SolePublisher $script:Attested.CandidateProxyName 120"
    assert switch.count(candidate_wait) == 2
    assert switch.count("Assert-SolePublisher $script:Attested.CandidateProxyName") == 3


def _reference_bind_source_allowed(observed: str, windows_path: str) -> bool:
    if not windows_path or len(windows_path) > 240:
        raise ValueError("unbounded expected source")
    match = _WINDOWS_PATH.fullmatch(windows_path)
    if match is None:
        raise ValueError("non-canonical expected source")
    segments = match.group("tail").split("\\")
    if len(segments) > 32 or any(segment in {".", ".."} for segment in segments):
        raise ValueError("unsafe expected source")
    if not observed or len(observed) > 512:
        return False
    direct_docker = windows_path.replace("\\", "/")
    desktop_tail = match.group("tail").replace("\\", "/")
    desktop = f"/run/desktop/mnt/host/{match.group('drive').lower()}/{desktop_tail}"
    return observed in (windows_path, direct_docker, desktop)


def _reference_capability_set_allowed(observed: tuple[object, ...]) -> bool:
    if len(observed) != 4 or any(not isinstance(value, str) for value in observed):
        return False
    values = tuple(value for value in observed if isinstance(value, str))
    if any(not value or len(value) > 32 for value in values) or len(set(values)) != 4:
        return False
    accepted = (
        frozenset(("CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID")),
        frozenset(("CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_SETGID", "CAP_SETUID")),
    )
    return frozenset(values) in accepted


def test_bind_source_contract_accepts_only_three_exact_canonical_spellings() -> None:
    expected = r"D:\jarvis\cache\sglang-qwen38-v12-attested"
    accepted = (
        expected,
        "D:/jarvis/cache/sglang-qwen38-v12-attested",
        "/run/desktop/mnt/host/d/jarvis/cache/sglang-qwen38-v12-attested",
    )
    rejected = (
        r"D:\jarvis/cache\sglang-qwen38-v12-attested",
        "D:/jarvis\\cache/sglang-qwen38-v12-attested",
        r"d:\jarvis\cache\sglang-qwen38-v12-attested",
        "d:/jarvis/cache/sglang-qwen38-v12-attested",
        r"E:\jarvis\cache\sglang-qwen38-v12-attested",
        r"D:\Jarvis\cache\sglang-qwen38-v12-attested",
        r"D:\jarvis\cache\..\cache\sglang-qwen38-v12-attested",
        "/run/desktop/mnt/host/c/jarvis/cache/sglang-qwen38-v12-attested",
        "/run/desktop/mnt/host/D/jarvis/cache/sglang-qwen38-v12-attested",
        "/Run/desktop/mnt/host/d/jarvis/cache/sglang-qwen38-v12-attested",
        "/run/desktop/mnt/host/d/Jarvis/cache/sglang-qwen38-v12-attested",
        "/run/desktop/mnt/host/d/jarvis/cache/../cache/sglang-qwen38-v12-attested",
        "/run/desktop/mnt/host/d/jarvis/cache/sglang-qwen38-v12-attested/",
    )

    assert all(_reference_bind_source_allowed(value, expected) for value in accepted)
    assert not any(_reference_bind_source_allowed(value, expected) for value in rejected)


@pytest.mark.parametrize(
    "expected",
    (
        r"d:\jarvis\cache\sglang-qwen38-v12-attested",
        "D:/jarvis/cache/sglang-qwen38-v12-attested",
        r"D:\jarvis\cache\..\cache\sglang-qwen38-v12-attested",
        r"D:\jarvis\\cache\sglang-qwen38-v12-attested",
        r"D:\jarvis\cache\sglang qwen38",
    ),
)
def test_bind_source_contract_rejects_noncanonical_expected_roots(expected: str) -> None:
    with pytest.raises(ValueError):
        _reference_bind_source_allowed(expected, expected)


@pytest.mark.parametrize(
    ("old", "mutation"),
    (
        ("$WindowsPath.Length -gt 240) {", "$WindowsPath.Length -gt 2400) {"),
        ("(?<drive>[A-Z])", "(?<drive>[A-Za-z])"),
        ("$segments.Count -gt 32 -or", "$segments.Count -gt 320 -or"),
        ("[string]$_ -ceq '..'", "[string]$_ -ceq '...'"),
        (".Value.ToLowerInvariant()", ".Value.ToUpperInvariant()"),
        ("/run/desktop/mnt/host/", "/run/desktop/mnt/hosts/"),
        ("$Observed.Length -gt 512) {", "$Observed.Length -gt 5120) {"),
        ("[StringComparison]::Ordinal", "[StringComparison]::OrdinalIgnoreCase"),
        (
            "$dockerWindowsPath = $WindowsPath.Replace('\\', '/')",
            "$dockerWindowsPath = $Observed.Replace('\\', '/')",
        ),
        (
            "Test-ExactAttestedBindSource ([string]$matches[0].Source) $Source",
            "[string]$matches[0].Source.Replace('/', '\\') -ceq $Source",
        ),
    ),
)
def test_static_gate_kills_bind_projection_contract_mutations(old: str, mutation: str) -> None:
    source = _COMMON.read_text(encoding="utf-8")
    _assert_static_bind_contract(source)
    assert old in source

    with pytest.raises(AssertionError):
        _assert_static_bind_contract(source.replace(old, mutation, 1))


def test_powershell_mutation_matrix_covers_runtime_spellings_and_near_misses() -> None:
    source = _POWERSHELL_TEST.read_text(encoding="utf-8")
    required = (
        "Assert-Accepted 'D:/jarvis/cache/sglang-qwen38-v12-attested' $cachePath",
        "Assert-Accepted 'D:/jarvis/data/models/qwen3.8-27b-nvfp4-a2genesis-bfd9b312' $modelPath",
        "'/run/desktop/mnt/host/d/jarvis/cache/sglang-qwen38-v12-attested'",
        "'D:\\jarvis/cache\\sglang-qwen38-v12-attested'",
        "'D:/jarvis\\cache/sglang-qwen38-v12-attested'",
        "'/run/desktop/mnt/host/c/jarvis/cache/sglang-qwen38-v12-attested'",
        "'/run/desktop/mnt/host/D/jarvis/cache/sglang-qwen38-v12-attested'",
        "'/run/desktop/mnt/host/d/jarvis/cache/../cache/sglang-qwen38-v12-attested'",
        "('D:\\' + ('a' * 238))",
        "@(1..33 | ForEach-Object",
        "Assert-Rejected ('x' * 513) $cachePath",
        "New-SyntheticContainer $cachePath -Type 'volume'",
        "New-SyntheticContainer $cachePath -ReadWrite $false",
        "attested bind mount projection: PASS",
    )
    assert all(item in source for item in required)


def test_capability_contract_accepts_only_two_complete_exact_spellings() -> None:
    accepted = (
        ("CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"),
        ("CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_SETGID", "CAP_SETUID"),
        ("SETUID", "CHOWN", "SETGID", "DAC_OVERRIDE"),
        ("CAP_SETUID", "CAP_CHOWN", "CAP_SETGID", "CAP_DAC_OVERRIDE"),
    )
    rejected = (
        ("CAP_CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"),
        ("CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID", "SYS_ADMIN"),
        ("CHOWN", "DAC_OVERRIDE", "SETGID"),
        ("chown", "DAC_OVERRIDE", "SETGID", "SETUID"),
        ("cap_chown", "CAP_DAC_OVERRIDE", "CAP_SETGID", "CAP_SETUID"),
        ("CAP_CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_SETGID", "CAP_SETUID"),
        ("CHOWN", "DAC_OVERRIDE", "SETGID", "SETGID"),
        ("CHOWN", "DAC_OVERRIDE", "SETGID", None),
        ("CHOWN", "DAC_OVERRIDE", "SETGID", 7),
        (),
        ("X" * 33, "DAC_OVERRIDE", "SETGID", "SETUID"),
    )

    assert all(_reference_capability_set_allowed(value) for value in accepted)
    assert not any(_reference_capability_set_allowed(value) for value in rejected)


@pytest.mark.parametrize(
    ("old", "mutation"),
    (
        ("$values.Count -ne 4", "$values.Count -ne 5"),
        ("[StringComparer]::Ordinal", "[StringComparer]::OrdinalIgnoreCase"),
        (
            "$value.Length -lt 1 -or $value.Length -gt 32 -or",
            "$value.Length -lt 1 -or $value.Length -gt 320 -or",
        ),
        ("-not $actual.Add($value)", "-not $actual.Contains($value)"),
        ("'CAP_CHOWN'", "'CHOWN'"),
        ("return ($composeMatch -or $dockerRuntimeMatch)", "return $composeMatch"),
        (
            "Test-ExactAttestedProxyCapabilitySet -Observed @($Proxy.HostConfig.CapAdd)",
            "[string]::Join(',', @($Proxy.HostConfig.CapAdd | Sort-Object)) -ceq "
            "'CHOWN,DAC_OVERRIDE,SETGID,SETUID'",
        ),
    ),
)
def test_static_gate_kills_capability_projection_contract_mutations(
    old: str,
    mutation: str,
) -> None:
    source = _COMMON.read_text(encoding="utf-8")
    _assert_static_capability_contract(source)
    assert old in source

    with pytest.raises(AssertionError):
        _assert_static_capability_contract(source.replace(old, mutation, 1))


def test_powershell_capability_matrix_covers_runtime_spellings_and_near_misses() -> None:
    source = _CAPABILITY_TEST.read_text(encoding="utf-8")
    required = (
        "Assert-Accepted $composeSpelling",
        "Assert-Accepted $dockerRuntimeSpelling",
        "Assert-Accepted @('CAP_SETUID', 'CAP_CHOWN', 'CAP_SETGID', 'CAP_DAC_OVERRIDE')",
        "Assert-Rejected @('CAP_CHOWN', 'DAC_OVERRIDE', 'SETGID', 'SETUID')",
        "'SYS_ADMIN'",
        "Assert-Rejected @('CHOWN', 'DAC_OVERRIDE', 'SETGID')",
        "'chown'",
        "'cap_chown'",
        "'CAP_CAP_CHOWN'",
        "Assert-Rejected @('CHOWN', 'DAC_OVERRIDE', 'SETGID', 'SETGID')",
        "Assert-Rejected @('CHOWN', 'DAC_OVERRIDE', 'SETGID', $null)",
        "Assert-Rejected @('CHOWN', 'DAC_OVERRIDE', 'SETGID', 7)",
        "('X' * 33)",
        "attested proxy capability projection: PASS",
    )
    assert all(item in source for item in required)


@pytest.mark.parametrize(
    ("function_name", "old", "mutation"),
    (
        ("Test-SolePublisherObservation", "$publishers.Count -eq 0", "$publishers.Count -le 1"),
        (
            "Test-SolePublisherObservation",
            "[string]$publishers[0] -ceq $ExpectedName",
            "[string]$publishers[0] -like $ExpectedName",
        ),
        ("Wait-SolePublisher", "$TimeoutSeconds -gt 120", "$TimeoutSeconds -gt 999"),
        ("Wait-SolePublisher", "if ($LASTEXITCODE -ne 0)", "if ($LASTEXITCODE -eq 999)"),
        ("Wait-SolePublisher", "Start-Sleep -Milliseconds 250", "Start-Sleep -Seconds 25"),
    ),
)
def test_static_gate_kills_publisher_wait_mutations(
    function_name: str,
    old: str,
    mutation: str,
) -> None:
    common = _COMMON.read_text(encoding="utf-8")
    switch = _SWITCH.read_text(encoding="utf-8")
    _assert_static_publisher_contract(common, switch)
    function = _powershell_function(common, function_name)
    assert old in function
    mutated = common.replace(function, function.replace(old, mutation, 1), 1)

    with pytest.raises(AssertionError):
        _assert_static_publisher_contract(mutated, switch)


def test_publisher_wait_is_used_only_at_fresh_candidate_publication_edges() -> None:
    common = _COMMON.read_text(encoding="utf-8")
    switch = _SWITCH.read_text(encoding="utf-8")
    _assert_static_publisher_contract(common, switch)

    initial = """\
    Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash
    Wait-SolePublisher $script:Attested.CandidateProxyName 120

    $stage = 'proxy_negative_paths'
"""
    restarted = """\
    Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash
    Wait-SolePublisher $script:Attested.CandidateProxyName 120
    Assert-ProxyNegativePaths $headers
"""
    assert initial in switch
    assert restarted in switch


def test_powershell_publisher_matrix_covers_pending_and_unsafe_sets() -> None:
    source = _PUBLISHER_TEST.read_text(encoding="utf-8")
    required = (
        "Test-SolePublisherObservation @($candidate) $candidate",
        "Test-SolePublisherObservation @() $candidate",
        "Assert-Rejected @($script:Attested.StableProxyName)",
        "Assert-Rejected @($candidate, $script:Attested.StableProxyName)",
        "Assert-Rejected @($candidate, $candidate)",
        "jarvis-gpt-sglang-qwen38-v12-attested-api-near",
        "Assert-Rejected @(7)",
        "Test-SolePublisherObservation @('unowned') 'unowned'",
        "attested publisher observation: PASS",
    )
    assert all(item in source for item in required)


@pytest.mark.parametrize(
    ("manifest_name", "required_names"),
    (
        (
            "ORCHESTRATION-SHA256SUMS",
            {
                "AttestedBundle.Common.ps1",
                "Switch-Qwen38V12Attested.ps1",
                "Rollback-Qwen38V12Attested.ps1",
                "Preflight-Qwen38V12Attested.ps1",
                "Test-AttestedBindMountProjection.ps1",
                "Test-AttestedCapabilityProjection.ps1",
                "Test-AttestedPublisherObservation.ps1",
                "ORCHESTRATION.md",
                "CORE-SHA256SUMS",
            },
        ),
        ("CORE-SHA256SUMS", set()),
    ),
)
def test_attested_bundle_sha256_manifests_match_exact_files(
    manifest_name: str,
    required_names: set[str],
) -> None:
    rows = (_REMOTE / manifest_name).read_text(encoding="ascii").splitlines()
    observed_names: set[str] = set()
    for row in rows:
        digest, separator, name = row.partition("  ")
        assert separator == "  "
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert name not in observed_names
        observed_names.add(name)
        assert hashlib.sha256((_REMOTE / name).read_bytes()).hexdigest() == digest
    if required_names:
        assert observed_names == required_names
