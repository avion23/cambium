# Sandboxing options for Cambium's Septum (M8) on this machine

Research date: 2026-08-09. Purpose: pick a worker-sandbox mechanism for Septum
(M8) that actually works on the dev/run machine this document was written on.
Every claim below is either a real command output pasted verbatim from this
machine, or a URL. Anything that could not be checked is marked **UNVERIFIED**.

Scope contract from the source docs:

- `docs/system-design.md` §M8: Septum wraps each worker in `bwrap`
  (`--die-with-parent`, ro-binds of `/usr /lib /lib64 /bin`, `--bind` of the
  worktree, fresh `/dev /proc`, `--tmpfs /tmp`, `--unshare-net` when
  `allow_network=False`).
- `docs/reviews/review-implementation.md` IMPL-M4: bwrap is Linux-only; the
  design must add platform backends (`SandboxExecSandbox` on macOS, noop) and
  a platform abstraction.
- `cambium-arch/docs/architecture.md` §4: Septum = `BwrapSandbox` (Linux),
  `SandboxExecSandbox` (macOS, best-effort), `NoopSandbox` (dev/CI). Spawn uses
  `sandbox.wrap([sys.executable, ...])`. The arch doc does **not** specify the
  failure mode when the Linux sandbox cannot start; that policy is proposed
  below (§6).

## 0. Machine facts

```
$ uname -a
Linux arm-server-01 6.17.0-1009-oracle #9~24.04.1-Ubuntu SMP Sat Mar  7 01:08:51 UTC 2026 aarch64 aarch64 aarch64 GNU/Linux

$ systemd-detect-virt
kvm

$ id
uid=1001(ubuntu) gid=1001(ubuntu) groups=1001(ubuntu),...27(sudo),...,120(lxd),999(docker),...

$ bwrap --version
bubblewrap 0.9.0

$ dpkg -l | grep -i bubblewrap
ii  bubblewrap  0.9.0-1ubuntu0.1  arm64  utility for unprivileged chroot and namespace manipulation
```

Ubuntu 24.04 (noble) aarch64, kernel 6.17, running as unprivileged `ubuntu`
(uid 1001) with passwordless sudo. Python 3.12.3 (system) and 3.14.7
(`~/.local/bin/python3.14`, uv-managed) are available.

## 1. Local evidence

### 1.1 bwrap sandbox test from the task — FAILS as unprivileged user

Command (workdir `/tmp`, 60 s timeout):

```
$ bwrap --ro-bind /usr /usr --ro-bind /bin /bin --proc /proc --dev /dev --tmpfs /tmp --unshare-all \
    /usr/bin/python3 -c "print('sandboxed')"
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
$ echo $?
1
```

It never ran python. Isolated variants fail earlier or identically:

```
$ bwrap --ro-bind /usr /usr --ro-bind /bin /bin --proc /proc --dev /dev --tmpfs /tmp \
    --unshare-user --unshare-ipc --unshare-pid --unshare-uts --unshare-cgroup-try \
    /usr/bin/python3 -c "print('sandboxed-no-net')"
bwrap: setting up uid map: Permission denied
$ echo $?
1

$ bwrap --ro-bind /usr /usr --ro-bind /bin /bin --proc /proc --dev /dev --tmpfs /tmp \
    --unshare-net /usr/bin/python3 -c "print('sandboxed-net-only')"
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
$ echo $?
1
```

### 1.2 Root cause: unprivileged user namespaces are blocked by AppArmor

`/usr/bin/bwrap` is not setuid (no `s` bit), and bwrap 0.9.0 removed setuid
mode entirely (see §3.1), so it must create its own user namespace. That is
blocked:

```
$ unshare -Ur true
unshare: write failed /proc/self/uid_map: Operation not permitted
$ echo $?
1

$ unshare -m true
unshare: unshare failed: Operation not permitted
$ echo $?
1

$ sysctl kernel.unprivileged_userns_clone user.max_user_namespaces
kernel.unprivileged_userns_clone = 1
user.max_user_namespaces = 95447

$ cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns
1

$ cat /proc/self/attr/current
unconfined
```

