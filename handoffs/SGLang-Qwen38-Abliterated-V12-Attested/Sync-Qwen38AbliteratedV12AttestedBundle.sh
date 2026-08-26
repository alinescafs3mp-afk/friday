#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

usage() {
    cat <<'EOF'
Usage:
  Sync-Qwen38AbliteratedV12AttestedBundle.sh [--remote-preflight|--execute]

With no mutation flag, validate the exact local payload and pinned SSH identity,
print the plan, and make no network connection. The transport creates only a
previously absent isolated bundle root; it never patches an existing root.
EOF
}

mode=plan
while (($#)); do
    case "$1" in
        --remote-preflight)
            [[ $mode == plan ]] || { usage >&2; exit 64; }
            mode=remote-preflight
            shift
            ;;
        --execute)
            [[ $mode == plan ]] || { usage >&2; exit 64; }
            mode=execute
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

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
remote_dir=$script_dir/remote
transport_dir=$script_dir/transport
manifest=$transport_dir/TRANSPORT-FILES.v1
applier=$transport_dir/Apply-Qwen38AbliteratedV12AttestedBundle.ps1

remote_host=192.168.1.78
remote_user=admin
ssh_key=/home/jericho/.ssh/friday_win_audit_ed25519
known_hosts=/home/jericho/.ssh/known_hosts
expected_client_fingerprint='SHA256:vhJUpURIJLODWZdo8LU8qnTMbLir86/J5tzl8VWp5+A'
expected_host_fingerprint='SHA256:wfOf57TOtNhTuQ6OAQUcWhMF47C8FWeUhku2gSAe6mY'
expected_manifest_sha256='7c8988e069f0558f75862b8e19a9267d085bb19d44d761ff449d6605eb55cb87'
expected_applier_sha256='2b5e4b32541c9a5c2e59f2b13dcfd07b15a3ad27a3d576dd28dda7c4dcf36bdb'
expected_payload_count=31

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

for command_name in ssh ssh-keygen iconv base64 sha256sum zip awk cut mktemp rm dirname stat find wc cat; do
    command -v "$command_name" >/dev/null || fail "required command is absent: $command_name"
done
[[ -f $manifest && ! -L $manifest ]] || fail 'transport manifest is absent or a symlink'
[[ -f $applier && ! -L $applier ]] || fail 'transport applier is absent or a symlink'
[[ -f $ssh_key && ! -L $ssh_key ]] || fail 'pinned SSH private key is absent or a symlink'
[[ -f $known_hosts && ! -L $known_hosts ]] || fail 'pinned known_hosts is absent or a symlink'

actual_manifest_sha256=$(sha256sum -- "$manifest" | awk '{print $1}')
[[ $actual_manifest_sha256 == "$expected_manifest_sha256" ]] ||
    fail 'transport manifest is not the frozen byte set'
actual_applier_sha256=$(sha256sum -- "$applier" | awk '{print $1}')
[[ $actual_applier_sha256 == "$expected_applier_sha256" ]] ||
    fail 'transport applier is not the frozen byte set'

declare -A seen=()
payloads=()
row_count=0
while IFS= read -r line; do
    [[ $line =~ ^([0-9a-f]{64})\ \ ([A-Za-z0-9._-]+)$ ]] ||
        fail 'transport manifest row is not canonical'
    sha256=${BASH_REMATCH[1]}
    name=${BASH_REMATCH[2]}
    [[ -z ${seen[$name]+present} ]] || fail "duplicate transport target: $name"
    seen[$name]=1
    ((row_count += 1))
    source_path=$remote_dir/$name
    [[ -f $source_path && ! -L $source_path ]] || fail "payload is absent or a symlink: $name"
    actual=$(sha256sum -- "$source_path" | awk '{print $1}')
    [[ $actual == "$sha256" ]] || fail "payload hash changed: $name"
    payloads+=("$source_path")
done < "$manifest"
[[ $row_count -eq $expected_payload_count ]] || fail 'transport payload count is not exact'

