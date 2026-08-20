[CmdletBinding()]
param(
    [Parameter()]
    [string]$BundleRoot = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$expectedPolicy = 'd51c092ca2ef566f092ef9d55320e302c2d10b710d319d27a6d982aba018dcfe'
$stableProxyName = 'jarvis-gpt-sglang-qwen38-perf-graph-api'
$stableProxyImageId = 'sha256:dc73b49f5124cf2ee538dfbdfbd121f0b4ccdcb20fea30f3a81bd477c02e2bb5'
$root = if ($BundleRoot) {
    (Get-Item -LiteralPath $BundleRoot -Force).FullName
}
else {
    $PSScriptRoot
}

function Get-OneDockerObject([string[]]$Arguments, [string]$Label) {
    $raw = @(& docker @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0) { throw "$Label inspect failed" }
    $values = @($raw | Out-String | ConvertFrom-Json)
    if ($values.Count -ne 1) { throw "$Label inspect was not exact" }
    return $values[0]
}

function Get-WrongCaseKey([string]$Value) {
    $characters = $Value.ToCharArray()
    for ($index = 0; $index -lt $characters.Count; $index++) {
        if ([char]::IsLower($characters[$index])) {
            $characters[$index] = [char]::ToUpperInvariant($characters[$index])
            return (-join $characters)
        }
        if ([char]::IsUpper($characters[$index])) {
            $characters[$index] = [char]::ToLowerInvariant($characters[$index])
            return (-join $characters)
        }
    }
    throw 'Current API key has no letter for the wrong-case gate'
}

function Assert-NginxConfig([string]$ImageId, [string]$Key, [string]$Label) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & docker run --rm --network none --env "JARVIS_LLM_API_KEY=$Key" --env 'SGLANG_UPSTREAM=127.0.0.1' $ImageId nginx -t 2>&1 | Out-Null
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) { throw "$Label nginx configuration test failed" }
}

function Get-ProxyStatus(
    [string]$ContainerName,
    [string]$Path,
    [AllowNull()]
    [string]$Key
) {
    $arguments = @('exec', $ContainerName, 'wget', '-S', '-O', '/dev/null')
    if ($null -ne $Key) {
        $arguments += @('--header', "Authorization: Bearer $Key")
    }
    $arguments += "http://127.0.0.1:8080$Path"
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& docker @arguments 2>&1 | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    $matches = [regex]::Matches($output, 'HTTP/[0-9.]+\s+([0-9]{3})')
    if ($matches.Count -lt 1) { throw 'Proxy test did not return an HTTP status' }
    return [int]$matches[$matches.Count - 1].Groups[1].Value
}

$receiptPath = Join-Path $root 'build-attestation.v1.json'
$receipt = Get-Content -Raw -LiteralPath $receiptPath -Encoding utf8 | ConvertFrom-Json
if ([string]$receipt.schema -cne 'friday.attested-image-build.v1' -or
    [string]$receipt.proxy.proxy_policy_sha256 -cne $expectedPolicy -or
    [string]$receipt.proxy.image_id -notmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Proxy build receipt is not exact'
}
$proxyImageId = [string]$receipt.proxy.image_id
$proxyImage = Get-OneDockerObject @('image', 'inspect', $proxyImageId) 'proxy image'
if ([string]$proxyImage.Id -cne $proxyImageId -or
    [string]$proxyImage.Config.Labels.'com.friday.proxy-policy-sha256' -cne $expectedPolicy) {
    throw 'Proxy image identity or policy label is not exact'
}

$stableProxy = Get-OneDockerObject @('inspect', $stableProxyName) 'stable proxy'
if (-not [bool]$stableProxy.State.Running -or [string]$stableProxy.Image -cne $stableProxyImageId) {
    throw 'Stable proxy identity is not exact'
}
$prefix = 'JARVIS_LLM_API_KEY='
$keyEntries = @($stableProxy.Config.Env | Where-Object {
    ([string]$_).StartsWith($prefix, [StringComparison]::Ordinal)
})
if ($keyEntries.Count -ne 1) { throw 'Stable proxy API key binding is not exact' }
$apiKey = ([string]$keyEntries[0]).Substring($prefix.Length)
if ($apiKey -cnotmatch '^[A-Za-z0-9._~-]{32,256}$') {
    throw 'Stable proxy API key shape is invalid'
}
$wrongCaseKey = Get-WrongCaseKey $apiKey

Assert-NginxConfig $proxyImageId ('A' * 256) '256-character dummy key'
Assert-NginxConfig $proxyImageId $apiKey 'stable key'

$testName = 'friday-proxy-policy-test-' + [Guid]::NewGuid().ToString('N')
try {
    & docker run --detach --name $testName --network none --env "JARVIS_LLM_API_KEY=$apiKey" --env 'SGLANG_UPSTREAM=127.0.0.1' $proxyImageId | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Proxy test container creation failed' }

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $testContainer = Get-OneDockerObject @('inspect', $testName) 'proxy test container'
        if (-not [bool]$testContainer.State.Running) {
            throw 'Proxy test container exited before acceptance'
        }
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & docker exec $testName nginx -t 2>&1 | Out-Null
            $nginxExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($nginxExitCode -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($nginxExitCode -ne 0) { throw 'Proxy test container did not become ready' }

    & docker exec $testName sh -c 'mkdir -p /run/friday-witness && printf "%s\n" "{\"status\":\"ok\"}" > /run/friday-witness/deployment-witness.v1.json'
    if ($LASTEXITCODE -ne 0) { throw 'Proxy test witness creation failed' }

    if ((Get-ProxyStatus $testName '/_friday/v1/deployment-witness' $apiKey) -ne 200 -or
        (Get-ProxyStatus $testName '/_friday/v1/deployment-witness' $wrongCaseKey) -ne 401 -or
        (Get-ProxyStatus $testName '/_friday/v1/deployment-witness' 'definitely-wrong-key') -ne 401 -or
        (Get-ProxyStatus $testName '/_friday/v1/deployment-witness' $null) -ne 401 -or
        (Get-ProxyStatus $testName '/v1/files' $apiKey) -ne 404) {
        throw 'Proxy authorization or management-route matrix failed'
    }
}
finally {
    $apiKey = $null
    $wrongCaseKey = $null
    & docker rm --force $testName *> $null
}

[pscustomobject]@{
    schema = 'friday.attested-proxy-test.v1'
    proxy_image_id = $proxyImageId
    proxy_policy_sha256 = $expectedPolicy
    nginx_test_256_character_key = 'ok'
    nginx_test_stable_key = 'ok'
    exact_key_authorized = 'ok'
    wrong_case_rejected = 'ok'
    missing_key_rejected = 'ok'
    wrong_key_rejected = 'ok'
    management_route_closed = 'ok'
} | ConvertTo-Json -Compress
