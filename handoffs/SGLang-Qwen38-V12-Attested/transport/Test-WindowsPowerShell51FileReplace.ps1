[CmdletBinding()]
param(
    [switch]$RequireWindowsPowerShell51
)

$ErrorActionPreference = 'Stop'

if ($RequireWindowsPowerShell51 -and
    ([string]$PSVersionTable.PSEdition -cne 'Desktop' -or
        [int]$PSVersionTable.PSVersion.Major -ne 5 -or
        [int]$PSVersionTable.PSVersion.Minor -ne 1)) {
    throw 'Native File.Replace gate requires Windows PowerShell 5.1 Desktop'
}

function Get-TestSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Native File.Replace test file is absent'
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TestCasState(
    [AllowNull()][object]$LiveHash,
    [AllowNull()][object]$BackupHash,
    [string]$OldHash,
    [string]$NewHash
) {
    if ([string]$LiveHash -ceq $NewHash) {
        if ($null -eq $BackupHash) { return 'retain' }
        if ([string]$BackupHash -ceq $OldHash) { return 'remove_backup' }
        return 'reject'
    }
    if ([string]$LiveHash -ceq $OldHash -and $null -eq $BackupHash) {
        return 'replace'
    }
    return 'reject'
}

$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) (
    'friday-attested-filereplace-' + [Guid]::NewGuid().ToString('N')
)
try {
    [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
    $targetPath = Join-Path $temporaryRoot 'target.txt'
    $sourcePath = Join-Path $temporaryRoot 'source.txt'
    $backupPath = Join-Path $temporaryRoot '.target.txt.friday-attested-sync-v1.backup'
    [IO.File]::WriteAllBytes($targetPath, [Text.Encoding]::UTF8.GetBytes('exact-old-bytes'))
    [IO.File]::WriteAllBytes($sourcePath, [Text.Encoding]::UTF8.GetBytes('exact-new-bytes'))
    $oldHash = Get-TestSha256 $targetPath
    $newHash = Get-TestSha256 $sourcePath
    if ((Get-TestCasState $oldHash $null $oldHash $newHash) -cne 'replace') {
        throw 'Native File.Replace start state was rejected'
    }

    [IO.File]::Replace($sourcePath, $targetPath, $backupPath, $true)
    $liveHash = Get-TestSha256 $targetPath
    $backupHash = Get-TestSha256 $backupPath
    if ($liveHash -cne $newHash -or $backupHash -cne $oldHash -or
        (Test-Path -LiteralPath $sourcePath)) {
        throw 'Native File.Replace did not preserve exact atomic evidence'
    }
    if ((Get-TestCasState $liveHash $backupHash $oldHash $newHash) -cne
        'remove_backup') {
        throw 'Native File.Replace crash residue was not resumable'
    }

    # A retry may delete only the deterministic backup after both hashes prove
    # the exact post-replacement crash state.
    if ((Get-TestSha256 $targetPath) -cne $newHash -or
        (Get-TestSha256 $backupPath) -cne $oldHash) {
        throw 'Native File.Replace evidence changed before backup cleanup'
    }
    Remove-Item -LiteralPath $backupPath -Force
    if (Test-Path -LiteralPath $backupPath) {
        throw 'Native File.Replace exact backup was not removed'
    }
    if ((Get-TestCasState (Get-TestSha256 $targetPath) $null $oldHash $newHash) -cne
        'retain') {
        throw 'Native File.Replace idempotent retry did not converge'
    }

    foreach ($mutation in @(
        [pscustomobject]@{
            Live = $oldHash; Backup = $oldHash; Label = 'old target plus backup'
        },
        [pscustomobject]@{
            Live = $newHash; Backup = ('c' * 64); Label = 'new target plus wrong backup'
        },
        [pscustomobject]@{
            Live = $oldHash; Backup = ('c' * 64); Label = 'old target plus wrong backup'
        }
    )) {
        if ((Get-TestCasState $mutation.Live $mutation.Backup $oldHash $newHash) -cne
            'reject') {
            throw "Unsafe native File.Replace crash state was accepted: $($mutation.Label)"
        }
    }

    [pscustomobject][ordered]@{
        schema = 'friday.attested-filereplace-test.v1'
        status = 'pass'
        powershell_edition = [string]$PSVersionTable.PSEdition
        powershell_version = [string]$PSVersionTable.PSVersion
        existing_file_replaced = $true
        exact_old_backup_verified = $true
        crash_retry_converged = $true
        unsafe_residue_rejected = $true
    } | ConvertTo-Json -Compress -Depth 12
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot -PathType Container) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
