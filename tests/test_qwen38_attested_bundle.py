"""Static and mutation gates for the canonical Qwen3.8 Windows bundle."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml

_HANDOFF = Path(__file__).resolve().parents[1] / "handoffs" / "SGLang-Qwen38-V12-Attested"
_REMOTE = _HANDOFF / "remote"
_TRANSPORT = _HANDOFF / "transport"
_COMMON = _REMOTE / "AttestedBundle.Common.ps1"
_POWERSHELL_TEST = _REMOTE / "Test-AttestedBindMountProjection.ps1"
_CAPABILITY_TEST = _REMOTE / "Test-AttestedCapabilityProjection.ps1"
_PUBLISHER_TEST = _REMOTE / "Test-AttestedPublisherObservation.ps1"
_NETWORK_TEST = _REMOTE / "Test-AttestedNetworkProjection.ps1"
_CLEANUP_TEST = _REMOTE / "Test-AttestedCleanupProjection.ps1"
_RECEIPT_TEST = _REMOTE / "Test-AttestedReceiptSerialization.ps1"
_SWITCH = _REMOTE / "Switch-Qwen38V12Attested.ps1"
_ROLLBACK = _REMOTE / "Rollback-Qwen38V12Attested.ps1"
_CLEANUP = _REMOTE / "Cleanup-StoppedQwen38V12Attested.ps1"
_BASE_COMPOSE = _REMOTE / "docker-compose.attested.yml"
_PUBLISH_COMPOSE = _REMOTE / "docker-compose.publish-8001.yml"
_SYNC = _HANDOFF / "Sync-Qwen38V12AttestedBundle.sh"
_TRANSPORT_APPLIER = _TRANSPORT / "Apply-Qwen38V12AttestedBundle.ps1"
_TRANSPORT_MANIFEST = _TRANSPORT / "TRANSPORT-FILES.v1"
_TRANSPORT_TEST = _TRANSPORT / "Test-AttestedBundleTransportProjection.ps1"
_REPLACE_TEST = _TRANSPORT / "Test-WindowsPowerShell51FileReplace.ps1"
_WINDOWS_PATH = re.compile(r"(?P<drive>[A-Z]):\\(?P<tail>[A-Za-z0-9._-]+(?:\\[A-Za-z0-9._-]+)*)\Z")


def _powershell_function(source: str, name: str) -> str:
    start = source.index(f"function {name}")
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


def _assert_static_network_contract(common: str, switch: str) -> None:
    base = _powershell_function(common, "Assert-NetworkBaseIdentity")
    publish = _powershell_function(common, "Assert-PublishNetworkIdentity")
    provision = _powershell_function(common, "Ensure-PublishNetwork")
    receipt = _powershell_function(common, "Get-PublishNetworkReceipt")
    topology = _powershell_function(common, "Assert-CandidateNetworkTopology")
    candidate = _powershell_function(common, "Assert-CandidateContainers")
    publication = _powershell_function(common, "Assert-CandidateProxyPortPublication")
    rendered = _powershell_function(switch, "Assert-PublishedComposeConfig")
    state = _powershell_function(switch, "Save-State")

    assert "[string]$Network.Driver -cne 'bridge'" in base
    assert "[string]$Network.Scope -cne 'local'" in base
    assert "-not [bool]$Network.EnableIPv4 -or [bool]$Network.EnableIPv6" in base
    assert "[bool]$Network.Internal -ne $Internal" in base
    assert "[bool]$Network.Attachable" in base
    assert "[bool]$Network.Ingress" in base
    assert "[bool]$Network.ConfigOnly" in base
    assert "'com.docker.network.enable_ipv4', 'com.docker.network.enable_ipv6'" in base
    assert "$script:Attested.PublishNetworkName $false" in publish
    assert "Assert-ExactProperties $Network.Labels @($expected.Keys)" in publish

    assert "'network', 'create', '--driver', 'bridge', '--ipv4=true', '--ipv6=false'" in provision
    assert "'--attachable=false', '--internal=false'" in provision
    assert "Assert-NetworkContainerProjection $network @() @()" in provision
    assert "return [pscustomobject][ordered]@{" in receipt
    assert "labels = [pscustomobject](Get-ExpectedPublishNetworkLabels)" in receipt
    assert "docker network rm" not in common
    assert "docker network disconnect" not in common

    assert "$engineNetworks.Count -ne 1" in topology
    assert "$proxyNetworks.Count -ne 2" in topology
    assert "$script:Attested.AttestedNetworkName" in topology
    assert "$script:Attested.PublishNetworkName" in topology
    assert "Assert-ContainerNetworkEndpoint $Engine" in topology
    assert "Assert-ContainerNetworkEndpoint $Proxy" in topology
    assert "([string]$PublishNetwork.Id) 1" in topology
    assert "Assert-NetworkContainerProjection $PublishNetwork" in topology
    assert "Assert-PublishNetworkReceipt $PublishNetworkReceipt $publishNetwork" in candidate

    assert "Assert-ExactProxyPortBindingMap $Proxy.NetworkSettings.Ports" in publication
    assert "$script:Attested.PublishNetworkName" in publication
    assert "-cnotmatch '^(?:[0-9]{1,3}\\.){3}[0-9]{1,3}$'" in publication

    assert "[string]::Join(',', $engineNetworks) -cne 'attested'" in rendered
    assert "$null -ne $engineAttestedEndpoint" in rendered
    assert "[string]::Join(',', $proxyNetworks) -cne 'attested,publish'" in rendered
    assert "$attestedEndpointProperties.Count -ne 0" in rendered
    assert "[string]::Join(',', $publishEndpointProperties) -cne 'gw_priority'" in rendered
    assert "[int]$publishEndpoint.gw_priority -ne 1" in rendered
    assert "Assert-ExactProperties $ports[0]" in rendered
    assert "[string]$ports[0].mode -cne 'ingress'" in rendered
    assert "[string]$ports[0].host_ip -cne '0.0.0.0'" in rendered
    assert "publish_network = $publishNetworkReceipt" in state
    assert "friday.attested-switch-state.v2" in state


def _assert_static_cleanup_contract(common: str, cleanup: str) -> None:
    state = _powershell_function(common, "Assert-CandidateCleanupState")
    remove = _powershell_function(common, "Remove-ExactStoppedContainer")
    final_network = _powershell_function(common, "Get-CleanupFinalPublishNetworkReceipt")

    assert state.count("friday.attested-switch-state.v1") == 1
    assert state.count("friday.attested-switch-state.v2") == 2
    assert "Assert-ExactProperties $State $properties" in state
    assert "[string]$State.candidate_engine_image_id -cne [string]$Receipt.engine.image_id" in state
    assert "[string]$State.candidate_proxy_image_id -cne [string]$Receipt.proxy.image_id" in state
    assert "Assert-PublishNetworkReceipt $State.publish_network $null" in state

    assert "[string]$Container.Id -cne $ExpectedId" in remove
    assert "[bool]$Container.State.Running" in remove
    assert "(Get-RestartSpec $Container) -cne 'no'" in remove
    assert "docker container rm $ExpectedId" in remove
    assert "--force" not in remove
    assert "--volumes" not in remove

    assert "if ($StateSchema -ceq 'friday.attested-switch-state.v2')" in final_network
    assert "Assert-PublishNetworkReceipt $SealedReceipt $PublishNetwork" in final_network
    assert "return $SealedReceipt" in final_network
    assert "return Get-PublishNetworkReceipt $PublishNetwork" in final_network

    assert "Assert-CandidateCleanupState $state $receipt" in cleanup
    assert (
        "Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash -LegacyInternalOnly"
        in cleanup
    )
    assert "Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash `" in cleanup
    assert "$publishNetworkReceipt" in cleanup
    assert cleanup.index("Remove-ExactStoppedContainer $candidateProxy") < cleanup.index(
        "Remove-ExactStoppedContainer $candidateEngine"
    )
    assert "Assert-NetworkContainerProjection $attestedNetwork @() @()" in cleanup
    assert "Assert-NetworkContainerProjection $publishNetwork @() @()" in cleanup
    v1_provision = (
        "if ($stateSchema -ceq 'friday.attested-switch-state.v1') {\n"
        "        $null = Ensure-PublishNetwork\n"
        "    }"
    )
    assert cleanup.count("Ensure-PublishNetwork") == 1
    assert v1_provision in cleanup
    assert "$state.publish_network" in cleanup
    assert (
        "Get-CleanupFinalPublishNetworkReceipt $stateSchema `\n"
        "        $sealedPublishNetworkReceipt $publishNetwork" in cleanup
    )
    for forbidden in (
        "docker network rm",
        "docker network disconnect",
        "docker compose down",
        "docker volume rm",
        "docker image rm",
    ):
        assert forbidden not in cleanup


def _assert_receipt_json_depth(source: str, expected_count: int) -> None:
    serializers = re.findall(r"ConvertTo-Json -Compress(?: -Depth [0-9]+)?", source)
    assert serializers == ["ConvertTo-Json -Compress -Depth 12"] * expected_count


def _assert_static_transport_applier(source: str) -> None:
    nullable = _powershell_function(source, "Test-ExactNullableSha256")
    backup_path = _powershell_function(source, "Get-ExactBackupPath")
    assert "$null -eq $Left -or $null -eq $Right" in nullable
    assert "return ($null -eq $Left -and $null -eq $Right)" in nullable
    assert "return ([string]$Left -ceq [string]$Right)" in nullable
    assert "$expectedRoles.Contains($Name)" in backup_path
    assert "'.{0}.friday-attested-sync-v1.backup' -f $Name" in backup_path
    assert source.count("Test-ExactNullableSha256 $actual $projection.before_sha256") == 1
    assert source.count("Test-ExactNullableSha256 $currentHash $projection.old_sha256") == 2
    assert "Test-ExactNullableSha256 `\n            $actualBackup" in source

    order_match = re.search(
        r"\$fullPublicationOrder = @\((.*?)\n\)",
        source,
        flags=re.DOTALL,
    )
    assert order_match is not None
    publication_order = re.findall(r"'([A-Za-z0-9._-]+)'", order_match.group(1))
    assert publication_order == [
        "docker-compose.publish-8001.yml",
        "AttestedBundle.Common.ps1",
        "Cleanup-StoppedQwen38V12Attested.ps1",
        "ORCHESTRATION.md",
        "README.md",
        "Rollback-Qwen38V12Attested.ps1",
        "Switch-Qwen38V12Attested.ps1",
        "Test-AttestedCleanupProjection.ps1",
        "Test-AttestedNetworkProjection.ps1",
        "Test-AttestedReceiptSerialization.ps1",
        "CORE-SHA256SUMS",
        "ORCHESTRATION-SHA256SUMS",
    ]
    assert "foreach ($name in $publicationOrder)" in source
    assert "[IO.File]::Replace($temporaryPath, $targetPath, $backupPath, $true)" in source
    assert "[IO.File]::Replace($temporaryPath, $targetPath, $null, $true)" not in source
    assert "[IO.File]::Move($temporaryPath, $targetPath)" in source
    assert source.count("Remove-Item -LiteralPath $backupPath -Force") == 2
    assert 'Get-ExactSha256 $backupPath "replaced backup $($projection.name)"' in source
    assert "Old live target has unexpected backup residue" in source
    assert "Converged live target has unsafe backup residue" in source
    assert "Final live target retained backup residue" in source
    assert "function Assert-NoSyncTemporaryResidue" in source
    assert "[0-9a-f]{32}\\.tmp$" in source
    assert source.count("Assert-NoSyncTemporaryResidue $liveRoot") == 3
    assert "Live root contains sync temporary residue" in source
    assert "'remove_backup'" in source
    assert "$replaceTestName = 'Test-WindowsPowerShell51FileReplace.ps1'" in source
    assert "@($applierName, $manifestName, $replaceTestName)" in source
    assert "[IO.FileMode]::Open" in source
    assert "[IO.FileShare]::None" in source
    assert "ConvertTo-Json -Compress -Depth 12" in source
    for forbidden in (
        "docker ",
        "Remove-Item -LiteralPath $targetPath",
        "rollback-state-attested.json",
        "switch-attested-",
    ):
        assert forbidden not in source


def _reference_transport_cas_action(
    old_hash: str | None,
    live_hash: str | None,
    new_hash: str,
    backup_hash: str | None = None,
) -> str:
    if live_hash == new_hash:
        if backup_hash is None:
            return "retain"
        if old_hash is not None and backup_hash == old_hash:
            return "remove_backup"
        return "reject"
    if live_hash == old_hash and backup_hash is None:
        return "replace"
    return "reject"


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

    initial_start = switch.index("    $stage = 'candidate_proxy_start'")
    initial_end = switch.index("    $stage = 'proxy_negative_paths'", initial_start)
    initial = switch[initial_start:initial_end]
    assert initial.count("Wait-SolePublisher $script:Attested.CandidateProxyName 120") == 1
    assert "Assert-CandidateProxyPortPublication $candidateProxy" in initial

    restart_start = switch.index("    $stage = 'epoch_restart_health'")
    restart_end = switch.index("    $stage = 'epoch_rotation_proof'", restart_start)
    restarted = switch[restart_start:restart_end]
    assert restarted.count("Wait-SolePublisher $script:Attested.CandidateProxyName 120") == 1
    assert "Assert-CandidateProxyPortPublication $candidateProxy" in restarted
    assert "Assert-ProxyNegativePaths $headers" in restarted


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


def _reference_network_topology_allowed(
    *,
    engine_networks: tuple[str, ...] = ("attested",),
    proxy_networks: tuple[str, ...] = ("attested", "publish"),
    attested_internal: bool = True,
    publish_internal: bool = False,
    attested_driver: str = "bridge",
    publish_driver: str = "bridge",
    engine_attested_id: str = "internal-id",
    proxy_attested_id: str = "internal-id",
    proxy_publish_id: str = "publish-id",
    publish_gw_priority: int = 1,
) -> bool:
    return (
        engine_networks == ("attested",)
        and tuple(sorted(proxy_networks)) == ("attested", "publish")
        and attested_internal
        and not publish_internal
        and attested_driver == "bridge"
        and publish_driver == "bridge"
        and engine_attested_id == "internal-id"
        and proxy_attested_id == "internal-id"
        and proxy_publish_id == "publish-id"
        and publish_gw_priority == 1
    )


def test_network_topology_reference_rejects_missing_extra_swapped_and_engine_ingress() -> None:
    assert _reference_network_topology_allowed()
    mutations = (
        {"proxy_networks": ("attested",)},
        {"proxy_networks": ("attested", "foreign", "publish")},
        {"proxy_attested_id": "publish-id", "proxy_publish_id": "internal-id"},
        {"attested_internal": False},
        {"publish_internal": True},
        {"engine_networks": ("attested", "publish")},
        {"publish_gw_priority": 0},
        {"attested_driver": "overlay"},
        {"publish_driver": "overlay"},
    )
    assert not any(_reference_network_topology_allowed(**mutation) for mutation in mutations)


def test_compose_overlay_adds_host_gateway_only_to_proxy() -> None:
    base = yaml.safe_load(_BASE_COMPOSE.read_text(encoding="utf-8"))
    publish = yaml.safe_load(_PUBLISH_COMPOSE.read_text(encoding="utf-8"))

    assert base["services"]["engine"]["networks"] == ["attested"]
    assert base["services"]["proxy"]["networks"] == ["attested"]
    assert base["networks"] == {
        "attested": {
            "name": "jarvis-gpt-qwen38-v12-attested-net",
            "internal": True,
        }
    }
    assert publish == {
        "services": {
            "proxy": {
                "ports": ["${JARVIS_OPENAI_BIND_ADDRESS:-0.0.0.0}:8001:8080"],
                "networks": {
                    "attested": {"gw_priority": 0},
                    "publish": {"gw_priority": 1},
                },
            }
        },
        "networks": {
            "publish": {
                "name": "jarvis-gpt-qwen38-v12-attested-publish-net",
                "external": True,
            }
        },
    }


@pytest.mark.parametrize(
    ("function_name", "old", "mutation"),
    (
        ("Assert-NetworkBaseIdentity", "[string]$Network.Driver -cne 'bridge'", "$false"),
        ("Assert-NetworkBaseIdentity", "[bool]$Network.Internal -ne $Internal", "$false"),
        ("Assert-PublishNetworkIdentity", "$script:Attested.PublishNetworkName $false", "$null $false"),
        ("Ensure-PublishNetwork", "'--attachable=false', '--internal=false'", "'--attachable=true'"),
        (
            "Assert-CandidateContainers",
            "Assert-PublishNetworkReceipt $PublishNetworkReceipt $publishNetwork",
            "$null = $PublishNetworkReceipt",
        ),
        ("Assert-CandidateNetworkTopology", "$engineNetworks.Count -ne 1", "$engineNetworks.Count -lt 1"),
        ("Assert-CandidateNetworkTopology", "$proxyNetworks.Count -ne 2", "$proxyNetworks.Count -lt 2"),
        (
            "Assert-CandidateNetworkTopology",
            "([string]$PublishNetwork.Id) 1",
            "([string]$PublishNetwork.Id) 0",
        ),
        (
            "Assert-CandidateProxyPortPublication",
            "Assert-ExactProxyPortBindingMap $Proxy.NetworkSettings.Ports",
            "$null = $Proxy.NetworkSettings.Ports",
        ),
    ),
)
def test_static_gate_kills_network_contract_mutations(
    function_name: str,
    old: str,
    mutation: str,
) -> None:
    common = _COMMON.read_text(encoding="utf-8")
    switch = _SWITCH.read_text(encoding="utf-8")
    _assert_static_network_contract(common, switch)
    function = _powershell_function(common, function_name)
    assert old in function
    mutated = common.replace(function, function.replace(old, mutation, 1), 1)

    with pytest.raises(AssertionError):
        _assert_static_network_contract(mutated, switch)


@pytest.mark.parametrize(
    ("old", "mutation"),
    (
        ("[string]::Join(',', $engineNetworks) -cne 'attested'", "$false"),
        ("$null -ne $engineAttestedEndpoint", "$false"),
        ("[string]::Join(',', $proxyNetworks) -cne 'attested,publish'", "$false"),
        ("$attestedEndpointProperties.Count -ne 0", "$false"),
        ("[int]$publishEndpoint.gw_priority -ne 1", "[int]$publishEndpoint.gw_priority -ne 0"),
        ("[string]$ports[0].host_ip -cne '0.0.0.0'", "$false"),
    ),
)
def test_static_gate_kills_published_compose_topology_mutations(old: str, mutation: str) -> None:
    common = _COMMON.read_text(encoding="utf-8")
    switch = _SWITCH.read_text(encoding="utf-8")
    _assert_static_network_contract(common, switch)
    function = _powershell_function(switch, "Assert-PublishedComposeConfig")
    assert old in function
    mutated = switch.replace(function, function.replace(old, mutation, 1), 1)

    with pytest.raises(AssertionError):
        _assert_static_network_contract(common, mutated)


def test_powershell_network_matrix_covers_required_negative_topologies() -> None:
    source = _NETWORK_TEST.read_text(encoding="utf-8")
    required = (
        "$exact = New-ExactTopology",
        "Assert-Topology $exact",
        "Get-PublishNetworkReceipt $exact.Publish",
        "Assert-PublishNetworkReceipt $publishReceipt $exact.Publish",
        "'missing publish network'",
        "'extra proxy network'",
        "'swapped network identities'",
        "'attested network is not internal'",
        "'publish network became internal'",
        "'engine attached to publish ingress'",
        "'publish gateway priority lost'",
        "'publish network driver changed'",
        "'publish ownership label changed'",
        "'foreign publish attachment'",
        "attested network topology projection: PASS",
    )
    assert all(item in source for item in required)


def test_cleanup_is_explicit_identity_bound_and_preserves_networks() -> None:
    common = _COMMON.read_text(encoding="utf-8")
    cleanup = _CLEANUP.read_text(encoding="utf-8")
    _assert_static_cleanup_contract(common, cleanup)
    assert "[switch]$Execute" in cleanup
    assert "if (-not $Execute)" in cleanup
    assert "mutation_authorized = $false" in cleanup
    assert "publish_network_permanent = $true" in cleanup
    assert "removed_only_bound_candidate_ids = $true" in cleanup


@pytest.mark.parametrize(
    ("old", "mutation"),
    (
        (
            "if ($stateSchema -ceq 'friday.attested-switch-state.v1') {\n"
            "        $null = Ensure-PublishNetwork\n"
            "    }",
            "if ($stateSchema -ceq 'friday.attested-switch-state.v2') {\n"
            "        $null = Ensure-PublishNetwork\n"
            "    }",
        ),
        (
            "$sealedPublishNetworkReceipt $publishNetwork",
            "$null $publishNetwork",
        ),
    ),
)
def test_static_gate_kills_cleanup_final_network_flow_mutations(
    old: str,
    mutation: str,
) -> None:
    common = _COMMON.read_text(encoding="utf-8")
    cleanup = _CLEANUP.read_text(encoding="utf-8")
    _assert_static_cleanup_contract(common, cleanup)
    assert old in cleanup
    mutated = cleanup.replace(old, mutation, 1)

    with pytest.raises(AssertionError):
        _assert_static_cleanup_contract(common, mutated)


@pytest.mark.parametrize(
    ("function_name", "old", "mutation"),
    (
        (
            "Assert-CandidateCleanupState",
            "friday.attested-switch-state.v1",
            "friday.attested-switch-state.v0",
        ),
        (
            "Assert-CandidateCleanupState",
            "friday.attested-switch-state.v2",
            "friday.attested-switch-state.v3",
        ),
        (
            "Remove-ExactStoppedContainer",
            "[string]$Container.Id -cne $ExpectedId",
            "[string]$Container.Id -like $ExpectedId",
        ),
        (
            "Remove-ExactStoppedContainer",
            "[bool]$Container.State.Running",
            "$false",
        ),
        (
            "Remove-ExactStoppedContainer",
            "(Get-RestartSpec $Container) -cne 'no'",
            "$false",
        ),
        (
            "Get-CleanupFinalPublishNetworkReceipt",
            "Assert-PublishNetworkReceipt $SealedReceipt $PublishNetwork",
            "$null = $SealedReceipt",
        ),
    ),
)
def test_static_gate_kills_cleanup_identity_mutations(
    function_name: str,
    old: str,
    mutation: str,
) -> None:
    common = _COMMON.read_text(encoding="utf-8")
    cleanup = _CLEANUP.read_text(encoding="utf-8")
    _assert_static_cleanup_contract(common, cleanup)
    function = _powershell_function(common, function_name)
    assert old in function
    mutated = common.replace(function, function.replace(old, mutation, 1), 1)

    with pytest.raises(AssertionError):
        _assert_static_cleanup_contract(mutated, cleanup)


def test_powershell_cleanup_matrix_covers_v1_v2_and_unsafe_state_mutations() -> None:
    source = _CLEANUP_TEST.read_text(encoding="utf-8")
    required = (
        "New-State 'v1'",
        "New-State 'v2'",
        "'unknown state schema'",
        "'extra state property'",
        "'unbound candidate ID'",
        "'no candidate IDs'",
        "'wrong candidate image'",
        "'internal publish receipt'",
        "'wrong publish ownership label'",
        "'post-removal different live network ID'",
        "'Final v2 cleanup did not preserve its sealed publish network receipt'",
        "'Final v1 cleanup did not adopt the exact live publish network'",
        "attested stopped-candidate cleanup projection: PASS",
    )
    assert all(item in source for item in required)


@pytest.mark.parametrize(
    ("path", "expected_count"),
    ((_SWITCH, 3), (_ROLLBACK, 4)),
)
def test_receipt_bearing_json_is_explicit_depth_12(
    path: Path,
    expected_count: int,
) -> None:
    source = path.read_text(encoding="utf-8")
    _assert_receipt_json_depth(source, expected_count)
    mutated = source.replace("ConvertTo-Json -Compress -Depth 12", "ConvertTo-Json -Compress", 1)
    with pytest.raises(AssertionError):
        _assert_receipt_json_depth(mutated, expected_count)


def test_powershell_receipt_serialization_test_is_ps51_compatible_and_exact() -> None:
    source = _RECEIPT_TEST.read_text(encoding="utf-8")
    required = (
        "ConvertTo-Json -Compress -Depth 12",
        "ConvertFrom-Json",
        "Assert-PublishNetworkReceipt $parsed.output.publish_network $null",
        "System.Management.Automation.Language.Parser",
        "'Switch-Qwen38V12Attested.ps1' = 3",
        "'Rollback-Qwen38V12Attested.ps1' = 4",
        "attested receipt depth-12 serialization: PASS",
    )
    assert all(item in source for item in required)


def test_transport_manifest_pins_exact_c560_predecessors_and_frozen_sources() -> None:
    old_hashes = {
        "AttestedBundle.Common.ps1": ("6a4e16654505decdd6599e95127e65e7dd50f55727d3eea296b0a5b4b38f6b0e"),
        "Cleanup-StoppedQwen38V12Attested.ps1": None,
        "docker-compose.publish-8001.yml": (
            "9403b256555fd105f3c17395dd1049fac894e140891ef8ff9ccb86767934fcae"
        ),
        "CORE-SHA256SUMS": ("844e65a1c3e1c727fe85819ae37589535698018dab051e55ba26766a8fed121a"),
        "ORCHESTRATION-SHA256SUMS": ("645079759508610eac6e9213d61d6c891e3521496894f2e3d7dbac05765d26e5"),
        "ORCHESTRATION.md": ("9e5a5d8d7953004cec1caa484784721bc7d1b834317d2f32d11abcd5966f850b"),
        "README.md": "9e976a0382b5f1c93cdc0f61b5fd671302934d3ca0da5f6cb9537e2abfc695b4",
        "Rollback-Qwen38V12Attested.ps1": (
            "812b8a2a8586d1c52053f6ab3580da645d9b201f31c612d41d631ff8abcd3962"
        ),
        "Switch-Qwen38V12Attested.ps1": ("02e441970c6c91b503668ca930d5f112521c6787a27f6de10d523f6bb7a1e690"),
        "Test-AttestedCleanupProjection.ps1": None,
        "Test-AttestedNetworkProjection.ps1": None,
        "Test-AttestedReceiptSerialization.ps1": None,
    }
    expected_roles = {
        name: (
            "bootstrap"
            if name
            in {
                "AttestedBundle.Common.ps1",
                "Cleanup-StoppedQwen38V12Attested.ps1",
                "docker-compose.publish-8001.yml",
            }
            else "full"
        )
        for name in old_hashes
    }
    observed: dict[str, tuple[str, str | None, str]] = {}
    for row in _TRANSPORT_MANIFEST.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(
            r"(bootstrap|full) ([0-9a-f]{64}|ABSENT) ([0-9a-f]{64}) "
            r"([A-Za-z0-9._-]+)",
            row,
        )
        assert match is not None
        role, old_value, new_value, name = match.groups()
        assert name not in observed
        observed[name] = (role, None if old_value == "ABSENT" else old_value, new_value)
        assert hashlib.sha256((_REMOTE / name).read_bytes()).hexdigest() == new_value
    assert set(observed) == set(old_hashes)
    for name, expected_old in old_hashes.items():
        assert observed[name][:2] == (expected_roles[name], expected_old)


def test_transport_wrapper_pins_verified_ssh_and_transport_identities() -> None:
    source = _SYNC.read_text(encoding="utf-8")
    required = (
        "remote_host='192.168.1.78'",
        "remote_user='admin'",
        "ssh_key='/home/jericho/.ssh/friday_win_audit_ed25519'",
        "known_hosts='/home/jericho/.ssh/known_hosts'",
        "SHA256:vhJUpURIJLODWZdo8LU8qnTMbLir86/J5tzl8VWp5+A",
        "SHA256:wfOf57TOtNhTuQ6OAQUcWhMF47C8FWeUhku2gSAe6mY",
        "-F /dev/null",
        "-o BatchMode=yes",
        "-o ConnectTimeout=10",
        "-o StrictHostKeyChecking=yes",
        '-o UserKnownHostsFile="$known_hosts"',
        "-o GlobalKnownHostsFile=/dev/null",
        "-o HostKeyAlgorithms=ssh-ed25519",
        "-o UpdateHostKeys=no",
        "-o IdentitiesOnly=yes",
        "-o PasswordAuthentication=no",
        "-o KbdInteractiveAuthentication=no",
        "-o PreferredAuthentications=publickey",
        "-o ProxyCommand=none",
        "-o ProxyJump=none",
        "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand",
        r"\$stdinStream=[Console]::OpenStandardInput()",
        'effective_ssh=$(ssh -G "${ssh_args[@]}" "$remote_target" 2>/dev/null)',
        "assert_effective_ssh globalknownhostsfile /dev/null",
        "assert_effective_ssh hostkeyalgorithms ssh-ed25519",
        "assert_effective_ssh updatehostkeys false",
        "replace_test=$transport_dir/Test-WindowsPowerShell51FileReplace.ps1",
        "& \\$replaceTest -RequireWindowsPowerShell51",
    )
    assert all(value in source for value in required)
    assert "scp " not in source
    assert "-p 22" not in source
    plan_exit = source.index("if [[ $mode == plan ]]")
    assert source.index('effective_ssh=$(ssh -G "${ssh_args[@]}"') < plan_exit
    assert plan_exit < source.index('ssh "${ssh_args[@]}"')

    manifest_pin = re.search(r"expected_manifest_sha256='([0-9a-f]{64})'", source)
    applier_pin = re.search(r"expected_applier_sha256='([0-9a-f]{64})'", source)
    replace_test_pin = re.search(r"expected_replace_test_sha256='([0-9a-f]{64})'", source)
    assert manifest_pin is not None
    assert applier_pin is not None
    assert replace_test_pin is not None
    assert manifest_pin.group(1) == hashlib.sha256(_TRANSPORT_MANIFEST.read_bytes()).hexdigest()
    assert applier_pin.group(1) == hashlib.sha256(_TRANSPORT_APPLIER.read_bytes()).hexdigest()
    assert replace_test_pin.group(1) == hashlib.sha256(_REPLACE_TEST.read_bytes()).hexdigest()


def test_transport_applier_is_manifest_last_nullable_and_atomic() -> None:
    source = _TRANSPORT_APPLIER.read_text(encoding="utf-8")
    _assert_static_transport_applier(source)

    mutations = (
        (
            "Test-ExactNullableSha256 $actual $projection.before_sha256",
            "[string]$actual -ceq [string]$projection.before_sha256",
        ),
        (
            "'CORE-SHA256SUMS',\n    'ORCHESTRATION-SHA256SUMS'",
            "'ORCHESTRATION-SHA256SUMS',\n    'CORE-SHA256SUMS'",
        ),
        (
            "[IO.File]::Replace($temporaryPath, $targetPath, $backupPath, $true)",
            "[IO.File]::Copy($temporaryPath, $targetPath, $true)",
        ),
        (
            "Remove-Item -LiteralPath $backupPath -Force",
            "Remove-Item -LiteralPath $targetPath -Force",
        ),
    )
    for old, mutation in mutations:
        assert old in source
        with pytest.raises(AssertionError):
            _assert_static_transport_applier(source.replace(old, mutation, 1))


def test_transport_crash_prefixes_are_manifest_safe_and_resumable() -> None:
    publication_order = (
        "docker-compose.publish-8001.yml",
        "AttestedBundle.Common.ps1",
        "Cleanup-StoppedQwen38V12Attested.ps1",
        "ORCHESTRATION.md",
        "README.md",
        "Rollback-Qwen38V12Attested.ps1",
        "Switch-Qwen38V12Attested.ps1",
        "Test-AttestedCleanupProjection.ps1",
        "Test-AttestedNetworkProjection.ps1",
        "Test-AttestedReceiptSerialization.ps1",
        "CORE-SHA256SUMS",
        "ORCHESTRATION-SHA256SUMS",
    )
    old = "a" * 64
    new = "b" * 64
    assert _reference_transport_cas_action(None, None, new) == "replace"
    assert _reference_transport_cas_action(None, new, new) == "retain"
    assert _reference_transport_cas_action(old, "c" * 64, new) == "reject"
    assert _reference_transport_cas_action(old, new, new, old) == "remove_backup"
    assert _reference_transport_cas_action(old, old, new, old) == "reject"
    assert _reference_transport_cas_action(old, new, new, "c" * 64) == "reject"

    for prefix_length in range(len(publication_order) + 1):
        landed = set(publication_order[:prefix_length])
        if "CORE-SHA256SUMS" in landed:
            assert {"README.md", "docker-compose.publish-8001.yml"} <= landed
        if "ORCHESTRATION-SHA256SUMS" in landed:
            assert landed == set(publication_order)
        for name in publication_order:
            live = new if name in landed else old
            assert _reference_transport_cas_action(old, live, new) != "reject"


def test_native_transport_projection_covers_absent_retry_order_and_encoded_stdin() -> None:
    source = _TRANSPORT_TEST.read_text(encoding="utf-8")
    required = (
        "ABSENT to create CAS projection failed",
        "Created target idempotent retry projection failed",
        "Interrupted prefix published CORE before all changed CORE members",
        "Interrupted prefix published orchestration manifest before all payloads",
        "Interrupted publication prefix is not resumable",
        r"$wrapperSource.Contains('\$stdinStream=[Console]::OpenStandardInput()')",
        "-EncodedCommand",
        "$startInfo.RedirectStandardInput = $true",
        "$process.StandardInput.BaseStream.Write($inputBytes, 0, $inputBytes.Length)",
        "Native encoded-command stdin receiver changed bytes",
        "attested bundle transport projection: PASS",
    )
    assert all(value in source for value in required)


def test_native_windows_powershell_file_replace_gate_is_real_and_crash_safe() -> None:
    source = _REPLACE_TEST.read_text(encoding="utf-8")
    required = (
        "[switch]$RequireWindowsPowerShell51",
        "PSEdition -cne 'Desktop'",
        "PSVersion.Major -ne 5",
        "PSVersion.Minor -ne 1",
        "[IO.File]::Replace($sourcePath, $targetPath, $backupPath, $true)",
        "Native File.Replace did not preserve exact atomic evidence",
        "Native File.Replace crash residue was not resumable",
        "Native File.Replace idempotent retry did not converge",
        "old target plus backup",
        "new target plus wrong backup",
        "old target plus wrong backup",
        "existing_file_replaced = $true",
        "crash_retry_converged = $true",
    )
    assert all(value in source for value in required)
    assert "[IO.File]::Replace($sourcePath, $targetPath, $null, $true)" not in source


@pytest.mark.parametrize(
    ("manifest_name", "required_names"),
    (
        (
            "ORCHESTRATION-SHA256SUMS",
            {
                "AttestedBundle.Common.ps1",
                "Switch-Qwen38V12Attested.ps1",
                "Rollback-Qwen38V12Attested.ps1",
                "Cleanup-StoppedQwen38V12Attested.ps1",
                "Preflight-Qwen38V12Attested.ps1",
                "Test-AttestedBindMountProjection.ps1",
                "Test-AttestedCapabilityProjection.ps1",
                "Test-AttestedPublisherObservation.ps1",
                "Test-AttestedNetworkProjection.ps1",
                "Test-AttestedCleanupProjection.ps1",
                "Test-AttestedReceiptSerialization.ps1",
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
