[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Plan', 'Populate', 'Verify')]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$DownloaderImage,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$VolumeName = 'friday-secondary-source-gptoss20b-ablit-79f64a52',

    [Parameter()]
    [string]$ManifestPath,

    [Parameter()]
    [string]$OutputManifest,

    [Parameter()]
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$expectedVolumeName = 'friday-secondary-source-gptoss20b-ablit-79f64a52'
$expectedRepository = 'huihui-ai/Huihui-gpt-oss-20b-mxfp4-abliterated-v2'
$expectedRevision = '79f64a520a4a0275f639c1a47d9a5614a8a54477'
$expectedDownloaderImage = 'lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405'
$expectedDownloaderImageId = 'sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405'
$expectedDownloaderManifest = 'sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405'
$expectedDownloaderMediaType = 'application/vnd.oci.image.manifest.v1+json'
$expectedManifestSha256 = '8dfc3a50d1a9407fbb07dde5f1b494157664c75cdd0e140ecb85f7d55732a296'
$expectedManifestSemanticSha256 = '4ab38461ce42f76c32d998ed091b8cfc0a8b483279f676eb8221e56df28d6d02'

if ($DownloaderImage -cne $expectedDownloaderImage) {
    throw 'DownloaderImage differs from the code-owned exact SGLang runtime.'
}
if ($VolumeName -cne $expectedVolumeName) {
    throw 'VolumeName must be the canonical sealed abliterated-source volume.'
}
if ($Mode -eq 'Populate' -and [string]::IsNullOrWhiteSpace($OutputManifest)) {
    throw 'Populate requires OutputManifest.'
}
if ($Mode -eq 'Verify' -and [string]::IsNullOrWhiteSpace($ManifestPath)) {
    throw 'Verify requires the canonical source ManifestPath.'
}

$plan = [ordered]@{
    schema = 'friday.secondary-source-volume-plan.v1'
    mode = $Mode
    apply = [bool]$Apply
    downloader_image = $DownloaderImage
    volume = $VolumeName
    repository = $expectedRepository
    revision = $expectedRevision
    manifest_raw_sha256 = $expectedManifestSha256
    manifest_semantic_sha256 = $expectedManifestSemanticSha256
    file_count = 12
    total_bytes = [int64]13789257124
    public_source = $true
    token_used = $false
}
if (-not $Apply -or $Mode -eq 'Plan') {
    $plan | ConvertTo-Json -Depth 4
    return
}

function Invoke-DockerCapture([string[]]$Arguments) {
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = (Get-Command docker.exe -ErrorAction Stop).Source
    $startInfo.Arguments = @($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + $_.Replace('"', '\"') + '"' } else { $_ }
    }) -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw 'Docker process could not start.'
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    [Threading.Tasks.Task]::WaitAll(@($stdoutTask, $stderrTask))
    if ($process.ExitCode -ne 0) {
        throw ('Docker operation failed with exit code ' + $process.ExitCode + '.')
    }
    return $stdoutTask.Result
}

function Get-TextSha256([string]$Text) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
        return ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $hasher.Dispose()
    }
}

