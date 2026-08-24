[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$UserName,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PublicKeyFile,

    [Parameter()]
    [ValidateSet('192.168.1.78')]
    [string]$ManagementAddress = '192.168.1.78',

    [Parameter()]
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$keyPath = (Get-Item -LiteralPath $PublicKeyFile).FullName
$keyBytes = [IO.File]::ReadAllBytes($keyPath)
if ($keyBytes.Length -lt 80 -or $keyBytes.Length -gt 16384) {
    throw 'Public key file has an invalid bounded size.'
}
$publicKey = [Text.Encoding]::UTF8.GetString($keyBytes).Trim()
if ($publicKey -notmatch '\A(?:ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/]+={0,3}(?: [^\r\n]{1,256})?\z') {
    throw 'Only one modern OpenSSH public key without options is accepted.'
}
$algorithm = [Security.Cryptography.SHA256]::Create()
try {
    $keyFingerprint = ([BitConverter]::ToString(
        $algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($publicKey))
    )).Replace('-', '').ToLowerInvariant()
} finally {
    $algorithm.Dispose()
}

$plan = [ordered]@{
    schema = 'friday.secondary-openssh-bootstrap.v1'
    apply = [bool]$Apply
    user = $UserName
    management_address = $ManagementAddress
    public_key_sha256 = $keyFingerprint
    password_authentication_changed = $false
    private_key_received = $false
}
if (-not $Apply) {
    $plan | ConvertTo-Json -Depth 4
    return
}

$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'OpenSSH bootstrap requires an elevated PowerShell session.'
}
$localUser = Get-LocalUser -Name $UserName -ErrorAction Stop
if (-not $localUser.Enabled) {
    throw 'Target local user is disabled.'
}
$capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if ([string]$capability.State -cne 'Installed') {
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
}
Set-Service -Name sshd -StartupType Automatic
Start-Service -Name sshd

function Add-AuthorizedKey([string]$Path, [string]$Key) {
    $existing = @()
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $item = Get-Item -LiteralPath $Path
        if ($item.Length -gt 1048576) {
            throw 'Existing authorized_keys exceeds the 1 MiB bootstrap bound.'
        }
        $existing = @([IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8))
        if (@($existing | Where-Object { [string]$_ -match 'PRIVATE KEY' }).Count -ne 0) {
            throw 'Existing authorized_keys contains forbidden private-key material.'
        }
    }
    if (@($existing | Where-Object { [string]::Equals(([string]$_).Trim(), $Key, [StringComparison]::Ordinal) }).Count -eq 0) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $backup = '{0}.friday-public-backup-{1}' -f $Path, [DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff')
            Copy-Item -LiteralPath $Path -Destination $backup
        }
        $updated = @($existing | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }) + @($Key)
        [IO.File]::WriteAllLines($Path, $updated, [Text.UTF8Encoding]::new($false))
    }
}

$adminGroup = Get-LocalGroup -SID 'S-1-5-32-544'
$adminMembers = @(Get-LocalGroupMember -Group $adminGroup)
$isAdministrator = @($adminMembers | Where-Object { [string]$_.SID.Value -ceq [string]$localUser.SID.Value }).Count -eq 1
if ($isAdministrator) {
    $authorizedKeys = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
    Add-AuthorizedKey $authorizedKeys $publicKey
    & icacls.exe $authorizedKeys /inheritance:r /grant:r '*S-1-5-18:F' /grant:r '*S-1-5-32-544:F' | Out-Null
} else {
    $profileRoot = Join-Path 'C:\Users' $UserName
    if (-not (Test-Path -LiteralPath $profileRoot -PathType Container)) {
        throw 'Target user profile directory is absent.'
    }
    $sshDirectory = Join-Path $profileRoot '.ssh'
    [IO.Directory]::CreateDirectory($sshDirectory) | Out-Null
    $authorizedKeys = Join-Path $sshDirectory 'authorized_keys'
    Add-AuthorizedKey $authorizedKeys $publicKey
    & icacls.exe $sshDirectory /inheritance:r /T /C `
        /grant:r ('*{0}:(OI)(CI)F' -f $localUser.SID.Value) `
        /grant:r '*S-1-5-18:(OI)(CI)F' | Out-Null
}
if ($LASTEXITCODE -ne 0) {
    throw 'Could not apply the restrictive authorized_keys ACL.'
}

$ruleName = 'Friday Secondary - OpenSSH bootstrap'
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Disable-NetFirewallRule
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule `
    -DisplayName $ruleName `
    -Direction Inbound `
    -Action Allow `
    -Enabled True `
    -Profile Any `
    -Protocol TCP `
    -LocalPort 22 `
    -RemoteAddress $ManagementAddress `
    -EdgeTraversalPolicy Block | Out-Null

$conflicts = @(
    Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
        Where-Object { [string]$_.DisplayName -cne $ruleName } |
        Get-NetFirewallPortFilter |
        Where-Object {
            [string]$_.Protocol -in @('6', 'TCP') -and
            @([string]$_.LocalPort -split ',') -contains '22'
        }
)
if ($conflicts.Count -ne 0) {
    throw 'Another enabled inbound allow rule reaches TCP 22; narrow it before trusting the channel.'
}

& (Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe') -t
if ($LASTEXITCODE -ne 0) {
    throw 'sshd configuration validation failed.'
}
Restart-Service -Name sshd
$plan.status = 'key_installed_password_login_unchanged'
$plan | ConvertTo-Json -Depth 4
