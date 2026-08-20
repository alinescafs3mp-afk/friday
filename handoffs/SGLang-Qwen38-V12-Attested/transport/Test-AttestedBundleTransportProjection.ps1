$ErrorActionPreference = 'Stop'

$applierPath = Join-Path $PSScriptRoot 'Apply-Qwen38V12AttestedBundle.ps1'
$wrapperPath = Join-Path (Split-Path -Parent $PSScriptRoot) `
    'Sync-Qwen38V12AttestedBundle.sh'
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $applierPath,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) {
    throw 'Transport applier does not parse'
}
$applierSource = Get-Content -Raw -LiteralPath $applierPath -Encoding utf8
$wrapperSource = Get-Content -Raw -LiteralPath $wrapperPath -Encoding utf8

$requiredApplierSource = @(
    'function Test-ExactNullableSha256(',
    'Test-ExactNullableSha256 $actual $projection.before_sha256',
    'Test-ExactNullableSha256 $currentHash $projection.old_sha256',
    "'.{0}.friday-attested-sync-v1.backup' -f `$Name",
    '[IO.File]::Replace($temporaryPath, $targetPath, $backupPath, $true)',
    'Get-ExactSha256 $backupPath "replaced backup $($projection.name)"',
    'Remove-Item -LiteralPath $backupPath -Force',
    'function Assert-NoSyncTemporaryResidue',
    'Live root contains sync temporary residue',
    "'remove_backup'",
    '[IO.File]::Move($temporaryPath, $targetPath)',
    "'CORE-SHA256SUMS',",
    "'ORCHESTRATION-SHA256SUMS'"
)
foreach ($required in $requiredApplierSource) {
    if (-not $applierSource.Contains($required)) {
        throw "Transport applier contract is absent: $required"
    }
}
if (-not $wrapperSource.Contains('\$stdinStream=[Console]::OpenStandardInput()') -or
    -not $wrapperSource.Contains('\$stdinStream.CopyTo(\$output)') -or
    $wrapperSource.Contains('\$input=[Console]::OpenStandardInput()')) {
    throw 'Encoded-command receiver does not use the exact non-automatic stdin variable'
}
$requiredSshProjection = @(
    '-o GlobalKnownHostsFile=/dev/null',
    '-o HostKeyAlgorithms=ssh-ed25519',
    '-o UpdateHostKeys=no',
    'effective_ssh=$(ssh -G "${ssh_args[@]}" "$remote_target" 2>/dev/null)',
    'assert_effective_ssh globalknownhostsfile /dev/null',
    'assert_effective_ssh hostkeyalgorithms ssh-ed25519',
    'assert_effective_ssh updatehostkeys false',
    'replace_test=$transport_dir/Test-WindowsPowerShell51FileReplace.ps1',
    '& \$replaceTest -RequireWindowsPowerShell51'
)
foreach ($required in $requiredSshProjection) {
    if (-not $wrapperSource.Contains($required)) {
        throw "Effective SSH projection contract is absent: $required"
    }
}

function Test-ReferenceNullableSha256(
    [AllowNull()][object]$Left,
    [AllowNull()][object]$Right
) {
    if ($null -eq $Left -or $null -eq $Right) {
        return ($null -eq $Left -and $null -eq $Right)
    }
    return ([string]$Left -ceq [string]$Right)
}

function Get-ReferenceCasAction(
    [AllowNull()][object]$OldHash,
    [AllowNull()][object]$LiveHash,
    [string]$NewHash,
    [AllowNull()][object]$BackupHash = $null
) {
    if (Test-ReferenceNullableSha256 $LiveHash $NewHash) {
        if ($null -eq $BackupHash) { return 'retain' }
        if ($null -ne $OldHash -and [string]$BackupHash -ceq [string]$OldHash) {
            return 'remove_backup'
        }
        return 'reject'
    }
    if ((Test-ReferenceNullableSha256 $LiveHash $OldHash) -and
        $null -eq $BackupHash) { return 'replace' }
    return 'reject'
}

$old = 'a' * 64
$new = 'b' * 64
if ((Get-ReferenceCasAction $null $null $new) -cne 'replace') {
    throw 'ABSENT to create CAS projection failed'
}
if ((Get-ReferenceCasAction $null $new $new) -cne 'retain') {
    throw 'Created target idempotent retry projection failed'
}
if ((Get-ReferenceCasAction $old $old $new) -cne 'replace' -or
    (Get-ReferenceCasAction $old $new $new) -cne 'retain' -or
    (Get-ReferenceCasAction $old $new $new $old) -cne 'remove_backup' -or
    (Get-ReferenceCasAction $old $old $new $old) -cne 'reject' -or
    (Get-ReferenceCasAction $old $new $new ('c' * 64)) -cne 'reject' -or
    (Get-ReferenceCasAction $old ('c' * 64) $new) -cne 'reject') {
    throw 'Existing target CAS projection failed'
}