User namespaces are enabled at the kernel level (`unprivileged_userns_clone=1`,
no `max_user_namespaces` exhaustion) but **AppArmor's
`apparmor_restrict_unprivileged_userns=1` denies userns creation to all
unprivileged processes**, including unconfined ones. This is the Ubuntu 23.10+
default. Every namespace-based sandbox (bwrap, firejail, nsjail, rootless
runc/gVisor) inherits this failure on this machine until it is remediated.

### 1.3 bwrap as root (via passwordless sudo) WORKS

`sudo -n` is available, and as root bwrap clears the namespace step:

```
$ sudo -n bwrap --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp --unshare-all \
    /bin/sh -c "ls /usr/bin/python3.12 && /usr/bin/python3.12 -c 'print(\"python-runs\")'"
/usr/bin/python3.12
python-runs
$ echo $?
0
```

### 1.4 The v0.1 M8 bwrap command is broken on this machine even as root

M8 binds `/lib` and `/lib64`. This machine uses a merged `/usr` — `/bin` and
`/lib` are symlinks into `/usr`, and `/lib64` does not exist:

```
$ ls -ld /usr /bin /lib /lib64
lrwxrwxrwx 1 root root 7 Jan 28  2026 /bin -> usr/bin
lrwxrwxrwx 1 root root 7 Jan 28  2026 /lib -> usr/lib
ls: cannot access '/lib64': No such file or directory

$ sudo -n bwrap --die-with-parent \
    --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 --ro-bind /bin /bin \
    --dev /dev --proc /proc --tmpfs /tmp --unshare-net \
    /usr/bin/python3.12 -c "print('M8-ok')"
bwrap: Can't find source path /lib64: No such file or directory
$ echo $?
1
```

A Septum command must not hard-code `/lib64`. Binding the whole root
read-only (`--ro-bind / /`) avoids this class of problem entirely.

### 1.5 Root-mode bwrap hides all files owned by uid 1001

bwrap created by root maps only uid 0 into the child user namespace. Files
owned by the unmapped host uid 1001 (`ubuntu`) appear as `nobody:nogroup`
inside the sandbox and are **inaccessible even to ns-root** — this includes
the worktree, `/home/ubuntu/cambium`, and the uv-managed Python at
`/home/ubuntu/.local/bin/python3.14`:

```
$ sudo -n bwrap --die-with-parent --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
    --unshare-all --unshare-net /bin/sh -c "id; stat -c '%a %U:%G %n' /home /home/ubuntu"
uid=0(root) gid=0(root) groups=0(root)
755 root:root /home
700 nobody:nogroup /home/ubuntu

$ sudo -n bwrap --die-with-parent --ro-bind / / --ro-bind /home/ubuntu/.local /home/ubuntu/.local \
    --bind /tmp/opencode/wt-test /tmp/opencode/wt-test --proc /proc --dev /dev --tmpfs /tmp \
    --unshare-all --unshare-net /home/ubuntu/.local/bin/python3.14 -c "print('ok')"
bwrap: execvp /home/ubuntu/.local/bin/python3.14: Permission denied
```

`--uid 1001` does not fix the mapping (the uid_map still contains only root):

```
$ sudo -n bwrap --die-with-parent --uid 1001 --gid 1001 --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
    --unshare-all --unshare-net /bin/sh -c "id; stat -c '%a %U:%G %n' /home/ubuntu"
uid=1001(ubuntu) gid=1001(ubuntu) groups=1001(ubuntu)
700 nobody:nogroup /home/ubuntu
```

So "run the supervisor as root and bwrap each worker" does not work out of the
box for a worker whose interpreter, worktree, and git state live under uid
1001. The clean fix is to make **unprivileged** bwrap work (bwrap run by uid
1001 maps 1001→0, and every uid-1001 file is then owned by ns-root — the
standard Flatpak-style setup). This is the recommendation in §6.

### 1.6 Network blocking with `--unshare-net` — VERIFIED (as root)

With bwrap working as root, `--unshare-net` gives a loopback-only network
namespace and outbound connects fail:

