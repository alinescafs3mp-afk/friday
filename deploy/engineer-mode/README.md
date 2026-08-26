# Engineer Mode container boundary (Ubuntu, opt-in)

This bundle is the supported Docker path for Friday's bubblewrap artifact
worker.  Base Compose remains portable and keeps Engineer Mode disabled.  On
Ubuntu, Docker's ordinary seccomp and `docker-default` AppArmor policies block
the namespace, mount and `pivot_root` calls which bubblewrap must execute, so
turning on only `FRIDAY_ENGINEER_MODE_ENABLED=1` intentionally fails the real
startup smoke test.

The opt-in bundle does **not** use `privileged`, `apparmor=unconfined`,
`seccomp=unconfined` or `CAP_SYS_ADMIN`.  The backend remains non-root with:

- `cap_drop: ALL` and `no-new-privileges:true`;
- a read-only root filesystem and a 512-task cgroup ceiling;
- a default-deny seccomp profile derived from Moby's default profile at commit
  `f9bc03ec19b2dc4c091449b08e88f85c0caa9f0b`;
- an enforcing AppArmor profile derived from the same Moby revision; and
- the existing real, fail-closed no-network bubblewrap startup smoke.

The worker inherits the backend's PID cgroup as its process-count boundary.
Friday verifies that this ceiling is finite before touching an artifact; the
supported live verifier additionally requires the shipped exact value of 512.
It deliberately does not derive `RLIMIT_NPROC` from container `/proc`: in the
combined Host Control contour the real desktop UID is shared with host
processes hidden by the container PID namespace, so that calculation can deny
bubblewrap itself while adding no stronger bound than the PID cgroup.

The seccomp delta admits only the observed bubblewrap setup calls: exact
`clone(SIGCHLD|CLONE_NEWNS|CLONE_NEWCGROUP|CLONE_NEWUTS|CLONE_NEWIPC|
CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET)`, exact
`unshare(CLONE_NEWUSER)`, and `mount`, `umount2`, `pivot_root`, and
`sethostname`. `clone3` remains `ENOSYS`, so it cannot become an unreviewed
namespace path. `setns`, new mount API calls, BPF, kernel modules, raw I/O and
the other Moby default-denied calls remain denied.

AppArmor cannot safely match the dynamically generated disconnected mount paths
across bubblewrap's two `pivot_root` operations. Also, with
`no-new-privileges`, an exec transition cannot grant mount permissions only
after `/usr/bin/bwrap` starts. The shipped profile therefore admits the four
mount-namespace operations for the confined backend as Ubuntu's own bwrap
profile does. This grants no initial-namespace capability: Docker drops every
capability, and the kernel permits those calls only after bubblewrap enters its
private user namespace. This is the narrowest portable Ubuntu 24.04 policy we
can substantiate without weakening either `no-new-privileges` or the capability
boundary.

## Supported host

- Ubuntu 24.04 LTS or newer, x86-64 or arm64;
- rootful Docker Engine/Compose with AppArmor enabled in enforce mode;
- the kernel AppArmor `userns_create` and `pivot_root` features;
- unprivileged user namespaces available to the non-root container process; and
- the release image built from `docker/Dockerfile.backend`, which installs the
  fixed `/usr/bin/bwrap`, `/usr/bin/python3`, and `/usr/bin/prlimit` runtime.

The same image deliberately installs the declared first-party Engineer tools:
`nmap`; `dig` and `host` from `dnsutils`; `file`; `strings`, `readelf`, and
`objdump` from `binutils`; and `openssl`. The image build verifies every fixed
path. They still run only through code-owned argv, target and timeout contracts;
their presence adds no shell or capability. `capa`, `rabin2`, and `apkid` remain
optional adapters: this release does not install or claim them because the
supported distro image has no accepted, version-pinned package contract for
those tools. Runtime inventory reports each of them unavailable instead of
fabricating evidence.

Rootless Docker, another LSM, or an older Ubuntu release is not accepted by this
profile. Leave Engineer Mode off rather than substituting an unconfined policy
or adding a capability.

## Install and render

Keep the backend stopped while changing its kernel policies. From the verified
release checkout, atomically install the enforcing AppArmor profile and the
root-owned seccomp file:

```bash
sudo deploy/engineer-mode/install-apparmor.sh
```

Keep `FRIDAY_ENGINEER_MODE_ENABLED=0` for the first container start. Render and
inspect the exact merge:

```bash
docker compose \
  -f docker-compose.yml \
  -f deploy/engineer-mode/compose.override.yml \
  config

docker compose \
  -f docker-compose.yml \
  -f deploy/engineer-mode/compose.override.yml \
  up -d --build backend
```

If Host Control is also enabled, place its identity/mount override before the
Engineer policy so both remain explicit:

```bash
docker compose \
  -f docker-compose.yml \
  -f deploy/host-control/compose.override.yml \
  -f deploy/engineer-mode/compose.override.yml \
  up -d --build backend
```

Run the live contract smoke before enabling the feature:

```bash
deploy/engineer-mode/verify-runtime.sh
```

It requires Docker's selected seccomp path to be the dedicated root-owned
`/etc/friday-engineer/seccomp.json`, verifies its owner/mode/link identity and
compares it with this exact release, then checks the live AppArmor attachment,
seccomp mode, empty effective capability set, no-new-privileges, PID limit and
a real bubblewrap worker. A non-root checkout user therefore cannot swap a
permissive profile around container creation and restore the shipped bytes
before verification. The worker proves that its
network-namespace identity differs from the backend,
that only loopback exists, that no external IPv4/IPv6 route exists, and that
fixed documentation-prefix IPv4/IPv6 datagram connects both fail. A static test
cannot replace this host/kernel/runtime evidence.

Only after that command passes, set `FRIDAY_ENGINEER_MODE_ENABLED=1` and recreate
the backend with the same Compose file list. The normal server startup repeats
the real bubblewrap smoke and fails closed if policy or kernel state drifted.

## Rollback

Set `FRIDAY_ENGINEER_MODE_ENABLED=0`, recreate the backend without this override,
and verify that no container still names `friday-engineer-backend`. Then remove
the host profile:

```bash
sudo deploy/engineer-mode/uninstall-apparmor.sh
```

The uninstaller removes only the two exact installed policy files after the
container has stopped; it refuses a different or unsafe seccomp target.
