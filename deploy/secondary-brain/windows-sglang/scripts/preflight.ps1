[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('\A(?:25[0-5]|2[0-4][0-9]|[01]?[0-9]?[0-9])(?:\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9]?[0-9])){3}\z')]
    [string]$ExpectedAddress = '192.168.1.35',

    [Parameter()]
    [string]$CudaCanaryImage,

    [Parameter()]
    [string]$SglangImage,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$GatewayImage = 'nginxinc/nginx-unprivileged@sha256:d61d7ef52430df468e74ed6ee6e914429b80e20ba988e3176278a73165f876cf',

    [Parameter()]
    [string]$OutputPath,

    [Parameter()]
    [switch]$RunGpuCanary,

    [Parameter()]
    [switch]$InspectSglangHelp,

    [Parameter()]
    [switch]$InspectGatewayImage
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$digestPattern = '\A[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}\z'

function Assert-ExactImageReference([string]$Value, [string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch $digestPattern) {
        throw "$Name must be an exact lowercase repo@sha256 digest."
    }
}

function Invoke-Captured([string]$Executable, [string[]]$Arguments) {
    $output = & $Executable @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable failed."
    }
    return (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
}

function Get-TextSha256([string]$Value) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

$operatingSystem = Get-CimInstance -ClassName Win32_OperatingSystem
$computerSystem = Get-CimInstance -ClassName Win32_ComputerSystem
$addresses = @(
    Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred |
        Where-Object { $_.IPAddress -notlike '127.*' } |
        ForEach-Object { [string]$_.IPAddress }
)
if ($addresses -notcontains $ExpectedAddress) {
    throw 'The expected static LAN address is not currently assigned.'
}

$wslVersion = Invoke-Captured 'wsl.exe' @('--version')
$wslStatus = Invoke-Captured 'wsl.exe' @('--status')
$dockerVersion = Invoke-Captured 'docker.exe' @('version', '--format', '{{json .}}') | ConvertFrom-Json
$dockerInfo = Invoke-Captured 'docker.exe' @('info', '--format', '{{json .}}') | ConvertFrom-Json
$composeVersion = Invoke-Captured 'docker.exe' @('compose', 'version', '--short')
if ([string]$dockerInfo.OSType -cne 'linux') {
    throw 'Docker Desktop is not using Linux containers.'
}

$hostGpuRows = @()
$hostGpuText = Invoke-Captured 'nvidia-smi.exe' @(
    '--query-gpu=name,driver_version,memory.total,compute_cap',
    '--format=csv,noheader,nounits'
)
foreach ($line in @($hostGpuText -split "`r?`n")) {
    $parts = @($line.Split(',') | ForEach-Object { $_.Trim() })
    if ($parts.Count -ne 4) {
        throw 'nvidia-smi returned an unexpected bounded projection.'
    }
    $hostGpuRows += [ordered]@{
        name = $parts[0]
        driver_version = $parts[1]
        memory_total_mib = [int]$parts[2]
        compute_capability = $parts[3]
    }
}
if ($hostGpuRows.Count -lt 1) {
    throw 'No NVIDIA GPU was observed.'
}

$gpuCanary = $null
if ($RunGpuCanary) {
    Assert-ExactImageReference $CudaCanaryImage 'CudaCanaryImage'
    $cudaImageId = Invoke-Captured 'docker.exe' @('image', 'inspect', '--format', '{{.Id}}', $CudaCanaryImage)
    $canaryProgram = @'
import json, torch
device = torch.device("cuda:0")
x = torch.arange(4096, dtype=torch.float32, device=device)
y = float((x * 2).sum().item())
p = torch.cuda.get_device_properties(device)
print(json.dumps({"name": p.name, "memory_total_bytes": p.total_memory,
                  "compute_capability": [p.major, p.minor], "kernel_sum": y}, sort_keys=True))
'@
    $gpuCanaryText = Invoke-Captured 'docker.exe' @(
        'run', '--rm', '--network', 'none', '--gpus', 'device=0',
        '--security-opt', 'no-new-privileges:true', '--cap-drop', 'ALL',
        '--read-only', '--tmpfs', '/tmp:size=64m', '--entrypoint', 'python3',
        $CudaCanaryImage, '-c', $canaryProgram
    )
    $gpuObservation = $gpuCanaryText | ConvertFrom-Json
    if ([double]$gpuObservation.kernel_sum -ne 16773120.0) {
        throw 'The CUDA allocation/kernel canary returned the wrong result.'
    }
    $gpuCanary = [ordered]@{
        image_ref = $CudaCanaryImage
        image_id = $cudaImageId
        observation = $gpuObservation
    }
}

$sglangHelp = $null
if ($InspectSglangHelp) {
    Assert-ExactImageReference $SglangImage 'SglangImage'
    $sglangImageId = Invoke-Captured 'docker.exe' @('image', 'inspect', '--format', '{{.Id}}', $SglangImage)
    $helpText = Invoke-Captured 'docker.exe' @(
        'run', '--rm', '--network', 'none', '--security-opt', 'no-new-privileges:true',
        '--cap-drop', 'ALL', '--entrypoint', 'python3', $SglangImage,
        '-m', 'sglang.launch_server', '--help'
    )
    $requiredFlags = @(
        '--model-path', '--served-model-name', '--api-key', '--reasoning-parser',
        '--tool-call-parser', '--attention-backend', '--fp4-gemm-backend',
        '--kv-cache-dtype', '--chunked-prefill-size', '--max-running-requests',
        '--cuda-graph-max-bs', '--context-length', '--max-total-tokens',
        '--mem-fraction-static', '--enable-metrics', '--enable-cache-report'
    )
    $missingFlags = @($requiredFlags | Where-Object { $helpText -notmatch [regex]::Escape($_) })
    if ($missingFlags.Count -ne 0) {
        throw 'The pinned SGLang image does not expose every baseline launch flag.'
    }
    $sglangHelp = [ordered]@{
        image_ref = $SglangImage
        image_id = $sglangImageId
        required_flag_count = $requiredFlags.Count
        required_flags_present = $true
        help_sha256 = Get-TextSha256 $helpText
    }
}