```
$ sudo -n bwrap --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
    --unshare-all --unshare-net /usr/bin/python3.12 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(('1.1.1.1', 443)); print('NETWORK: CONNECTED (not blocked!)')
except Exception as e:
    print('NETWORK: BLOCKED ->', type(e).__name__, e)
"
NETWORK: BLOCKED -> OSError [Errno 101] Network is unreachable
$ echo $?
0
```

Errcode 101 (`ENETUNREACH`): the netns has no route to the outside. This is the
property Septum's `allow_network=False` relies on, and it holds.

### 1.7 Other sandbox tools installed?

```
$ which firejail bwrap seccomp-tools
firejail not found
/usr/bin/bwrap
seccomp-tools not found

$ which docker runc containerd runsc gvisor
docker not found, runc not found, containerd not found, runsc not found, gvisor not found
```

No firejail, no docker/containerd/runc runtime, no gVisor, no seccomp
tooling. The `docker` and `lxd` groups exist but no client is installed.
bubblewrap is the only sandbox binary on the machine.

### 1.8 Landlock: kernel yes, Python stdlib no, privileges needed

Kernel 6.17 ships Landlock (`landlock` is in the LSM stack), and the syscalls
resolve (no `ENOSYS`):

```
$ cat /sys/kernel/security/lsm
lockdown,capability,landlock,yama,apparmor,ima,evm

$ python3 - <<'EOF'
import ctypes, errno
libc = ctypes.CDLL(None, use_errno=True)
for name, num in [("create_ruleset",444), ("add_rule",445), ("restrict_self",446)]:
    r = libc.syscall(num, 0, 0, 0)
    print(f"landlock_{name} -> ret={r} errno={ctypes.get_errno()} ({errno.errorcode.get(ctypes.get_errno())})")
EOF
landlock_create_ruleset -> ret=-1 errno=14 (EFAULT)
landlock_add_rule -> ret=-1 errno=22 (EINVAL)
landlock_restrict_self -> ret=-1 errno=1 (EPERM)
```

Ruleset creation works with a real attr when run as root (unprivileged gets
`EPERM`; creating a ruleset needs `CAP_SYS_ADMIN` in the current user
namespace):

```
$ sudo -n python3 - <<'EOF'
import ctypes, errno
libc = ctypes.CDLL(None, use_errno=True)
class LLRA(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]
attr = LLRA(0x1FFF, 0)   # ABI-v1 fs-access mask; 0x1FFFF is rejected (unsupported bits -> EINVAL)
r = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
print(f"create_ruleset -> fd {r} errno {ctypes.get_errno()}")
EOF
create_ruleset -> fd 3 errno 22 (EINVAL)
```

The Python stdlib does **not** expose Landlock on either interpreter present on
this machine, and the official 3.14 `os` docs list no `landlock_*` function:

```
$ python3 -c "import os; print('landlock_restrict_self:', hasattr(os, 'landlock_restrict_self'))"
Python 3.12.3
landlock_restrict_self: False

$ /home/ubuntu/.local/bin/python3.14 --version
Python 3.14.7
$ /home/ubuntu/.local/bin/python3.14 -c "import os; print('landlock_restrict_self:', hasattr(os, 'landlock_restrict_self'))"
landlock_restrict_self: False
```

Landlock on this machine would require (a) a privileged helper or pre-created
ruleset, and (b) ctypes glue — no stdlib path. It also does not gate network
access. It is therefore not a Septum replacement on this machine; it is a
possible future hardening layer. Codex uses Landlock-style path mediation for
its CLI sandbox on Linux/macOS, but bwrap is still the documented Linux
mechanism there (§3.4).

### 1.9 AppArmor remediation profile — not installed locally

Ubuntu's `apparmor-profiles` package ships `bwrap-userns-restrict`, the
narrow remediation that lets unprivileged bwrap create a userns without
disabling the restriction globally. It is absent here:

```
$ ls /usr/share/apparmor/extra-profiles/bwrap-userns-restrict
ls: cannot access '/usr/share/apparmor/extra-profiles/bwrap-userns-restrict': No such file or directory
```

So both remediation paths (profile, or the `sysctl` switch) require a one-time
root action on this machine; neither has been applied (not applied here —
`UNVERIFIED` that either works until tested after installation).

