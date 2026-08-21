#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

usage() {
    cat <<'EOF'
Usage:
  Sync-Qwen38V12AttestedBundle.sh --phase bootstrap [--remote-preflight|--execute]
  Sync-Qwen38V12AttestedBundle.sh --phase full [--remote-preflight|--execute]

With neither mutation flag, validates the frozen local bytes and pinned SSH
identity, prints the exact plan, and makes no network connection.

--remote-preflight  stage and CAS-check remotely without changing live bundle files
--execute           stage, CAS-check, and atomically apply the selected phase
EOF
}

phase=''
mode='plan'
while (($#)); do
    case "$1" in
        --phase)
            (($# >= 2)) || { usage >&2; exit 64; }
            phase=$2
            shift 2
            ;;
        --remote-preflight)
            [[ $mode == plan ]] || { usage >&2; exit 64; }
            mode='remote-preflight'
            shift
            ;;
        --execute)
            [[ $mode == plan ]] || { usage >&2; exit 64; }
            mode='execute'
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 64
            ;;
    esac
done
[[ $phase == bootstrap || $phase == full ]] || { usage >&2; exit 64; }

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
remote_dir=$script_dir/remote
transport_dir=$script_dir/transport
manifest=$transport_dir/TRANSPORT-FILES.v1
applier=$transport_dir/Apply-Qwen38V12AttestedBundle.ps1
replace_test=$transport_dir/Test-WindowsPowerShell51FileReplace.ps1

remote_host='192.168.1.78'
remote_user='admin'
ssh_key='/home/jericho/.ssh/friday_win_audit_ed25519'
known_hosts='/home/jericho/.ssh/known_hosts'
expected_client_fingerprint='SHA256:vhJUpURIJLODWZdo8LU8qnTMbLir86/J5tzl8VWp5+A'
expected_host_fingerprint='SHA256:wfOf57TOtNhTuQ6OAQUcWhMF47C8FWeUhku2gSAe6mY'
expected_manifest_sha256='8c6f87adb8da0009a2489552587a079d14479cd7ec08e062dbcedd0870a99565'
expected_applier_sha256='fd76ee17cb2f2d63eefc74c59fc9b0e5ebe079891372407836cb1b5a0a84d0f2'
expected_replace_test_sha256='64f58fbaeab76ea92308aaf4f93123631684529160d3eccbf3fa1e431776d7c7'

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

for command_name in ssh ssh-keygen iconv base64 sha256sum zip awk cut mktemp rm dirname cat stat; do
    command -v "$command_name" >/dev/null || fail "required command is absent: $command_name"
done
[[ -f $manifest && ! -L $manifest ]] || fail 'transport manifest is absent or a symlink'
[[ -f $applier && ! -L $applier ]] || fail 'transport applier is absent or a symlink'
[[ -f $replace_test && ! -L $replace_test ]] ||
    fail 'native File.Replace test is absent or a symlink'
[[ -f $ssh_key && ! -L $ssh_key ]] || fail 'pinned SSH private key is absent or a symlink'
[[ -f $known_hosts && ! -L $known_hosts ]] || fail 'pinned known_hosts is absent or a symlink'

actual_manifest_sha256=$(sha256sum -- "$manifest" | awk '{print $1}')
[[ $actual_manifest_sha256 == "$expected_manifest_sha256" ]] ||
    fail 'transport manifest is not the frozen byte set'
actual_applier_sha256=$(sha256sum -- "$applier" | awk '{print $1}')
[[ $actual_applier_sha256 == "$expected_applier_sha256" ]] ||
    fail 'transport applier is not the frozen byte set'
actual_replace_test_sha256=$(sha256sum -- "$replace_test" | awk '{print $1}')
[[ $actual_replace_test_sha256 == "$expected_replace_test_sha256" ]] ||
    fail 'native File.Replace test is not the frozen byte set'

ssh_args=(
    -F /dev/null
    -i "$ssh_key"
    -o BatchMode=yes
    -o ConnectTimeout=10
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$known_hosts"
    -o GlobalKnownHostsFile=/dev/null
    -o HostKeyAlgorithms=ssh-ed25519
    -o UpdateHostKeys=no
    -o IdentitiesOnly=yes
    -o PasswordAuthentication=no
    -o KbdInteractiveAuthentication=no
    -o PreferredAuthentications=publickey
    -o ProxyCommand=none
    -o ProxyJump=none
)
remote_target=$remote_user@$remote_host

declare -A expected_roles=(
    [AttestedBundle.Common.ps1]=bootstrap
    [Cleanup-StoppedQwen38V12Attested.ps1]=bootstrap
    [docker-compose.publish-8001.yml]=bootstrap
    [CORE-SHA256SUMS]=full
    [ORCHESTRATION-SHA256SUMS]=full
    [ORCHESTRATION.md]=full
    [README.md]=full
    [Rollback-Qwen38V12Attested.ps1]=full
    [Switch-Qwen38V12Attested.ps1]=full
    [Test-AttestedCleanupProjection.ps1]=full
    [Test-AttestedNetworkProjection.ps1]=full
    [Test-AttestedReceiptSerialization.ps1]=full
)
declare -A seen=()
selected_files=()
row_count=0
while IFS=' ' read -r role old_sha256 new_sha256 name extra; do
    [[ -n $role && -z ${extra:-} ]] || fail 'transport manifest field count is not exact'
    [[ $role == bootstrap || $role == full ]] || fail 'transport manifest phase is invalid'
    [[ $old_sha256 == ABSENT || $old_sha256 =~ ^[0-9a-f]{64}$ ]] ||
        fail 'transport manifest old hash is invalid'
    [[ $new_sha256 =~ ^[0-9a-f]{64}$ ]] || fail 'transport manifest new hash is invalid'
    [[ $name =~ ^[A-Za-z0-9._-]+$ ]] || fail 'transport manifest target name is invalid'
    [[ -n ${expected_roles[$name]+present} && ${expected_roles[$name]} == "$role" ]] ||
        fail "transport manifest target or role is not allowlisted: $name"
    [[ -z ${seen[$name]+present} ]] || fail "duplicate transport manifest target: $name"
    seen[$name]=1
    ((row_count += 1))

    source_path=$remote_dir/$name
    [[ -f $source_path && ! -L $source_path ]] || fail "frozen source is absent or a symlink: $name"
    actual_source_sha256=$(sha256sum -- "$source_path" | awk '{print $1}')
    [[ $actual_source_sha256 == "$new_sha256" ]] || fail "frozen source hash changed: $name"
    if [[ $phase == full || $role == bootstrap ]]; then
        selected_files+=("$source_path")
    fi
done < "$manifest"
[[ $row_count -eq ${#expected_roles[@]} ]] || fail 'transport manifest row count is not exact'
for name in "${!expected_roles[@]}"; do
    [[ -n ${seen[$name]+present} ]] || fail "transport manifest omits target: $name"
done
if [[ $phase == bootstrap ]]; then
    [[ ${#selected_files[@]} -eq 3 ]] || fail 'bootstrap source count is not exact'
else
    [[ ${#selected_files[@]} -eq 12 ]] || fail 'full source count is not exact'
fi

client_fingerprint=$(ssh-keygen -lf "$ssh_key" -E sha256 | awk 'NR == 1 {print $2}')
[[ $client_fingerprint == "$expected_client_fingerprint" ]] ||
    fail 'SSH client key fingerprint is not exact'
known_host_records=$(ssh-keygen -F "$remote_host" -f "$known_hosts") ||
    fail 'pinned Windows host is absent from known_hosts'
mapfile -t ed25519_fingerprints < <(
    printf '%s\n' "$known_host_records" |
        ssh-keygen -lf - -E sha256 |
        awk '$4 == "(ED25519)" {print $2}'
)
[[ ${#ed25519_fingerprints[@]} -eq 1 &&
    ${ed25519_fingerprints[0]} == "$expected_host_fingerprint" ]] ||
    fail 'Windows ED25519 host fingerprint is not exact'

effective_ssh=$(ssh -G "${ssh_args[@]}" "$remote_target" 2>/dev/null) ||
    fail 'could not project the effective SSH configuration'
assert_effective_ssh() {
    local key=$1
    local expected=$2
    local actual
    actual=$(awk -v wanted="$key" '$1 == wanted { $1 = ""; sub(/^ /, ""); print }' \
        <<< "$effective_ssh")
    [[ $actual == "$expected" ]] ||
        fail "effective SSH contract changed: $key=$actual"
}
assert_effective_ssh host 192.168.1.78
assert_effective_ssh hostname 192.168.1.78
assert_effective_ssh user admin
assert_effective_ssh port 22
assert_effective_ssh batchmode yes
assert_effective_ssh connecttimeout 10
assert_effective_ssh stricthostkeychecking true
assert_effective_ssh userknownhostsfile /home/jericho/.ssh/known_hosts
assert_effective_ssh globalknownhostsfile /dev/null
assert_effective_ssh hostkeyalgorithms ssh-ed25519
assert_effective_ssh updatehostkeys false
assert_effective_ssh identitiesonly yes
assert_effective_ssh passwordauthentication no
assert_effective_ssh kbdinteractiveauthentication no
assert_effective_ssh preferredauthentications publickey
assert_effective_ssh identityfile /home/jericho/.ssh/friday_win_audit_ed25519

printf 'phase=%s mode=%s target=%s@%s:22 files=%d\n' \
    "$phase" "$mode" "$remote_user" "$remote_host" "${#selected_files[@]}"
printf 'client_fingerprint=%s host_fingerprint=%s\n' \
    "$client_fingerprint" "$expected_host_fingerprint"
if [[ $mode == plan ]]; then
    printf 'network_connection=false mutation_authorized=false\n'
    exit 0
fi

temporary_root=$(mktemp -d /tmp/friday-qwen38-sync.XXXXXXXX)
cleanup_temporary_root() {
    [[ $temporary_root == /tmp/friday-qwen38-sync.* ]] || return 1
    rm -rf -- "$temporary_root"
}
trap cleanup_temporary_root EXIT
archive_path=$temporary_root/qwen38-v12-attested-$phase.zip
zip -q -X -j "$archive_path" "$applier" "$manifest" "$replace_test" \
    "${selected_files[@]}"
archive_sha256=$(sha256sum -- "$archive_path" | awk '{print $1}')
archive_size=$(stat -c '%s' -- "$archive_path")
maximum_archive_bytes=8388608
[[ $archive_size =~ ^[0-9]+$ && $archive_size -ge 1 &&
    $archive_size -le $maximum_archive_bytes ]] ||
    fail 'transport archive size is outside the bounded receiver contract'
session_id=$(printf '%s' "$archive_sha256:$phase:$mode:$temporary_root" | sha256sum | cut -c1-32)

receiver_source=$(cat <<PS
\$ErrorActionPreference='Stop'
\$syncRoot='D:\jarvis-gpt\qwen38-v12-attested-sync'
[IO.Directory]::CreateDirectory(\$syncRoot)|Out-Null
\$si=Get-Item -LiteralPath \$syncRoot -Force
if((\$si.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Sync root is a reparse point'}
\$root=Join-Path \$syncRoot 'incoming'
[IO.Directory]::CreateDirectory(\$root)|Out-Null
\$ri=Get-Item -LiteralPath \$root -Force
if((\$ri.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Incoming root is a reparse point'}
\$sha='$archive_sha256'
\$expected=[int64]$archive_size
\$cap=[int64]$maximum_archive_bytes
if(\$expected-lt 1-or \$expected-gt \$cap){throw 'Archive size is outside the receiver cap'}
\$dst=Join-Path \$root (\$sha+'.zip')
\$tmp=Join-Path \$root ('.'+\$sha+'.$session_id.partial')
try{
  \$stdinStream=[Console]::OpenStandardInput()
  \$output=[IO.File]::Open(\$tmp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
  \$buffer=New-Object byte[] 65536
  \$remaining=\$expected
  \$deadline=[DateTime]::UtcNow.AddSeconds(120)
  try{
    while(\$remaining-gt 0){
      \$now=[DateTime]::UtcNow
      if(\$now-ge \$deadline){throw 'Archive receive deadline expired'}
      \$wait=[int][Math]::Min(10000,[Math]::Max(1,(\$deadline-\$now).TotalMilliseconds))
      \$wanted=[int][Math]::Min([int64]\$buffer.Length,\$remaining)
      \$readTask=\$stdinStream.ReadAsync(\$buffer,0,\$wanted)
      if(-not \$readTask.Wait(\$wait)){throw 'Archive stdin read timed out'}
      \$read=[int]\$readTask.Result
      if(\$read-le 0){throw 'Archive stdin ended before the exact byte count'}
      \$output.Write(\$buffer,0,\$read)
      \$remaining-=\$read
    }
    \$output.Flush(\$true)
  }finally{\$output.Dispose();\$stdinStream.Dispose()}
  if((Get-Item -LiteralPath \$tmp -Force).Length-ne \$expected){throw 'Staged archive size changed'}
  if((Get-FileHash -LiteralPath \$tmp -Algorithm SHA256).Hash.ToLowerInvariant()-cne \$sha){throw 'Staged archive hash changed'}
  if(Test-Path -LiteralPath \$dst -PathType Leaf){
    \$di=Get-Item -LiteralPath \$dst -Force
    if((\$di.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Existing staged archive is a reparse point'}
    if((Get-FileHash -LiteralPath \$dst -Algorithm SHA256).Hash.ToLowerInvariant()-cne \$sha){throw 'Existing staged archive is corrupt'}
  }else{[IO.File]::Move(\$tmp,\$dst)}
}finally{if(Test-Path -LiteralPath \$tmp -PathType Leaf){Remove-Item -LiteralPath \$tmp -Force}}
[pscustomobject][ordered]@{schema='friday.attested-bundle-stage.v1';archive_sha256=\$sha;archive_size=\$expected;status='staged'}|ConvertTo-Json -Compress -Depth 4
PS
)
receiver_encoded=$(printf '%s' "$receiver_source" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
[[ ${#receiver_encoded} -le 7600 ]] || fail 'bounded receiver exceeds the remote command-line budget'
ssh "${ssh_args[@]}" "$remote_target" \
    "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $receiver_encoded" \
    < "$archive_path"

execute_argument=''
if [[ $mode == execute ]]; then
    execute_argument='-Execute'
fi
apply_source=$(cat <<PS
\$ErrorActionPreference='Stop'
\$sha='$archive_sha256'
\$syncRoot='D:\jarvis-gpt\qwen38-v12-attested-sync'
\$si=Get-Item -LiteralPath \$syncRoot -Force
if((\$si.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Sync root is a reparse point'}
\$incoming=Join-Path \$syncRoot 'incoming'
\$ii=Get-Item -LiteralPath \$incoming -Force
if((\$ii.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Incoming root is a reparse point'}
\$archive=Join-Path \$incoming (\$sha+'.zip')
\$ai=Get-Item -LiteralPath \$archive -Force
if((\$ai.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Staged archive is a reparse point'}
if((Get-FileHash -LiteralPath \$archive -Algorithm SHA256).Hash.ToLowerInvariant()-cne \$sha){throw 'Staged archive hash changed before expansion'}
\$expandedParent=Join-Path \$syncRoot 'expanded'
[IO.Directory]::CreateDirectory(\$expandedParent)|Out-Null
\$ei=Get-Item -LiteralPath \$expandedParent -Force
if((\$ei.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Expanded root is a reparse point'}
\$expanded=Join-Path \$expandedParent (\$sha+'-$session_id')
if(Test-Path -LiteralPath \$expanded){throw 'Expanded session path already exists'}
[IO.Directory]::CreateDirectory(\$expanded)|Out-Null
Expand-Archive -LiteralPath \$archive -DestinationPath \$expanded
\$manifest=Join-Path \$expanded 'TRANSPORT-FILES.v1'
\$applier=Join-Path \$expanded 'Apply-Qwen38V12AttestedBundle.ps1'
\$replaceTest=Join-Path \$expanded 'Test-WindowsPowerShell51FileReplace.ps1'
if((Get-FileHash -LiteralPath \$manifest -Algorithm SHA256).Hash.ToLowerInvariant()-cne '$actual_manifest_sha256'){throw 'Expanded transport manifest is not frozen'}
if((Get-FileHash -LiteralPath \$applier -Algorithm SHA256).Hash.ToLowerInvariant()-cne '$actual_applier_sha256'){throw 'Expanded transport applier is not frozen'}
if((Get-FileHash -LiteralPath \$replaceTest -Algorithm SHA256).Hash.ToLowerInvariant()-cne '$actual_replace_test_sha256'){throw 'Expanded native File.Replace test is not frozen'}
& \$replaceTest -RequireWindowsPowerShell51
& \$applier -Phase '$phase' -SourceRoot \$expanded $execute_argument
PS
)
apply_encoded=$(printf '%s' "$apply_source" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
ssh "${ssh_args[@]}" "$remote_target" \
    "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $apply_encoded"