while IFS= read -r source_path; do
    name=${source_path##*/}
    [[ -n ${seen[$name]+present} ]] || fail "remote payload is omitted from transport: $name"
done < <(find "$remote_dir" -maxdepth 1 -type f -print | LC_ALL=C sort)
[[ $(find "$remote_dir" -maxdepth 1 -type f | wc -l) -eq $expected_payload_count ]] ||
    fail 'remote payload directory count is not exact'

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

client_fingerprint=$(ssh-keygen -lf "$ssh_key" -E sha256 | awk 'NR == 1 {print $2}')
[[ $client_fingerprint == "$expected_client_fingerprint" ]] ||
    fail 'SSH client key fingerprint is not exact'
known_host_records=$(ssh-keygen -F "$remote_host" -f "$known_hosts") ||
    fail 'pinned Windows host is absent from known_hosts'
mapfile -t host_fingerprints < <(
    printf '%s\n' "$known_host_records" |
        ssh-keygen -lf - -E sha256 |
        awk '$4 == "(ED25519)" {print $2}'
)
[[ ${#host_fingerprints[@]} -eq 1 &&
    ${host_fingerprints[0]} == "$expected_host_fingerprint" ]] ||
    fail 'Windows ED25519 host fingerprint is not exact'

effective_ssh=$(ssh -G "${ssh_args[@]}" "$remote_target" 2>/dev/null) ||
    fail 'could not project the effective SSH configuration'
assert_effective_ssh() {
    local key=$1 expected=$2 actual
    actual=$(awk -v wanted="$key" '$1 == wanted { $1 = ""; sub(/^ /, ""); print }' \
        <<< "$effective_ssh")
    [[ $actual == "$expected" ]] || fail "effective SSH contract changed: $key=$actual"
}
assert_effective_ssh host 192.168.1.78
assert_effective_ssh hostname 192.168.1.78
assert_effective_ssh user admin
assert_effective_ssh port 22
assert_effective_ssh batchmode yes
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

printf 'mode=%s target=%s@%s:22 payloads=%d create_new_only=true\n' \
    "$mode" "$remote_user" "$remote_host" "${#payloads[@]}"
printf 'client_fingerprint=%s host_fingerprint=%s\n' \
    "$client_fingerprint" "$expected_host_fingerprint"
if [[ $mode == plan ]]; then
    printf 'network_connection=false mutation_authorized=false\n'
    exit 0
fi

temporary_root=$(mktemp -d /tmp/friday-qwen38-ablit-sync.XXXXXXXX)
cleanup_temporary_root() {
    [[ $temporary_root == /tmp/friday-qwen38-ablit-sync.* ]] || return 1
    rm -rf -- "$temporary_root"
}
trap cleanup_temporary_root EXIT
archive_path=$temporary_root/qwen38-abliterated-v12-attested.zip
zip -q -X -j "$archive_path" "$applier" "$manifest" "${payloads[@]}"
archive_sha256=$(sha256sum -- "$archive_path" | awk '{print $1}')
archive_size=$(stat -c '%s' -- "$archive_path")
maximum_archive_bytes=8388608
[[ $archive_size =~ ^[0-9]+$ && $archive_size -ge 1 &&
    $archive_size -le $maximum_archive_bytes ]] ||
    fail 'transport archive size is outside the bounded receiver contract'
session_id=$(printf '%s' "$archive_sha256:$mode:$temporary_root" | sha256sum | cut -c1-32)

receiver_source=$(cat <<PS
\$ErrorActionPreference='Stop'
\$syncRoot='D:\jarvis-gpt\qwen38-abliterated-v12-attested-sync'
[IO.Directory]::CreateDirectory(\$syncRoot)|Out-Null
\$incoming=Join-Path \$syncRoot 'incoming'
[IO.Directory]::CreateDirectory(\$incoming)|Out-Null
foreach(\$path in @(\$syncRoot,\$incoming)){
  \$item=Get-Item -LiteralPath \$path -Force
  if((\$item.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Sync path is a reparse point'}
}
\$sha='$archive_sha256'
\$expected=[int64]$archive_size
\$cap=[int64]$maximum_archive_bytes
if(\$expected-lt 1-or \$expected-gt \$cap){throw 'Archive size is outside the receiver cap'}
\$dst=Join-Path \$incoming (\$sha+'.zip')
\$tmp=Join-Path \$incoming ('.'+\$sha+'.$session_id.partial')
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
  if((Get-FileHash -LiteralPath \$tmp -Algorithm SHA256).Hash.ToLowerInvariant()-cne \$sha){throw 'Staged archive hash changed'}
  if(Test-Path -LiteralPath \$dst -PathType Leaf){
    if((Get-FileHash -LiteralPath \$dst -Algorithm SHA256).Hash.ToLowerInvariant()-cne \$sha){throw 'Existing staged archive is corrupt'}
    Remove-Item -LiteralPath \$tmp -Force
  }else{[IO.File]::Move(\$tmp,\$dst)}
}finally{if(Test-Path -LiteralPath \$tmp -PathType Leaf){Remove-Item -LiteralPath \$tmp -Force}}
[pscustomobject][ordered]@{status='staged';archive_sha256=\$sha;archive_size=\$expected}|ConvertTo-Json -Compress
PS
)
receiver_encoded=$(printf '%s' "$receiver_source" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
[[ ${#receiver_encoded} -le 7600 ]] || fail 'bounded receiver exceeds command-line budget'
ssh "${ssh_args[@]}" "$remote_target" \
    "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $receiver_encoded" \
    < "$archive_path"

execute_argument=
[[ $mode == execute ]] && execute_argument='-Execute'
apply_source=$(cat <<PS
\$ErrorActionPreference='Stop'
\$sha='$archive_sha256'
\$syncRoot='D:\jarvis-gpt\qwen38-abliterated-v12-attested-sync'
\$archive=Join-Path (Join-Path \$syncRoot 'incoming') (\$sha+'.zip')
if((Get-FileHash -LiteralPath \$archive -Algorithm SHA256).Hash.ToLowerInvariant()-cne \$sha){throw 'Staged archive hash changed before expansion'}
\$expandedParent=Join-Path \$syncRoot 'expanded'
[IO.Directory]::CreateDirectory(\$expandedParent)|Out-Null
\$expanded=Join-Path \$expandedParent (\$sha+'-$session_id')
if(Test-Path -LiteralPath \$expanded){throw 'Expanded session path already exists'}
[IO.Directory]::CreateDirectory(\$expanded)|Out-Null
Expand-Archive -LiteralPath \$archive -DestinationPath \$expanded
\$manifest=Join-Path \$expanded 'TRANSPORT-FILES.v1'
\$applier=Join-Path \$expanded 'Apply-Qwen38AbliteratedV12AttestedBundle.ps1'
if((Get-FileHash -LiteralPath \$manifest -Algorithm SHA256).Hash.ToLowerInvariant()-cne '$actual_manifest_sha256'){throw 'Expanded manifest is not frozen'}
if((Get-FileHash -LiteralPath \$applier -Algorithm SHA256).Hash.ToLowerInvariant()-cne '$actual_applier_sha256'){throw 'Expanded applier is not frozen'}
& \$applier -SourceRoot \$expanded $execute_argument
PS
)
apply_encoded=$(printf '%s' "$apply_source" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
ssh "${ssh_args[@]}" "$remote_target" \
    "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $apply_encoded"
