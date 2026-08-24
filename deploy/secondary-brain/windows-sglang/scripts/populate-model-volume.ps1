[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Plan', 'Discover', 'Verify')]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$DownloaderImage,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$VolumeName,

    [Parameter()]
    [string]$TokenFile,

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

if ($DownloaderImage -notmatch '\A[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}\z') {
    throw 'DownloaderImage must be an exact lowercase repo@sha256 digest.'
}
if ($VolumeName -notmatch '\A[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\z') {
    throw 'VolumeName is not a bounded Docker volume name.'
}
if ($Mode -eq 'Discover' -and [string]::IsNullOrWhiteSpace($OutputManifest)) {
    throw 'Discover requires OutputManifest.'
}
if ($Mode -eq 'Verify' -and [string]::IsNullOrWhiteSpace($ManifestPath)) {
    throw 'Verify requires an accepted ManifestPath.'
}

$plan = [ordered]@{
    schema = 'friday.secondary-model-volume-plan.v1'
    mode = $Mode
    apply = [bool]$Apply
    downloader_image = $DownloaderImage
    volume = $VolumeName
    model_repository = 'shanjiaz/gpt-oss-20b-nvfp4-modelopt'
    model_revision = 'fb9848e169d5b38cbc00ecf3383283ea1fc33a21'
    token_supplied_by_file = -not [string]::IsNullOrWhiteSpace($TokenFile)
}
if (-not $Apply -or $Mode -eq 'Plan') {
    $plan | ConvertTo-Json -Depth 4
    return
}

& docker version --format '{{.Server.Version}}' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker engine is unavailable.'
}
& docker image inspect --format '{{.Id}}' $DownloaderImage | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Exact downloader image is not present locally; this script never pulls a mutable image.'
}

$toolPath = (Get-Item -LiteralPath (Join-Path $PSScriptRoot 'model_volume_tool.py')).FullName
$toolMount = ('type=bind,source={0},target=/bundle/model_volume_tool.py,readonly' -f $toolPath)

function Test-DockerVolumeExists([string]$Name) {
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # A missing volume is the expected Discover state. Windows PowerShell
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

if ($Mode -eq 'Discover') {
    if (Test-DockerVolumeExists $VolumeName) {
        throw 'Discovery refuses an existing volume.'
    }
    & docker volume create `
        --label 'com.friday.role=optional-secondary-model-candidate' `
        --label 'com.friday.model-revision=fb9848e169d5b38cbc00ecf3383283ea1fc33a21' `
        $VolumeName | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create the candidate model volume.'
    }
    $arguments = @(
        'run', '--rm', '--network', 'bridge',
        '--mount', ('type=volume,source={0},target=/volume' -f $VolumeName),
        '--mount', $toolMount,
        '--entrypoint', 'python3'
    )
    if (-not [string]::IsNullOrWhiteSpace($TokenFile)) {
        $resolvedToken = (Get-Item -LiteralPath $TokenFile).FullName
        $arguments += @('--mount', ('type=bind,source={0},target=/run/secrets/hf-token,readonly' -f $resolvedToken))
    }
    $arguments += @($DownloaderImage, '/bundle/model_volume_tool.py', 'download')
    if (-not [string]::IsNullOrWhiteSpace($TokenFile)) {
        $arguments += @('--token-file', '/run/secrets/hf-token')
    }
    $manifestJson = & docker @arguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Pinned-revision model discovery failed; the candidate volume was retained for inspection.'
    }
    $resolvedOutput = [IO.Path]::GetFullPath($OutputManifest)
    [IO.File]::WriteAllText($resolvedOutput, (($manifestJson -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
    [ordered]@{
        schema = 'friday.secondary-model-volume-discovery.v1'
        status = 'observed_unaccepted'
        volume = $VolumeName
        manifest_path = $resolvedOutput
    } | ConvertTo-Json -Depth 4
    return
}

$resolvedManifest = (Get-Item -LiteralPath $ManifestPath).FullName
$manifestMount = ('type=bind,source={0},target=/bundle/accepted-model-manifest.json,readonly' -f $resolvedManifest)
$verificationJson = & docker run --rm --network none `
    --mount ('type=volume,source={0},target=/volume,readonly' -f $VolumeName) `
    --mount $toolMount `
    --mount $manifestMount `
    --entrypoint python3 `
    $DownloaderImage /bundle/model_volume_tool.py verify `
    --manifest /bundle/accepted-model-manifest.json
if ($LASTEXITCODE -ne 0) {
    throw 'Model volume does not match the accepted manifest.'
}
$verificationJson