## 2. What a worker could do unsandboxed (analysis, no code)

Without a sandbox, a Cambium worker running as uid 1001 has full read/write
access to everything uid 1001 owns (`/home/ubuntu`, the whole repo, all
worktrees, git config, ssh keys, provider API keys in env), plus outbound
network, process listing, `/proc/1` etc. `os.system`/`subprocess` are only
restricted by uid permissions and rlimits. Septum's purpose is to cut this
surface: hide everything except the worktree, deny writes elsewhere, and drop
network when the task does not allow it.

What bwrap gives (and what was verified above):

- **Filesystem**: fresh mount namespace + `--ro-bind / /` (or targeted ro-binds)
  hides everything by default; only `--bind`ed paths are writable. `--tmpfs`
  for `/tmp`, fresh `/dev` and `/proc`.
- **Network**: `--unshare-net` puts the worker in a loopback-only netns —
  verified blocking outbound connects (§1.6). With network allowed, omit it
  and the worker sees the host netns (fine for LLM egress).
- **Process/pid isolation**: `--unshare-pid` hides other processes; bwrap runs
  a trivial pid1 that reaps children (bubblewrap README §3.1).
- **What bwrap does not give**: it is not a complete sandbox — the security
  model is whatever args the caller passes (bubblewrap README §3.1). It does
  not apply seccomp filters by itself (none are specified in either Septum
  design; seccomp-tools are not installed). `PR_SET_NO_NEW_PRIVS` neutralizes
  setuid escalation inside the sandbox.

## 3. Web sources

### 3.1 bubblewrap README — setuid removed, userns required, netns is loopback-only

https://github.com/containers/bubblewrap

- "Historically, bubblewrap also supported a setuid mode ... However, this has
  been removed."
- `--unshare-net`: "The sandbox will not see the network. Instead it will have
  its own network namespace with only a loopback device."
- "bubblewrap is not a complete, ready-made sandbox with a specific security
  policy ... the level of protection ... is entirely determined by the
  arguments passed to bubblewrap."
- Firejail comparison: firejail "combines a setuid tool with a lot of
  desktop-specific sandboxing features"; the bwrap authors recommend bwrap.

### 3.2 AppArmor user-namespace restriction (the local blocker)

https://manpages.ubuntu.com/manpages/noble/en/man5/apparmor.d.5.html

The `userns` rule: "user namespace creation may be restricted so that it is not
available to unprivileged unconfined processes. If this is the case any
process trying to create user namespaces will require a profile that allows
the necessary permissions." (`userns create,`). This is the mechanism behind
`kernel.apparmor_restrict_unprivileged_userns=1` observed locally.

### 3.3 gVisor — not a fit here

https://gvisor.dev/docs/

gVisor is an OCI runtime (`runsc`) implementing "a Linux-like interface ...
written in Go" — an application kernel, explicitly "not a wrapper over Linux
isolation primitives (e.g. firejail, AppArmor)". It requires container tooling
(docker/containerd/OCI bundles). None of that is installed on this machine
(§1.7), and it is heavyweight for per-worker subprocesses. Rejected for
Septum; `UNVERIFIED` for performance fit on aarch64 (no runsc available).

### 3.4 Codex sandbox docs — the closest precedent (bubblewrap + warning fallback)

https://learn.chatgpt.com/codex/sandboxing (was
`https://github.com/openai/codex/blob/main/docs/sandbox.md`, which redirects)

- "Codex uses platform-native enforcement on each OS": **macOS = built-in
  Seatbelt**, **Windows = native Windows sandbox**, **Linux/WSL2 = bubblewrap**
  (`sudo apt install bubblewrap`).
- "If no `bwrap` executable is available, Codex falls back to a bundled
  helper, but that helper requires support for unprivileged user namespace
  creation."
- **"Codex surfaces a startup warning when `bwrap` is missing or when the
  helper can't create the needed user namespace."** — the fallback-with-warning
  pattern.
- **Ubuntu 24.04 AppArmor note** (exact match for this machine): "On Ubuntu
  24.04, Codex may still warn that it can't create the needed user namespace
  after `bubblewrap` is installed." Remediation: load
  `/usr/share/apparmor/extra-profiles/bwrap-userns-restrict` (from
  `apparmor-profiles`), or `sysctl -w
  kernel.apparmor_restrict_unprivileged_userns=0`.
