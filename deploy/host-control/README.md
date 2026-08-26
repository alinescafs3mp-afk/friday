# Host Control deployment (Ubuntu, opt-in)

This directory installs the two host-side trust domains without changing the
running Friday backend. The backend feature, package installation and public
network access remain independently disabled until their reviewed
`FRIDAY_HOST_*` flags are set. Desktop control and one-shot execution are
reserved but unsupported in this release; setting either flag to `1` makes
startup validation fail closed.

The first release surface is intentionally small:

- `friday-host-agent` runs as the selected desktop user and accepts only the
  authenticated, typed host protocol over its Unix socket;
- each CLI action is delegated to a bounded transient `systemd --user` unit;
- `friday-package-broker` runs as root, is socket activated, and exposes only
  exact APT plan/execute/status operations;
- the shipped broker policy admits only the Ubuntu `nmap` package;
- GUI control and generic one-shot execution stay disabled and unsupported.

There is no host shell, passwordless sudo rule, Docker socket, system D-Bus
mount, or broad host filesystem mount in this deployment.

## Prerequisites

Use Ubuntu 24.04 LTS or newer with systemd, rootful Docker Compose, and a
dedicated non-root Friday desktop user with a private primary group.  Rootless
Docker remains supported by the base Friday deployment, but not by this optional
Unix-socket override: its subordinate-ID mapping cannot simultaneously preserve
the owner-only socket/key permissions and the exact host UID checked through
`SO_PEERCRED`.
Ubuntu 24.04 is the offline distro-package baseline because it provides Python
3.12 and `python3-cryptography` 41.0.7; both satisfy Friday's Python >=3.11 and
`cryptography>=41.0.7,<51` metadata. Bootstrap packages must already be installed
by the local operator:

```text
python3
python3-venv
python3-pip
python3-apt
python3-cryptography
bubblewrap
util-linux
```

The host agent treats Ubuntu's fixed `/usr/bin/bwrap` as a mandatory execution
boundary and its offline `--check-config` rejects a missing or unsafe binary
before service installation completes. The broker deliberately relies on
Ubuntu's `python3-apt`; the installer creates
its dedicated virtual environment with `--system-site-packages` and fails if
`apt`/`apt_pkg` cannot be imported. It also checks the interpreter and installed
cryptography version, then performs an in-memory Ed25519 generate/serialize/sign/
verify probe before installing Friday. The host never builds Friday from source.
The release environment must build the wheel with `setuptools>=77` and
`wheel>=0.45`, then use `tools/build_host_control_release_bundle.py` to bind that
wheel and the closed deployment file set to one clean exact Git commit. The
builder validates the wheel payload against Git `HEAD`, writes a deterministic
archive, an internal strict manifest and an external archive SHA-256 sidecar.
The archive and its expected digest must reach the operator over independent
authenticated release channels. The installer copies the verified wheel to a
root-owned temporary file, checks its exact digest, bounded archive layout,
license, packages and entrypoints, then uses pip with
`--no-index --no-deps --force-reinstall`. Setup cannot resolve or fetch Python
code from the network. Ubuntu 22.04's default Python and
`python3-cryptography` are below this supported offline baseline.

The override builds and runs the backend as the selected desktop user's exact
numeric UID and GID and sets `userns_mode: host`.  The process remains non-root,
with every capability dropped and `no-new-privileges`; the setting exists only
so the native agent observes that same UID through `SO_PEERCRED`.  The installer
derives the one allowed peer UID from `--user`; it never admits host root or an
unrelated numeric identity.

The socket directory stays user-owned `0700`, and both the socket and shared
HMAC key stay user-owned `0600`. Matching the owner UID lets the container
traverse/read them without adding a group member or broadening either mode.
Peer UID alone still grants nothing because every request also requires the
shared HMAC key.

The shipped deployment also pins `local-user-agent` as one exact agent identity
in both the root-owned user-service environment and the Compose backend. It is
not an operator interpolation value; changing only one side must fail the
authenticated handshake rather than silently select another agent.

