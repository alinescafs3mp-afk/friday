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
    -not $wrapperSource.Contains('\$readTask=\$stdinStream.ReadAsync(\$buffer,0,\$wanted)') -or
    -not $wrapperSource.Contains("maximum_archive_bytes=8388608") -or
    -not $wrapperSource.Contains("AddSeconds(120)") -or
    $wrapperSource.Contains('\$stdinStream.CopyTo(\$output)') -or
    $wrapperSource.Contains('[Console]::In.ReadToEnd()') -or
    $wrapperSource.Contains('[ScriptBlock]::Create(') -or
    $wrapperSource.Contains('\$input=[Console]::OpenStandardInput()')) {
    throw 'Encoded-command receiver is not exact-byte, capped, and deadline-bounded'
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
    $expectedInputBytes = ([IO.FileInfo]$inputPath).Length
    $receiver = @"
`$ErrorActionPreference='Stop'
`$expected=[int64]$expectedInputBytes
`$cap=[int64]8388608
if(`$expected-lt 1-or `$expected-gt `$cap){throw 'Archive size is outside the receiver cap'}
`$stdinStream=[Console]::OpenStandardInput()
`$output=[IO.File]::Open('$escapedOutputPath',[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
`$buffer=New-Object byte[] 65536
`$remaining=`$expected
`$deadline=[DateTime]::UtcNow.AddSeconds(120)
try{
  while(`$remaining-gt 0){
    `$now=[DateTime]::UtcNow
    if(`$now-ge `$deadline){throw 'Archive receive deadline expired'}
    `$wait=[int][Math]::Min(10000,[Math]::Max(1,(`$deadline-`$now).TotalMilliseconds))
    `$wanted=[int][Math]::Min([int64]`$buffer.Length,`$remaining)
    `$readTask=`$stdinStream.ReadAsync(`$buffer,0,`$wanted)
    if(-not `$readTask.Wait(`$wait)){throw 'Archive stdin read timed out'}
    `$read=[int]`$readTask.Result
    if(`$read-le 0){throw 'Archive stdin ended before the exact byte count'}
    `$output.Write(`$buffer,0,`$read)
    `$remaining-=`$read
  }
  `$output.Flush(`$true)
}finally{`$output.Dispose();`$stdinStream.Dispose()}
if((Get-Item -LiteralPath '$escapedOutputPath' -Force).Length-ne `$expected){throw 'Staged archive size changed'}
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

    $journalPath = Join-Path $temporaryRoot 'extended-string-journal.jsonl'
    [IO.File]::WriteAllLines(
        $journalPath,
        [string[]]@(
            '{"at_utc":"2026-08-20T16:03:29Z","state":"started"}',
            '{"at_utc":"2026-08-20T16:05:24Z","state":"ssh_disconnected"}'
        ),
        [Text.UTF8Encoding]::new($false)
    )
    $unsafeJournalTail = @(Get-Content -LiteralPath $journalPath -Encoding utf8)
    if ($unsafeJournalTail.Count -ne 2 -or
        $null -eq $unsafeJournalTail[0].PSObject.Properties['PSPath'] -or
        $null -eq $unsafeJournalTail[0].PSObject.Properties['PSProvider']) {
        throw 'Native Get-Content extended-string reproduction is absent'
    }
    $unsafeProjectionRejected = $false
    try {
        foreach ($line in $unsafeJournalTail) {
            if ($null -ne $line.PSObject.Properties['PSPath'] -or
                $null -ne $line.PSObject.Properties['PSProvider']) {
                throw 'extended Get-Content string is not a JSON primitive'
            }
        }
    }
    catch {
        if ($_.Exception.Message -cne 'extended Get-Content string is not a JSON primitive') {
            throw
        }
        $unsafeProjectionRejected = $true
    }
    if (-not $unsafeProjectionRejected) {
        throw 'Raw Get-Content journal projection was not rejected before serialization'
    }
    $safeJournalTail = @($unsafeJournalTail | ForEach-Object {
        $record = ([string]$_ | ConvertFrom-Json)
        [ordered]@{
            at_utc = [string]$record.at_utc
            state = [string]$record.state
        }
    })
    $safeJson = [ordered]@{ journal_tail = $safeJournalTail } |
        ConvertTo-Json -Compress -Depth 4
    $safeJsonBytes = [Text.Encoding]::UTF8.GetByteCount([string]$safeJson)
    if ($safeJsonBytes -lt 1 -or $safeJsonBytes -gt 4096 -or
        $safeJson -match 'PSPath|PSProvider|PSDrive|PSParentPath') {
        throw 'Safe primitive journal projection is not bounded'
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot -PathType Container) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

'attested bundle transport projection: PASS'