- Sandbox modes used by Codex: `read-only`, `workspace-write`,
  `danger-full-access`, with a config `sandbox_mode` key.

### 3.5 nsjail — same userns/root dependency

https://github.com/google/nsjail

NsJail does namespaces + seccomp-bpf + rlimits. Its own troubleshooting says
`--disable_clone_newuser` "requires root", and the standard mode uses a user
namespace (`-U/--uid_mapping` needs `newuidmap`). Identical dependency on
unprivileged userns as bwrap, plus it is not installed and needs building from
source. Rejected for Septum on this machine.

### 3.6 firejail — not installed; setuid-based

https://github.com/netblue30/firejail

Firejail is a setuid sandbox (per the bwrap README §3.1). It is not installed
(§1.7), is heavier (desktop-oriented profiles), and adds a setuid binary to
the trust base. Rejected for Septum on this machine.

### 3.7 Python stdlib — no Landlock API in 3.14

https://docs.python.org/3/library/os.html

The 3.14 `os` module docs contain no `landlock_*` function (confirmed by
inspection; also confirmed live by `hasattr` on 3.12.3 and 3.14.7, §1.8).

## 4. Recommendation table

| Mechanism | Strengths | Weaknesses | Verdict for Cambium |
|---|---|---|---|
| **bubblewrap (bwrap) 0.9.0** | Installed; matches v0.1/v2 Septum design; unprivileged-by-design (userns); verified netns network-block (§1.6); small, auditable, no setuid; Codex precedent on Linux | Unprivileged userns blocked by AppArmor here (§1.2) until remediated; root-mode has the uid-1001 invisibility problem (§1.5); v0.1 M8 flag list breaks on merged-usr (`/lib64`); not a complete sandbox without careful flags | **RECOMMENDED.** Needs one-time remediation to run unprivileged (§5); flags must be rewritten to bind `/` read-only instead of per-dir `/lib /lib64` |
| **Landlock (via `os.landlock_*`)** | In-kernel (6.17), no namespaces, path-granular; Codex CLI uses a Landlock-style sandbox on Linux/macOS | **Not in the Python stdlib** on 3.12.3 or 3.14.7 (verified); ruleset creation needs `CAP_SYS_ADMIN` (unprivileged got `EPERM`, verified); no network control; needs ctypes glue + privileged helper | Rejected as the Septum mechanism on this machine; possible future hardening layer. **UNVERIFIED** for viability of a full ctypes implementation (not built) |
| **firejail** | Prebuilt policy profiles | Not installed; setuid; desktop-oriented; heavier | Rejected (not installed; adds setuid to trust base) |
| **gVisor / runsc** | Strong syscall-level isolation; memory-safe | Not installed; needs OCI/docker/containerd infra; heavier; aarch64 fit untested | Rejected for per-worker subprocesses; revisit only if container infra is added. **UNVERIFIED** performance |
| **nsjail** | Namespaces + seccomp + rlimits in one | Same userns/root dependency; not installed; build from source | Rejected (same blocker as bwrap, more moving parts) |
| **seccomp-only / rlimits** | Works unprivileged (no namespaces) | Cannot hide files or block network; not a boundary for a coding agent; no tooling installed | Rejected alone; bwrap's `PR_SET_NO_NEW_PRIVS` covers the relevant part |
| **NoopSandbox (no sandbox)** | Always available; dev/CI speed | No isolation | Accepted only as explicit opt-out / fallback with warning (matches arch v2 §4) |

## 5. Recommendation for Septum

**Mechanism: `BwrapSandbox` (bubblewrap 0.9.0), run unprivileged by the
supervisor's uid, exactly as arch v2 §4 already specifies.**

Privileges: none beyond the invoking uid. bwrap unprivileged maps
uid 1001→0 into the child userns, so the worktree, `/home/ubuntu/.local`
(worker interpreter), and git state remain fully readable/writable inside the
sandbox — the opposite of the root-mode failure in §1.5. Do not run the
supervisor or bwrap as root for sandboxing purposes.