Network authority is independently pinned in the root-owned
`/etc/friday-host-control/host-agent-policy.toml`.  The native agent reloads and
re-normalizes the exact targets from every network plan under that policy
immediately before execution. Its digest is carried by the signed handshake;
the backend refuses all Host Control capabilities when its
`FRIDAY_HOST_ALLOWED_CIDRS`/`FRIDAY_HOST_PUBLIC_NETWORK_ENABLED` identity does
not match. A backend process, even with the shared request HMAC, therefore
cannot widen the operator's host-side CIDR policy. The installer creates an
empty, public-disabled policy on first install and preserves later root edits.
If public scope is enabled later, the shared request HMAC and an approval ID are
not authorization: the backend must attach a short-lived Ed25519 proof bound to
the exact plan/job/actor/idempotency tuple. The native agent verifies and
durably consumes that proof, then verifies it again immediately before launch.
Private scans do not accept or require this proof.

The installer also renders `/etc/tmpfiles.d/friday-host-agent.conf`. It creates
the private `/run/friday-host-agent/<uid>` directory at boot independently of
the agent unit. Stopping the agent removes only `agent.sock`, not the bind-source
directory, so a backend start/restart remains fail-soft and reports the agent as
disconnected instead of failing Compose mount setup.

The Friday data directory passed below must already exist, be canonical (no
symlink component), and be owned by the selected desktop user. It must be the
same directory mounted at `/runtime/data` by base Compose (normally
`${FRIDAY_HOST_HOME}/data`); do not point the nested host-job mount at a second
data tree.

## Install

From a clean, versioned release commit, build the canonical wheel outside the
worktree and construct the closed bundle. Both `--wheel` and `--output` require
canonical absolute paths, and the output filename must carry the exact release
version:

```bash
.venv/bin/python -m build --wheel --outdir /srv/friday-release/wheel
.venv/bin/python tools/build_host_control_release_bundle.py build \
  --source-root "$PWD" \
  --wheel /srv/friday-release/wheel/friday-<VERSION>-py3-none-any.whl \
  --output /srv/friday-release/friday-host-control-<VERSION>.tar.gz
```

The builder refuses a dirty worktree, a wheel that differs from the exact Git
commit, an incomplete or extra deployment file, unsafe Git modes/types, and an
existing output. Repeating the build from the same inputs must produce identical
archive bytes. Publish the `.tar.gz` through the artifact channel and its
`.tar.gz.sha256` through a separate authenticated release channel.

On the Ubuntu host, first copy the archive into a root-owned, non-writable
staging directory. Obtain the 64-character archive digest independently; do not
derive trust from a sidecar downloaded beside the archive. Using the verifier
from a trusted checkout of the expected release commit, validate the archive
before extracting it:

```bash
sudo install -d -o root -g root -m 0700 /var/lib/friday-host-control-release
sudo install -o root -g root -m 0600 \
  /srv/incoming/friday-host-control-<VERSION>.tar.gz \
  /var/lib/friday-host-control-release/friday-host-control-<VERSION>.tar.gz
sudo /trusted/friday/.venv/bin/python \
  /trusted/friday/tools/build_host_control_release_bundle.py verify \
  --archive /var/lib/friday-host-control-release/friday-host-control-<VERSION>.tar.gz \
  --expected-sha256 <64-LOWERCASE-HEX-FROM-INDEPENDENT-CHANNEL>
sudo tar -xzf \
  /var/lib/friday-host-control-release/friday-host-control-<VERSION>.tar.gz \
  -C /var/lib/friday-host-control-release
```

The verifier checks the external archive digest, canonical compression/tar
encoding, closed member inventory, per-file manifest hashes/modes, canonical
wheel metadata and payload integrity without extracting attacker-selected
paths. Keep the verified archive root-owned between verification and extraction;
any replacement requires verification again.

Then run one explicit local operator setup command using only paths from that
verified bundle and the wheel digest printed by the verifier/manifest:

```bash
sudo /var/lib/friday-host-control-release/deploy/host-control/install.sh \
  --user friday \
  --friday-data-dir /srv/friday/data \
  --artifact-wheel /var/lib/friday-host-control-release/wheel/friday-<VERSION>-py3-none-any.whl \
  --artifact-sha256 <64-LOWERCASE-HEX-FROM-RELEASE-MANIFEST>
```

Add `--enable` only when the units should be enabled immediately. Without it,
the script installs and validates files but starts nothing.  To enable later:

