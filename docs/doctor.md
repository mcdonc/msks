# The host doctor

![The doctor's report: every row with its marker, the hints
under each miss, and the closing tally](screens/doctor.png)

`msksd doctor` checks the host for the tools and libraries the
daemon and the client exec at runtime. Every miss comes with a
fix.

```console
msksd doctor
```

The command takes the same `--config` flag the daemon takes. It
checks the tools this host's daemon will actually exec: the
config's tool paths and environment apply.

## What it checks

The checks cover runtime needs only: the external dependencies,
the cloud-hypervisor VMM, and a few configuration settings.

Tool names resolve on the daemon's search path, not the
caller's. The session's own PATH answers first. Then come
Debian's admin directories (`/usr/local/sbin`, `/usr/sbin`,
`/sbin`), because the systemd user manager's default PATH
carries them. A session whose PATH lacks these directories
still gets the daemon's truth.

When a tool was found only through the admin directories, the
run adds a warning row. Its fix is to log out and back in (the
setup tool adds the directories to `~/.profile`), or to run
`export PATH=$PATH:/usr/sbin:/sbin`. A daemon started by hand
inherits the session's PATH, so the fix reaches that shape too.

One check grades by platform, not by a tool. On an Apple
Silicon host whose kernel runs KVM in nested-virtualization
mode (the Asahi Linux class today), the report carries a
warning row about the kernel's guest-interrupt limitation
([#649](https://github.com/mcdonc/msks/issues/649)). On such a
host, workspaces start and their kernels boot, then stall
before ssh answers. Everything short of a guest boot works.

The row requires both markers. The first marker: cpu0's MIDR
reports Apple's implementer. The second marker: the kernel runs
KVM in NV mode. The mode shows up in any of three places. The
command line sets `kvm-arm.mode=nested`, sysfs publishes kvm's
mode parameter as `nested`, or the kernel log carries KVM's
NV-mode init line (`VHE+NV2 mode initialized`). The markers
report the mode, not the kernel's provenance.

Each marker is read where the host leaves it readable. A
restricted kernel log reads as absent. The command line and
sysfs stay world-readable.

x86_64 hosts and non-Apple arm64 servers get no row. An
Apple-silicon host whose KVM is not in NV mode stays clean too.
A Linux VM under macOS's Virtualization.framework reads as
Apple silicon, because the hypervisor passes the host MIDR
through. Its own KVM delivers guest interrupts, so workspaces
boot and ssh answers there
([#664](https://github.com/mcdonc/msks/issues/664)).

## Reading the report

Every row prints with a marker:

- **✓**: present and functional. The binary answered its
  version probe, the library answered one of its three probes,
  the binding loaded.
- **⚠**: a warning-grade miss. The daemon does not need this
  path (the ssh/rsync pair), or a config failed to load.
- **✗**: an error-grade miss. The daemon needs this dependency
  on a core path.

Each miss carries an install hint matched to the detected
package manager, printed as a paste-ready command sequence
under the row. The hints use each manager's real package
names: `genisoimage` serves `mkisofs`, and `qemu-utils` ships
`qemu-img`.

Some fixes are not distro packages. Where that is so, the hint
gives the real source: cloud-hypervisor installs from the
upstream static release. Debian's archive carries it in none of
its suites.

The run ends with a tally (`14 passed`, or `12 passed, 1
error, 1 warning`). When anything failed, a closing block
repeats every miss with its fix. The exit code is 0 when every
check passes or only warns, and 1 when any check errors.