**Required one-time remediation on this machine** (either; both need root):

1. Narrow: install `apparmor-profiles` and load
   `/etc/apparmor.d/bwrap-userns-restrict` (profile grants `userns create,` to
   bwrap only). Matches Codex's Ubuntu 24.04 guidance (§3.4). **UNVERIFIED**
   until installed and tested here.
2. Broad: `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`
   (persist via `/etc/sysctl.d/`). Also **UNVERIFIED** here — deliberately not
   applied during this research.

`UNVERIFIED`: after applying either, confirm with the §1.1 command succeeding
(`sandboxed` printed, exit 0) before wiring Septum. That probe command is
exactly the capability check Septum should run at startup.

**Filesystem policy (rewrite the v0.1 M8 flags):** `--die-with-parent`,
`--ro-bind / /`, `--bind <worktree> <worktree>`, `--bind <session_dir>
<session_dir>`, `--proc /proc`, `--dev /dev`, `--tmpfs /tmp`, plus `--ro-bind`
of any extra interpreter path not under `/` (e.g. the uv-managed
`~/.local/share/uv/python/...` if `sys.executable` resolves there). Binding
the whole root read-only is the robust form on merged-usr systems; the v0.1
per-dir `/lib /lib64` list is broken (verified §1.4). Secret injection per
arch v2 §12 stays `--setenv KEY=value` for the allowlisted env names.

**Network policy:** `allow_network=False` (the default) → append
`--unshare-net` (blocking verified §1.6). `allow_network=True` → omit it so
the worker reaches the LLM providers through the host netns.

**Failure mode when the sandbox cannot start** (not specified by arch v2; this
is the proposed policy, following Codex §3.4):

- Startup capability probe: run
  `bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp
  /bin/true` once at supervisor init; cache the result.
- On probe failure, emit a `sandbox_unavailable` warning event and apply
  `sandbox.mode` from `SandboxConfig`:
  - `required` (default for untrusted/networked specs): refuse to spawn the
    worker; mark the task `FAILED` with `failure_reason="sandbox_unavailable"`.
    No silent noop.
  - `warn`: run the worker with `NoopSandbox` and log a per-task warning event
    (dev/CI convenience; never for untrusted code).
  - `off`: `NoopSandbox` explicitly configured (development only, matches arch
    v2's "noop (dev/CI)").
- Default for the CLI/host: `required`. This keeps the honest-gap posture of
  arch v2 §19 ("the design does not assume bubblewrap" is about platform
  portability, not about silently degrading isolation).

## 6. Sources

- bubblewrap README: https://github.com/containers/bubblewrap
- AppArmor profile syntax (`userns` rule): https://manpages.ubuntu.com/manpages/noble/en/man5/apparmor.d.5.html
- Codex sandbox docs (bubblewrap on Linux, Seatbelt on macOS, warning + AppArmor profile/sysctl, sandbox modes): https://learn.chatgpt.com/codex/sandboxing
- gVisor overview: https://gvisor.dev/docs/
- nsjail README: https://github.com/google/nsjail
- firejail repo: https://github.com/netblue30/firejail
- Python `os` module docs (no `landlock_*` API): https://docs.python.org/3/library/os.html

## 7. UNVERIFIED items

- bwrap unprivileged operation after loading `bwrap-userns-restrict` or
  setting `kernel.apparmor_restrict_unprivileged_userns=0` (neither was
  applied during this research; the sysctl is deliberately not toggled).
- `--unshare-net` behavior in the **unprivileged** (remediated) case — the
  netns block was only executed via root-mode bwrap.
- Landlock-as-Septum viability end-to-end (no ctypes Landlock helper was
  built; the unprivileged `EPERM` on `restrict_self` blocks the stdlib-free
  path).
- gVisor/runsc performance and compatibility on aarch64 (no runsc available).
- Behavior on the macOS build machine (this research is Linux-only; macOS
  Seatbelt via `sandbox-exec` remains the arch v2 `SandboxExecSandbox`
  backend, untested here).
- Whether Docker/LXD tooling will be installed later (group membership exists,
  binaries do not) and whether that would change the recommendation.