```bash
FRIDAY_USER=friday
FRIDAY_UID="$(id -u -- "$FRIDAY_USER")"
FRIDAY_HOME="$(getent passwd "$FRIDAY_USER" | cut -d: -f6)"
sudo systemctl enable --now friday-package-broker.socket
sudo loginctl enable-linger "$FRIDAY_USER"
sudo systemctl start "user@$FRIDAY_UID.service"
sudo /usr/sbin/runuser -u "$FRIDAY_USER" -- /usr/bin/env -i \
  HOME="$FRIDAY_HOME" USER="$FRIDAY_USER" LOGNAME="$FRIDAY_USER" \
  PATH=/usr/bin:/bin XDG_RUNTIME_DIR="/run/user/$FRIDAY_UID" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$FRIDAY_UID/bus" \
  /usr/bin/systemctl --user daemon-reload
sudo /usr/sbin/runuser -u "$FRIDAY_USER" -- /usr/bin/env -i \
  HOME="$FRIDAY_HOME" USER="$FRIDAY_USER" LOGNAME="$FRIDAY_USER" \
  PATH=/usr/bin:/bin XDG_RUNTIME_DIR="/run/user/$FRIDAY_UID" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$FRIDAY_UID/bus" \
  /usr/bin/systemctl --user enable --now friday-host-agent.service
```

Linger keeps CLI jobs and reconciliation alive after logout.  The uninstall
script never disables linger because other user services may depend on it.

The installer is repeatable and refuses unsafe pre-existing keys, a shared
primary group, a symlinked/broad data path, a system-range user UID/GID, or a venv
without Ubuntu `python3-apt`. It also refuses a noncanonical/symlinked wheel,
wrong manifest digest, unsafe archive member, missing license/package/entrypoint,
or any attempt to build from source on the target. Existing policy and keys are
preserved rather than overwritten. User-owned state and key paths are created by
the selected user through descriptor-relative, no-symlink operations; root never
writes through a pathname below that user's home or data tree. Before any
root-owned virtualenv executable is run, the complete tree is attested as
root-owned and non-writable by group/others. Each artifact is installed into a
new permanent digest-versioned release directory, so generated console-script
shebangs never become stale. The binaries, package/configuration preflights and
all replacement unit/config files are complete before the root-owned `current`
activation symlink changes atomically. A first upgrade from the legacy fixed
`/opt/friday-host-control/venv` layout leaves that directory untouched as the
rollback source. After activation, any installer failure or handled signal
restores the previous activation, exact root-owned unit/config files and the
service enable/linger/start state changed by that run. Existing policy, secrets,
receipts, jobs and user data are never rolled back. Stop both host services
before a reinstall/upgrade; the installer refuses to replace executable code
while a job or socket may still be live. It derives the host-plane build identity
from the installed package version plus a digest of the installed host-control
Python sources, then writes that identity to the root-owned service
configuration. It creates independent secrets:

The installer publishes a single root-only recovery journal at
`/opt/friday-host-control/.install-transaction` before changing `current`.
Normal install/upgrade refuses to proceed while that journal exists. If the
process was terminated in a way that prevented its EXIT rollback, use the exact
same verified release bundle and the selected Friday user to complete the
fail-closed recovery; do not delete or edit the journal:

```bash
sudo /var/lib/friday-host-control-release/deploy/host-control/install.sh \
  --recover --user friday
```

Recovery validates the journal, restores the recorded activation, files and
service state without evaluating journal text, removes only the abandoned
candidate release, and then removes the journal. A malformed journal or changed
user UID is a terminal operator-visible refusal rather than permission to start
a new installation.

- `%h/.config/friday-host-agent/agent.key`, user-owned `0600`, shared read-only
  with only the backend container;
- `/etc/friday-host-control/broker.key`, root-owned `0640`, readable by the
  selected user's private primary group and never mounted into the backend.
- `/etc/friday-host-control/broker-signing.key`, a root-only `0600` Ed25519
  seed used only by the broker, plus a separately pinned group-readable raw
  public key for the host agent. The private seed never crosses that boundary.
