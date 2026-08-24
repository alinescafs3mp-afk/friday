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
    [string]$HardwareRuntimeReceiptOutputPath,

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
$expectedObservedHardwareReceiptSha256 = '7b850221e7e11ac0063971d7baaf627c96eae5441368f1907cc070106832b0f3'
$expectedSglangImage = 'lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405'
$expectedSglangImageId = 'sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405'
$expectedSglangConfigDigest = 'sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc'
$expectedSglangOciManifestDigest = 'sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405'
$expectedWindowsCaption = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('0JzQsNC50LrRgNC+0YHQvtGE0YIgV2luZG93cyAxMSBQcm8=')
)
$ociManifestMediaType = 'application/vnd.oci.image.manifest.v1+json'
$expectedGpu = [ordered]@{
    compute_capability = '12.0'
    driver_version = '610.88'
    memory_total_mib = 16303
    name = 'NVIDIA GeForce RTX 5080 Laptop GPU'
    uuid = 'GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07'
}
$expectedWslVersions = @(
    '2.7.3.0',
    '6.6.114.1-1',
    '1.0.73',
    '1.2.6676',
    '1.611.1-81528511',
    '10.0.26100.1-240331-1435.ge-release',
    '10.0.26200.9168'
)

function Assert-ExactImageReference([string]$Value, [string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch $digestPattern) {
        throw "$Name must be an exact lowercase repo@sha256 digest."
    }
}