function Write-NewUtf8File([string]$Path, [string]$Text) {
    $parent = Split-Path -Parent $Path
    if ([string]::IsNullOrWhiteSpace($parent) -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'Manifest output parent directory is absent.'
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $stream = [IO.FileStream]::new(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
}

function Test-DockerVolumeExists([string]$Name) {
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # A missing volume is the expected Populate state. Windows PowerShell
        # 5 can promote docker's stderr NativeCommandError while the enclosing
        # script uses Stop, so capture the native exit code under Continue.
        $ErrorActionPreference = 'Continue'
        & docker volume inspect $Name 1> $null 2> $null
        $inspectExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($inspectExitCode -eq 0) {
        return $true
    }
    if ($inspectExitCode -eq 1) {
        return $false
    }
    throw 'Docker volume existence check failed unexpectedly.'
}

Invoke-DockerCapture @('version', '--format', '{{.Server.Version}}') | Out-Null
$downloaderInspection = (
    Invoke-DockerCapture @('image', 'inspect', '--format', '{{json .}}', $DownloaderImage)
) | ConvertFrom-Json
if ([string]$downloaderInspection.Id -cne $expectedDownloaderImageId -or
    @($downloaderInspection.RepoDigests | Where-Object { [string]$_ -ceq $expectedDownloaderImage }).Count -ne 1 -or
    [string]$downloaderInspection.Descriptor.digest -cne $expectedDownloaderManifest -or
    [string]$downloaderInspection.Descriptor.mediaType -cne $expectedDownloaderMediaType -or
    [string]$downloaderInspection.Os -cne 'linux' -or
    [string]$downloaderInspection.Architecture -cne 'amd64') {
    throw 'Local downloader image identity/platform differs from the exact contract.'
}

$toolPath = (Get-Item -LiteralPath (Join-Path $PSScriptRoot 'model_volume_tool.py')).FullName
$toolMount = ('type=bind,source={0},target=/bundle/model_volume_tool.py,readonly' -f $toolPath)

if ($Mode -eq 'Populate') {
    if (Test-DockerVolumeExists $VolumeName) {
        throw 'Population refuses an existing volume.'
    }
    $resolvedOutput = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputManifest)
    if (Test-Path -LiteralPath $resolvedOutput) {
        throw 'Population refuses to overwrite an existing manifest copy.'
    }
    $outputParent = Split-Path -Parent $resolvedOutput
    if (-not (Test-Path -LiteralPath $outputParent -PathType Container)) {
        throw 'Manifest output parent directory is absent.'
    }

    Invoke-DockerCapture @(
        'volume', 'create',
        '--label', 'com.friday.role=optional-secondary-source-model',
        '--label', ('com.friday.model-revision=' + $expectedRevision),
        '--label', ('com.friday.source-manifest-sha256=' + $expectedManifestSha256),
        $VolumeName
    ) | Out-Null
    $arguments = @(
        'run', '--rm', '--pull', 'never', '--network', 'bridge',
        '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true',
        '--tmpfs', '/tmp:size=1g,mode=1777',
        '--env', 'HF_HOME=/tmp/huggingface',
        '--env', 'HF_HUB_ENABLE_HF_TRANSFER=0',
        '--mount', ('type=volume,source={0},target=/volume' -f $VolumeName),
        '--mount', $toolMount,
        '--entrypoint', 'python3',
        $DownloaderImage, '/bundle/model_volume_tool.py', 'download',
        '--volume', '/volume'
    )
    try {
        $manifestText = (Invoke-DockerCapture $arguments).TrimEnd("`r", "`n") + "`n"
        if ((Get-TextSha256 $manifestText) -cne $expectedManifestSha256) {
            throw 'Populated source manifest output identity is invalid.'
        }
        $verificationText = Invoke-DockerCapture @(
            'run', '--rm', '--pull', 'never', '--network', 'none',
            '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true',
            '--tmpfs', '/tmp:size=64m,mode=1777',
            '--env', 'HF_HUB_OFFLINE=1',
            '--mount', ('type=volume,source={0},target=/volume,readonly' -f $VolumeName),
            '--mount', $toolMount,
            '--entrypoint', 'python3',
            $DownloaderImage, '/bundle/model_volume_tool.py', 'verify',
            '--source', '/volume'
        )
        $verification = $verificationText | ConvertFrom-Json
        if (
            $verification.status -cne 'passed' -or
            $verification.manifest_raw_sha256 -cne $expectedManifestSha256 -or
            $verification.manifest_semantic_sha256 -cne $expectedManifestSemanticSha256 -or
            [int]$verification.file_count -ne 12 -or
            [int64]$verification.total_bytes -ne 13789257124
        ) {
            throw 'Offline verification returned an unexpected source receipt.'
        }
        Write-NewUtf8File $resolvedOutput $manifestText
    } catch {
        throw 'Pinned abliterated-source population failed; the candidate volume was retained for inspection.'
    }
    [ordered]@{
        schema = 'friday.secondary-source-volume-population.v1'
        status = 'verified'
        volume = $VolumeName
        repository = $expectedRepository
        revision = $expectedRevision
        manifest_path = $resolvedOutput
        manifest_raw_sha256 = $expectedManifestSha256
        file_count = 12
        total_bytes = [int64]13789257124
        offline_verified = $true
        token_used = $false
    } | ConvertTo-Json -Depth 4
    return
}

if (-not (Test-DockerVolumeExists $VolumeName)) {
    throw 'Canonical source volume is absent.'
}
$resolvedManifest = (Get-Item -LiteralPath $ManifestPath).FullName
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedManifest).Hash.ToLowerInvariant() -cne $expectedManifestSha256) {
    throw 'External source manifest copy is not byte-identical to the canonical manifest.'
}
$manifestMount = ('type=bind,source={0},target=/bundle/source-model.verified.json,readonly' -f $resolvedManifest)
$verificationJson = Invoke-DockerCapture @(
    'run', '--rm', '--pull', 'never', '--network', 'none',
    '--read-only', '--cap-drop', 'ALL',
    '--security-opt', 'no-new-privileges:true',
    '--tmpfs', '/tmp:size=64m,mode=1777',
    '--env', 'HF_HUB_OFFLINE=1',
    '--mount', ('type=volume,source={0},target=/volume,readonly' -f $VolumeName),
    '--mount', $toolMount,
    '--mount', $manifestMount,
    '--entrypoint', 'python3',
    $DownloaderImage, '/bundle/model_volume_tool.py', 'verify',
    '--source', '/volume',
    '--manifest', '/bundle/source-model.verified.json'
)
$verification = $verificationJson | ConvertFrom-Json
if (
    $verification.status -cne 'passed' -or
    $verification.manifest_raw_sha256 -cne $expectedManifestSha256 -or
    $verification.manifest_semantic_sha256 -cne $expectedManifestSemanticSha256 -or
    [int]$verification.file_count -ne 12 -or
    [int64]$verification.total_bytes -ne 13789257124
) {
    throw 'Model volume does not match the canonical abliterated-source manifest.'
}
$verificationJson