$gatewayImage = $null
if ($InspectGatewayImage) {
    $expectedGateway = 'nginxinc/nginx-unprivileged@sha256:d61d7ef52430df468e74ed6ee6e914429b80e20ba988e3176278a73165f876cf'
    if (-not [string]::Equals($GatewayImage, $expectedGateway, [StringComparison]::Ordinal)) {
        throw 'Gateway image differs from the code-owned exact OCI index digest.'
    }
    $gatewayInspectionText = Invoke-Captured 'docker.exe' @(
        'image', 'inspect', '--format', '{{json .}}', $GatewayImage
    )
    $gatewayInspection = $gatewayInspectionText | ConvertFrom-Json
    $gatewayVersion = @(
        $gatewayInspection.Config.Env |
            Where-Object { [string]$_ -like 'NGINX_VERSION=*' }
    )
    if ([string]$gatewayInspection.Os -cne 'linux' -or
        [string]$gatewayInspection.Architecture -cne 'amd64' -or
        [string]$gatewayInspection.Config.User -cne '101' -or
        $gatewayVersion.Count -ne 1 -or
        [string]$gatewayVersion[0] -cne 'NGINX_VERSION=1.31.3' -or
        @($gatewayInspection.RepoDigests | Where-Object { [string]$_ -ceq $expectedGateway }).Count -ne 1) {
        throw 'Local gateway image does not match the exact platform/user/version contract.'
    }
    $gatewayRuntimeProbe = Invoke-Captured 'docker.exe' @(
        'run', '--rm', '--network', 'none', '--user', '101', '--read-only',
        '--tmpfs', '/tmp:size=16m', '--security-opt', 'no-new-privileges:true',
        '--cap-drop', 'ALL', '--entrypoint', '/bin/sh', $GatewayImage, '-ceu',
        'test "$(id -u)" = 101; test "$(nginx -v 2>&1)" = "nginx version: nginx/1.31.3"; command -v sed >/dev/null; command -v wget >/dev/null; printf verified'
    )
    if ([string]$gatewayRuntimeProbe -cne 'verified') {
        throw 'Gateway executable/user runtime probe failed.'
    }
    $gatewayImage = [ordered]@{
        image_ref = $expectedGateway
        image_id = [string]$gatewayInspection.Id
        platform = 'linux/amd64'
        user = '101'
        nginx_version = '1.31.3'
        runtime_probe = 'verified'
        platform_manifest_digest = 'sha256:8d764dd92e0b48d0ca94887dc0fe1df6dffc5200b25b2efcc2deb7ffb61d714c'
        config_digest = 'sha256:89dc7d054bddca245db3d5a779e363007d0e75b1161cfe2f283ebeaf0ed90d50'
    }
}

$dockerDesktopRun = Get-ItemProperty `
    -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' `
    -ErrorAction SilentlyContinue
$dockerAutoStart = $null -ne $dockerDesktopRun -and `
    -not [string]::IsNullOrWhiteSpace([string]$dockerDesktopRun.'Docker Desktop')

$report = [ordered]@{
    schema = 'friday.secondary-windows-preflight.v1'
    status = $(if ($RunGpuCanary -and $InspectSglangHelp -and $InspectGatewayImage) {
        'automated_preflight_checks_passed'
    } else {
        'inventory_incomplete'
    })
    observed_at = [DateTime]::UtcNow.ToString('o')
    computer = [ordered]@{
        manufacturer = [string]$computerSystem.Manufacturer
        model = [string]$computerSystem.Model
        windows_caption = [string]$operatingSystem.Caption
        windows_version = [string]$operatingSystem.Version
        windows_build = [string]$operatingSystem.BuildNumber
        expected_address = $ExpectedAddress
        expected_address_present = $true
    }
    wsl = [ordered]@{
        version_output_sha256 = Get-TextSha256 $wslVersion
        status_output_sha256 = Get-TextSha256 $wslStatus
    }
    docker = [ordered]@{
        server_version = [string]$dockerVersion.Server.Version
        server_os = [string]$dockerInfo.OSType
        server_architecture = [string]$dockerInfo.Architecture
        compose_version = $composeVersion
        desktop_autostart_observed = $dockerAutoStart
    }
    host_gpus = $hostGpuRows
    gpu_container_canary = $gpuCanary
    sglang_help = $sglangHelp
    gateway_image = $gatewayImage
    operator_checks_required = @('wsl_update_state', 'docker_desktop_wsl2_setting', 'ac_sleep_disabled')
    credentials_retained = $false
}
$json = $report | ConvertTo-Json -Depth 8
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
    [IO.File]::WriteAllText($resolvedOutput, ($json + "`n"), [Text.UTF8Encoding]::new($false))
}
$json