function Invoke-Captured([string]$Executable, [string[]]$Arguments) {
    $previousErrorActionPreference = $ErrorActionPreference
    $exitCode = $null
    try {
        # Native stderr can become a terminating NativeCommandError under
        # Windows PowerShell 5 when the enclosing script uses Stop.
        $ErrorActionPreference = 'Continue'
        $output = & $Executable @Arguments 2> $null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
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

function Convert-ExactGpuProjection([string]$Text) {
    $lines = @($Text -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($lines.Count -ne 1 -or $lines[0].Length -gt 512) {
        throw 'nvidia-smi did not return one bounded GPU row.'
    }
    $parts = @($lines[0].Split(',') | ForEach-Object { $_.Trim() })
    if ($parts.Count -ne 5) {
        throw 'nvidia-smi returned an unexpected bounded projection.'
    }
    $memory = 0
    if (-not [int]::TryParse($parts[2], [ref]$memory)) {
        throw 'nvidia-smi returned an invalid memory projection.'
    }
    $projection = [ordered]@{
        compute_capability = $parts[3]
        driver_version = $parts[4]
        memory_total_mib = $memory
        name = $parts[1]
        uuid = $parts[0]
    }
    foreach ($key in $expectedGpu.Keys) {
        if ([string]$projection[$key] -cne [string]$expectedGpu[$key]) {
            throw 'GPU identity differs from the code-owned hardware contract.'
        }
    }
    return $projection
}

function Get-WslVersionProjection([string]$Text) {
    $values = @()
    foreach ($line in @($Text -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        $separator = $line.IndexOf(':')
        if ($separator -lt 1 -or $separator -eq ($line.Length - 1)) {
            throw 'wsl --version returned an unexpected row.'
        }
        $values += $line.Substring($separator + 1).Trim()
    }
    if ($values.Count -ne $expectedWslVersions.Count) {
        throw 'wsl --version returned an unexpected component set.'
    }
    for ($index = 0; $index -lt $expectedWslVersions.Count; $index += 1) {
        if ([string]$values[$index] -cne [string]$expectedWslVersions[$index]) {
            throw 'WSL component version differs from the code-owned hardware contract.'
        }
    }
    return [ordered]@{
        direct3d_version = $values[4]
        dxcore_version = $values[5]
        kernel_version = $values[1]
        msrdc_version = $values[3]
        version = $values[0]
        windows_component_version = $values[6]
        wslg_version = $values[2]
    }
}

function Write-NewUtf8File([string]$Path, [string]$Text) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $parent = Split-Path -Parent $resolved
    if ([string]::IsNullOrWhiteSpace($parent) -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'Hardware receipt parent directory does not exist.'
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
    $stream = $null
    try {
        $stream = [IO.File]::Open($resolved, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
    return $resolved
}

function Test-ComposeExactImageSelector([string]$ImageRef) {
    $nonce = [Guid]::NewGuid().ToString('N')
    $project = "fridayselector$nonce"
    $composePath = Join-Path ([IO.Path]::GetTempPath()) "$project.json"
    $composeValue = [ordered]@{
        services = [ordered]@{
            selector = [ordered]@{
                image = $ImageRef
                network_mode = 'none'
                pull_policy = 'never'
            }
        }
    }
    [void](Write-NewUtf8File $composePath (($composeValue | ConvertTo-Json -Depth 5 -Compress) + "`n"))
    $cleanupExitCode = $null
    try {
        [void](Invoke-Captured 'docker.exe' @(
            'compose', '--project-name', $project, '--file', $composePath,
            'create', '--pull', 'never', 'selector'
        ))
        $containerId = Invoke-Captured 'docker.exe' @(
            'compose', '--project-name', $project, '--file', $composePath,
            'ps', '--all', '--quiet', 'selector'
        )
        if ($containerId -notmatch '\A[0-9a-f]{12,64}\z') {
            throw 'Compose did not create exactly one bounded selector container.'
        }
        $inspection = Invoke-Captured 'docker.exe' @(
            'container', 'inspect', '--format', '{{json .}}', $containerId
        ) | ConvertFrom-Json
        $manifestDescriptor = $inspection.ImageManifestDescriptor
        if ([string]$inspection.Config.Image -cne $ImageRef -or
            [string]$inspection.Image -cne $expectedSglangImageId -or
            [string]$manifestDescriptor.digest -cne $expectedSglangOciManifestDigest -or
            [string]$manifestDescriptor.mediaType -cne $ociManifestMediaType -or
            [string]$manifestDescriptor.platform.os -cne 'linux' -or
            [string]$manifestDescriptor.platform.architecture -cne 'amd64') {
            throw 'Compose did not resolve the exact local SGLang image.'
        }
    } finally {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & docker.exe compose --project-name $project --file $composePath down --remove-orphans 1> $null 2> $null
            $cleanupExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
            if (Test-Path -LiteralPath $composePath -PathType Leaf) {
                Remove-Item -LiteralPath $composePath -Force
            }
        }
        if ($cleanupExitCode -ne 0) {
            throw 'Compose selector canary cleanup failed.'
        }
    }
    return $true
}

$operatingSystem = Get-CimInstance -ClassName Win32_OperatingSystem
$computerSystem = Get-CimInstance -ClassName Win32_ComputerSystem
if ([string]$operatingSystem.Caption -cne $expectedWindowsCaption -or
    [string]$operatingSystem.Version -cne '10.0.26200' -or
    [string]$operatingSystem.BuildNumber -cne '26200') {
    throw 'Windows version differs from the code-owned hardware contract.'
}
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
$wslProjection = Get-WslVersionProjection $wslVersion
$dockerVersion = Invoke-Captured 'docker.exe' @('version', '--format', '{{json .}}') | ConvertFrom-Json
$dockerInfo = Invoke-Captured 'docker.exe' @('info', '--format', '{{json .}}') | ConvertFrom-Json
$composeVersion = Invoke-Captured 'docker.exe' @('compose', 'version', '--short')
$dockerDesktopPath = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$dockerDesktop = Get-Item -LiteralPath $dockerDesktopPath
$dockerDesktopVersion = $dockerDesktop.VersionInfo
if ([string]$dockerVersion.Client.Version -cne '29.7.2' -or
    [string]$dockerVersion.Client.ApiVersion -cne '1.55' -or
    [string]$dockerVersion.Server.Version -cne '29.7.2' -or
    [string]$dockerVersion.Server.ApiVersion -cne '1.55' -or
    [string]$dockerInfo.OSType -cne 'linux' -or
    [string]$dockerInfo.Architecture -cne 'x86_64' -or
    [string]$composeVersion -cne '5.4.0' -or
    [string]$dockerDesktopVersion.ProductVersion -cne '4.87.0.236836' -or
    [string]$dockerDesktopVersion.FileVersion -cne '4.87.0.236836') {
    throw 'Docker runtime differs from the code-owned hardware contract.'
}

$hostGpuText = Invoke-Captured 'nvidia-smi.exe' @(
    '--id=0',
    '--query-gpu=uuid,name,memory.total,compute_cap,driver_version',
    '--format=csv,noheader,nounits'
)
$hostGpu = Convert-ExactGpuProjection $hostGpuText

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
        'run', '--rm', '--pull', 'never', '--network', 'none', '--gpus', 'device=0',
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
$runtimeGpu = $null
if ($InspectSglangHelp) {
    Assert-ExactImageReference $SglangImage 'SglangImage'
    if ([string]$SglangImage -cne $expectedSglangImage) {
        throw 'SglangImage differs from the code-owned hardware/runtime contract.'
    }
    $sglangInspectionText = Invoke-Captured 'docker.exe' @(
        'image', 'inspect', '--format', '{{json .}}', $SglangImage
    )
    $sglangInspection = $sglangInspectionText | ConvertFrom-Json
    if ([string]$sglangInspection.Id -cne $expectedSglangImageId -or
        @($sglangInspection.RepoDigests | Where-Object { [string]$_ -ceq $expectedSglangImage }).Count -ne 1 -or
        [string]$sglangInspection.Descriptor.digest -cne $expectedSglangOciManifestDigest -or
        [string]$sglangInspection.Descriptor.mediaType -cne $ociManifestMediaType -or
        [string]$sglangInspection.Os -cne 'linux' -or
        [string]$sglangInspection.Architecture -cne 'amd64') {
        throw 'Local SGLang image identity/platform differs from the exact runtime contract.'
    }
    $composeSelectorVerified = Test-ComposeExactImageSelector $SglangImage
    $helpText = Invoke-Captured 'docker.exe' @(
        'run', '--rm', '--pull', 'never', '--network', 'none', '--security-opt', 'no-new-privileges:true',
        '--cap-drop', 'ALL', '--entrypoint', 'python3', $SglangImage,
        '-m', 'sglang.launch_server', '--help'
    )
    $requiredFlags = @(
        '--model-path', '--served-model-name', '--api-key', '--reasoning-parser', '--dtype',
        '--tool-call-parser', '--attention-backend', '--quantization', '--moe-runner-backend',
        '--flashinfer-mxfp4-moe-precision',
        '--kv-cache-dtype', '--chunked-prefill-size', '--max-running-requests',
        '--cuda-graph-backend-decode', '--cuda-graph-backend-prefill',
        '--context-length', '--max-total-tokens',
        '--mem-fraction-static', '--enable-metrics', '--enable-cache-report'
    )
    $missingFlags = @($requiredFlags | Where-Object { $helpText -notmatch [regex]::Escape($_) })
    if ($missingFlags.Count -ne 0) {
        throw 'The pinned SGLang image does not expose every baseline launch flag.'
    }
    $versionProbeCode = 'import importlib.metadata as m,json,torch; print(json.dumps({"cuda_runtime_version":torch.version.cuda,"flashinfer_version":m.version("flashinfer-python"),"pytorch_version":m.version("torch"),"sgl_kernel_version":m.version("sgl-kernel"),"sglang_version":m.version("sglang")},sort_keys=True,separators=(",",":")))'
    $runtimeVersionsText = Invoke-Captured 'docker.exe' @(
        'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
        '--tmpfs', '/tmp:size=16m', '--security-opt', 'no-new-privileges:true',
        '--cap-drop', 'ALL', '--entrypoint', 'python3', $SglangImage,
        '-c', $versionProbeCode
    )
    $runtimeVersions = $runtimeVersionsText | ConvertFrom-Json
    if ([string]$runtimeVersions.sglang_version -cne '0.5.17' -or
        [string]$runtimeVersions.cuda_runtime_version -cne '13.0' -or
        [string]$runtimeVersions.pytorch_version -cne '2.11.0+cu130' -or
        [string]$runtimeVersions.flashinfer_version -cne '0.6.15.post1' -or
        [string]$runtimeVersions.sgl_kernel_version -cne '0.4.5') {
        throw 'Pinned SGLang image package/runtime versions differ from the exact contract.'
    }
    $sglangHelp = [ordered]@{
        image_ref = $SglangImage
        image_id = [string]$sglangInspection.Id
        image_config_digest = $expectedSglangConfigDigest
        image_oci_manifest_digest = [string]$sglangInspection.Descriptor.digest
        compose_exact_selector_verified = [bool]$composeSelectorVerified
        required_flag_count = $requiredFlags.Count
        required_flags_present = $true
        help_sha256 = Get-TextSha256 $helpText
        runtime_versions = [ordered]@{
            sglang_version = [string]$runtimeVersions.sglang_version
            cuda_runtime_version = [string]$runtimeVersions.cuda_runtime_version
            pytorch_version = [string]$runtimeVersions.pytorch_version
            flashinfer_version = [string]$runtimeVersions.flashinfer_version
            sgl_kernel_version = [string]$runtimeVersions.sgl_kernel_version
        }
    }
    if ($RunGpuCanary) {
        $runtimeGpuText = Invoke-Captured 'docker.exe' @(
            'run', '--rm', '--pull', 'never', '--network', 'none', '--gpus', 'device=0',
            '--security-opt', 'no-new-privileges:true', '--cap-drop', 'ALL', '--read-only',
            '--entrypoint', '/usr/bin/nvidia-smi', $SglangImage,
            '--id=0', '--query-gpu=uuid,name,memory.total,compute_cap,driver_version',
            '--format=csv,noheader,nounits'
        )
        $runtimeGpu = Convert-ExactGpuProjection $runtimeGpuText
    }
}

$gatewayImage = $null
if ($InspectGatewayImage) {
    $expectedGateway = 'nginxinc/nginx-unprivileged@sha256:d61d7ef52430df468e74ed6ee6e914429b80e20ba988e3176278a73165f876cf'
    $expectedGatewayPlatformManifest = 'sha256:8d764dd92e0b48d0ca94887dc0fe1df6dffc5200b25b2efcc2deb7ffb61d714c'
    $expectedGatewayConfig = 'sha256:89dc7d054bddca245db3d5a779e363007d0e75b1161cfe2f283ebeaf0ed90d50'
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
    if ([string]$gatewayInspection.Id -cne $expectedGatewayPlatformManifest -or
        [string]$gatewayInspection.Descriptor.digest -cne $expectedGatewayPlatformManifest -or
        [string]$gatewayInspection.Descriptor.mediaType -cne $ociManifestMediaType -or
        [string]$gatewayInspection.Os -cne 'linux' -or
        [string]$gatewayInspection.Architecture -cne 'amd64' -or
        [string]$gatewayInspection.Config.User -cne '101' -or
        $gatewayVersion.Count -ne 1 -or
        [string]$gatewayVersion[0] -cne 'NGINX_VERSION=1.31.3' -or
        @($gatewayInspection.RepoDigests | Where-Object { [string]$_ -ceq $expectedGateway }).Count -ne 1) {
        throw 'Local gateway image does not match the exact platform/user/version contract.'
    }
    $gatewayRuntimeProbe = Invoke-Captured 'docker.exe' @(
        'run', '--rm', '--pull', 'never', '--network', 'none', '--user', '101', '--read-only',
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
        platform_manifest_digest = $expectedGatewayPlatformManifest
        config_digest = $expectedGatewayConfig
    }
}

$dockerDesktopRun = Get-ItemProperty `
    -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' `
    -ErrorAction SilentlyContinue
$dockerAutoStart = $null -ne $dockerDesktopRun -and `
    -not [string]::IsNullOrWhiteSpace([string]$dockerDesktopRun.'Docker Desktop')

$hardwareReceipt = $null
$hardwareReceiptJson = $null
$hardwareReceiptSha256 = $null
$resolvedHardwareReceipt = $null
if ($null -ne $runtimeGpu) {
    $hardwareReceipt = [ordered]@{
        docker = [ordered]@{
            client_api_version = [string]$dockerVersion.Client.ApiVersion
            client_version = [string]$dockerVersion.Client.Version
            compose_version = [string]$composeVersion
            desktop_file_version = [string]$dockerDesktopVersion.FileVersion
            desktop_product_version = [string]$dockerDesktopVersion.ProductVersion
            server_api_version = [string]$dockerVersion.Server.ApiVersion
            server_architecture = [string]$dockerInfo.Architecture
            server_os = [string]$dockerInfo.OSType
            server_version = [string]$dockerVersion.Server.Version
        }
        gpu = $runtimeGpu
        schema = 'friday.secondary-hardware-runtime.v1'
        status = 'observed_unaccepted'
        windows = [ordered]@{
            build = [string]$operatingSystem.BuildNumber
            caption = [string]$operatingSystem.Caption
            version = [string]$operatingSystem.Version
        }
        wsl = $wslProjection
    }
    $hardwareReceiptJson = ($hardwareReceipt | ConvertTo-Json -Depth 6 -Compress) + "`n"
    $hardwareReceiptSha256 = Get-TextSha256 $hardwareReceiptJson
    if ([string]$hardwareReceiptSha256 -cne $expectedObservedHardwareReceiptSha256) {
        throw 'Hardware/runtime receipt serialization differs from the canonical contract.'
    }
}
if (-not [string]::IsNullOrWhiteSpace($HardwareRuntimeReceiptOutputPath)) {
    if ($null -eq $hardwareReceiptJson) {
        throw 'HardwareRuntimeReceiptOutputPath requires both GPU and SGLang runtime canaries.'
    }
    $resolvedHardwareReceipt = Write-NewUtf8File $HardwareRuntimeReceiptOutputPath $hardwareReceiptJson
}

$report = [ordered]@{
    schema = 'friday.secondary-windows-preflight.v1'
    status = $(if ($RunGpuCanary -and $InspectSglangHelp -and $InspectGatewayImage -and $null -ne $runtimeGpu) {
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
        components = $wslProjection
        version_output_sha256 = Get-TextSha256 $wslVersion
        status_output_sha256 = Get-TextSha256 $wslStatus
    }
    docker = [ordered]@{
        client_version = [string]$dockerVersion.Client.Version
        client_api_version = [string]$dockerVersion.Client.ApiVersion
        server_version = [string]$dockerVersion.Server.Version
        server_api_version = [string]$dockerVersion.Server.ApiVersion
        server_os = [string]$dockerInfo.OSType
        server_architecture = [string]$dockerInfo.Architecture
        compose_version = $composeVersion
        desktop_product_version = [string]$dockerDesktopVersion.ProductVersion
        desktop_file_version = [string]$dockerDesktopVersion.FileVersion
        desktop_autostart_observed = $dockerAutoStart
    }
    host_gpu = $hostGpu
    runtime_gpu = $runtimeGpu
    gpu_container_canary = $gpuCanary
    sglang_help = $sglangHelp
    gateway_image = $gatewayImage
    hardware_runtime_receipt = [ordered]@{
        status = $(if ($null -ne $hardwareReceipt) { 'observed_unaccepted' } else { 'not_observed' })
        sha256 = $hardwareReceiptSha256
        output_path = $resolvedHardwareReceipt
    }
    operator_checks_required = @('wsl_update_state', 'docker_desktop_wsl2_setting', 'ac_sleep_disabled')
    credentials_retained = $false
}
$json = $report | ConvertTo-Json -Depth 8
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
    [IO.File]::WriteAllText($resolvedOutput, ($json + "`n"), [Text.UTF8Encoding]::new($false))
}
$json