$publicationOrder = @(
    'docker-compose.publish-8001.yml',
    'AttestedBundle.Common.ps1',
    'Cleanup-StoppedQwen38V12Attested.ps1',
    'ORCHESTRATION.md',
    'README.md',
    'Rollback-Qwen38V12Attested.ps1',
    'Switch-Qwen38V12Attested.ps1',
    'Test-AttestedCleanupProjection.ps1',
    'Test-AttestedNetworkProjection.ps1',
    'Test-AttestedReceiptSerialization.ps1',
    'CORE-SHA256SUMS',
    'ORCHESTRATION-SHA256SUMS'
)
$sourcePositions = @($publicationOrder | ForEach-Object { $applierSource.IndexOf("'$_'") })
if ($sourcePositions -contains -1) {
    throw 'Publication order member is absent from applier source'
}
$fullOrderStart = $applierSource.IndexOf('$fullPublicationOrder = @(')
$fullOrderEnd = $applierSource.IndexOf("`n)", $fullOrderStart)
if ($fullOrderStart -lt 0 -or $fullOrderEnd -lt 0) {
    throw 'Full publication order block is absent'
}
$orderBlock = $applierSource.Substring($fullOrderStart, $fullOrderEnd - $fullOrderStart)
$lastPosition = -1
foreach ($name in $publicationOrder) {
    $position = $orderBlock.IndexOf("'$name'")
    if ($position -le $lastPosition) {
        throw 'Full publication order is not exact'
    }
    $lastPosition = $position
}

# Every possible interrupted prefix leaves either both old manifests, only a
# new CORE manifest whose changed members already landed, or the fully complete
# new orchestration manifest. Retrying each prefix accepts old/new targets.
for ($prefix = 0; $prefix -le $publicationOrder.Count; $prefix++) {
    $landed = @{}
    for ($index = 0; $index -lt $prefix; $index++) {
        $landed[$publicationOrder[$index]] = $true
    }
    if ($landed.ContainsKey('CORE-SHA256SUMS') -and
        (-not $landed.ContainsKey('README.md') -or
            -not $landed.ContainsKey('docker-compose.publish-8001.yml'))) {
        throw 'Interrupted prefix published CORE before all changed CORE members'
    }
    if ($landed.ContainsKey('ORCHESTRATION-SHA256SUMS') -and
        $landed.Count -ne $publicationOrder.Count) {
        throw 'Interrupted prefix published orchestration manifest before all payloads'
    }
    foreach ($name in $publicationOrder) {
        $live = $(if ($landed.ContainsKey($name)) { $new } else { $old })
        if ((Get-ReferenceCasAction $old $live $new) -ceq 'reject') {
            throw 'Interrupted publication prefix is not resumable'
        }
    }
}

$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) (
    'friday-attested-encoded-transport-' + [Guid]::NewGuid().ToString('N')
)
try {
    [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
    $inputPath = Join-Path $temporaryRoot 'input.bin'
    $outputPath = Join-Path $temporaryRoot 'output.bin'
    [IO.File]::WriteAllBytes($inputPath, [byte[]](0, 1, 2, 10, 13, 127, 128, 254, 255))
    $escapedOutputPath = $outputPath.Replace("'", "''")
    $receiver = @"
`$ErrorActionPreference='Stop'
`$stdinStream=[Console]::OpenStandardInput()
`$output=[IO.File]::Open('$escapedOutputPath',[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
try{`$stdinStream.CopyTo(`$output)}finally{`$output.Dispose()}
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($receiver))
    $enginePath = (Get-Process -Id $PID).Path
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $enginePath
    $startInfo.Arguments = "-NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded"
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw 'Native encoded-command process did not start' }
    $inputBytes = [IO.File]::ReadAllBytes($inputPath)
    $process.StandardInput.BaseStream.Write($inputBytes, 0, $inputBytes.Length)
    $process.StandardInput.BaseStream.Dispose()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw "Native encoded-command stdin receiver failed: $stdout $stderr"
    }
    $inputHash = (Get-FileHash -LiteralPath $inputPath -Algorithm SHA256).Hash
    $outputHash = (Get-FileHash -LiteralPath $outputPath -Algorithm SHA256).Hash
    if ([string]$inputHash -cne [string]$outputHash) {
        throw "Native encoded-command stdin receiver changed bytes: $inputHash -> $outputHash"
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot -PathType Container) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

'attested bundle transport projection: PASS'