- `/etc/friday-host-control/backend-approval-signing.key`, root-owned `0640`
  and readable only through the memberless `friday-host-approval` GID granted
  as a supplemental group to the backend container. Its root-owned, selected
  user-group-readable `0640` public key is pinned independently by both the root
  broker and native host agent; the private seed is explicitly inaccessible to
  the agent unit. The backend turns `PR_SET_DUMPABLE` off before opening the seed
  and fails closed if the kernel does not confirm that boundary.

## Compose opt-in

The installer prints the exact runtime identity, isolated signer GID and host paths needed by
the override.  Copy those lines unchanged into the same protected environment
used for Compose interpolation:

```dotenv
FRIDAY_HOST_RUNTIME_UID=1000
FRIDAY_HOST_RUNTIME_GID=1000
FRIDAY_HOST_AGENT_SOCKET_DIR_HOST=/run/friday-host-agent/1000
FRIDAY_HOST_AGENT_KEY_FILE_HOST=/home/friday/.config/friday-host-agent/agent.key
FRIDAY_HOST_APPROVAL_SIGNING_KEY_FILE_HOST=/etc/friday-host-control/backend-approval-signing.key
FRIDAY_HOST_APPROVAL_SIGNER_GID=987
FRIDAY_HOST_JOB_DATA_DIR_HOST=/srv/friday/data/host-control/jobs

# Every capability is still off until explicitly reviewed.
FRIDAY_HOST_CONTROL_ENABLED=0
FRIDAY_HOST_PACKAGE_INSTALL_ENABLED=0
# Reserved/unsupported in this release; startup requires both to remain 0.
FRIDAY_HOST_DESKTOP_CONTROL_ENABLED=0
FRIDAY_HOST_ONE_SHOT_EXEC_ENABLED=0
FRIDAY_HOST_PUBLIC_NETWORK_ENABLED=0
FRIDAY_HOST_ALLOWED_CIDRS=
FRIDAY_HOST_ALLOWED_PATH_ROOTS=
```

Render and inspect the merged configuration before starting it:

```bash
docker compose \
  -f docker-compose.yml \
  -f deploy/host-control/compose.override.yml \
  config
```

The override bind-mounts only the existing agent socket directory, the exact
host-job directory, the user-agent key as a read-only Compose secret, and the
exact root-owned approval seed as a read-only file. The latter remains unreadable
to ordinary processes of the selected host UID because only the container gets
the otherwise memberless supplemental signer GID. It
sets `create_host_path: false`, so a missing runtime directory is an error rather
than a root-owned replacement silently created by Docker.  `/usr`, `/etc`,
`/home`, `/run/user`, the Docker socket, the broker socket/key, host executables,
and system D-Bus are not mounted.

Start the backend with the same two files and force a build so the image's
`jericho` passwd entry receives the reviewed numeric identity:

```bash
docker compose \
  -f docker-compose.yml \
  -f deploy/host-control/compose.override.yml \
  up -d --build backend
```

Before enabling Host Control, prove that the running backend has the selected
host identity. Both comparisons must produce the same number on each side:

```bash
id -u friday
docker compose -f docker-compose.yml -f deploy/host-control/compose.override.yml \
  exec -T backend id -u
id -g friday
docker compose -f docker-compose.yml -f deploy/host-control/compose.override.yml \
  exec -T backend id -g
```

Do not proceed if either value differs. A successful authenticated Host Control
handshake after `FRIDAY_HOST_CONTROL_ENABLED=1` is the final transport check: it
proves traversal of the `0700` directory, access to the private key and socket,
exact peer-UID admission, and HMAC verification together. Do not loosen a mode,
add the selected host user to the signer group, or grant any unprinted group to
make a failed check pass.

Prove the disconnected lifecycle before the first capability rollout. The
directory must survive with mode `0700`, the socket must be absent, backend must
restart normally, and `jericho doctor` must report Host Control unavailable:

```bash
systemctl --user stop friday-host-agent.service
stat -c '%u:%g:%a' /run/friday-host-agent/1000
test ! -S /run/friday-host-agent/1000/agent.sock
docker compose -f docker-compose.yml -f deploy/host-control/compose.override.yml \
  up -d --force-recreate backend
docker compose -f docker-compose.yml -f deploy/host-control/compose.override.yml \
  exec -T backend jericho doctor
systemctl --user start friday-host-agent.service
```

After the start, run `jericho doctor` again and require an authenticated healthy
agent identity. This stop/start test does not enable package, desktop, one-shot,
or public-network capabilities.

For the first controlled nmap test, enable only the core feature and the exact
private subnet authorized by the operator. First edit the native policy as root:

```toml
[network]
schema_version = 1
allowed_cidrs = ["192.168.1.0/24"]
allow_public = false
```

Save that exact document as
`/etc/friday-host-control/host-agent-policy.toml`, keep it root-owned and not
writable by group/others, then restart the user agent. Configure the identical
scope in the backend:

```dotenv
FRIDAY_HOST_CONTROL_ENABLED=1
FRIDAY_HOST_ALLOWED_CIDRS=192.168.1.0/24
```

Restart the backend and require `jericho doctor` to report the same signed
network-policy digest. Any one-sided edit intentionally makes the handshake
fail closed. Revocation is the reverse sequence: narrow the root-owned native
policy first, restart the agent, then narrow/restart the backend; an already
approved wider plan is rejected by the agent before execution.

Enable `FRIDAY_HOST_PACKAGE_INSTALL_ENABLED=1` only for the separate test that
must prepare an exact APT plan and obtain human approval.  Public CIDRs require
the independent public-network flag and remain outside the first release
acceptance. If a later acceptance enables them, require a pending human approval
followed by one successful exact proof. The native agent pins the backend
signer's public-key digest in the authenticated handshake, verifies the exact
job/plan/idempotency/actor/owner/approval payload and expiry again immediately
before launch, and consumes the proof in a restart-safe immutable ledger. Prove
that forged, expired, drifted, deleted/replayed and post-restart proofs never
reach the runner. Keep desktop and one-shot flags at `0`.

## Validation and operation

Static unit validation is effect-free:

```bash
systemd-analyze verify \
  deploy/host-control/systemd/user/friday-host-agent.service \
  deploy/host-control/systemd/system/friday-package-broker.socket \
  deploy/host-control/systemd/system/friday-package-broker.service
```

After installation, inspect both trust domains separately:

```bash
systemctl --user status friday-host-agent.service
sudo systemctl status friday-package-broker.socket
journalctl --user -u friday-host-agent.service
sudo journalctl -u friday-package-broker.service
```

The broker has an effect-free configuration preflight. Run it before enabling
package installation:

```bash
sudo /opt/friday-host-control/current/bin/friday-package-broker \
  --check-config \
  --systemd-socket \
  --socket /run/friday-package-broker/broker.sock \
  --policy /etc/friday-host-control/broker-policy.toml \
  --key-file /etc/friday-host-control/broker.key \
  --signing-key-file /etc/friday-host-control/broker-signing.key \
  --approval-verification-public-key-file /etc/friday-host-control/backend-approval-signing.pub \
  --state-dir /var/lib/friday-package-broker
```

For an optional graphical-session probe, import only the required public session
coordinates, then restart the user agent:

```bash
systemctl --user import-environment \
  DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_CURRENT_DESKTOP DBUS_SESSION_BUS_ADDRESS
systemctl --user restart friday-host-agent.service
```

This does not enable desktop control.  With no graphical session the agent must
report GUI capability unavailable; it must not synthesize a successful launch.

The production acceptance run remains the sequence defined in the architecture
brief: start with `nmap` absent, reject one plan and prove no effect, approve a
fresh exact plan, install through the broker, attest `/usr/bin/nmap`, resume the
original bounded local-subnet scan, then verify coverage/evidence and the linked
approval, package transaction, host job, and final response.  Never use the
uninstall script to erase those acceptance receipts.

## Rollback and removal

Rollback the backend first by setting every `FRIDAY_HOST_*_ENABLED` flag to `0`
and removing the optional Compose override.  Normal Friday operation must remain
healthy with both host services stopped.

Then remove the host-side services:

```bash
sudo deploy/host-control/uninstall.sh --user friday
```

This removes units and the dedicated venv, but preserves keys, policies, broker
state, and host-job evidence for audit/reinstall. `--purge-secrets` additionally
removes the two HMAC keys, both Ed25519 key pairs, broker policy, and native
host-agent network policy; it still preserves broker state and job evidence.
Package removal is never implicit: packages installed through an
approved transaction remain installed until a separate future removal contract
exists.
