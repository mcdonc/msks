# The host doctor

`msksd doctor` checks the host for the external tools and
libraries the daemon and the client exec at runtime, and tells
you how to fix every miss. Run it when a host is newly
prepared, when a refusal names a missing tool, or before a
deployment first serves:

```console
msksd doctor
```

The command takes the same `--config` flag the daemon takes.
It therefore checks the tools this host's daemon will actually
exec: the config's tool paths and the environment apply. See
[Configuration](config.md) for the settings and their
precedence.

## What it checks

The error-grade rows are the daemon's core paths. They cover
the cloud-hypervisor VMM, the e2fsprogs pair that grows and
checks workspace volumes, and the `mkisofs` that packs the
cidata seed disks. They also cover the
iproute2/nftables/conntrack trio behind each workspace's tap,
`qemu-img` (the root overlay and volume-format probes), and the
two libraries the interceptor's NFQUEUE path links against.

Two library-adjacent rows follow. The vendored ctypes binding's
own soname load is a row beside the two library rows, and all
three run on Linux hosts only. Off Linux, the binding row names
the platform contract instead of hinting packages that cannot
work there. On Linux, the binding dlopens
`libnetfilter_queue.so.1`, the same load the consent path
performs, so a host whose runtime soname is absent fails the
row even when `pkg-config` still answers for the library. The
libraries themselves are verified through `pkg-config`, the
linker cache (`ldconfig -p`), or that soname load, whichever
answers first.

The tmux row holds the host tmux to the 3.2 floor the consent
terminal documents, parsing the version the binary prints.

The warning-grade rows are the client-side tools the documented
workflows run over `msks forward`: the ssh client and the host
rsync it drives. The daemon itself runs without them, so a miss
warns instead of failing the run.

The check set covers runtime requirements only. Debugging and
development tools stay out of it.

## Reading the report

Every row prints with a marker:

- **✓** — present and functional: the binary answered its
  version probe, the library answered one of its three probes,
  the binding loaded.
- **⚠** — a warning-grade miss: a degraded path the daemon does
  not need (the ssh/rsync pair), or a config that failed to
  load.
- **✗** — an error-grade miss: a core-path dependency the
  daemon needs.

Each miss carries an install hint matched to the detected
package manager, printed as a paste-ready command sequence under
the row. The hints match each manager's real package names:
`genisoimage` serves `mkisofs`, `qemu-utils` ships `qemu-img`,
and `conntrack` is apt's name. The library rows name the runtime
soname packages (`libnetfilter-queue1` and `libnfnetlink0` on
apt). Where a fix is not a distro package, the hint names the
real source: cloud-hypervisor installs from the upstream static
release, which Debian's archive carries in none of its
suites.

The run ends with a tally (`14 passed`, or `12 passed, 1 error,
1 warning`). When anything failed, a closing block repeats every
miss with its fix. The exit code is 0 when every check passes or
only warns, and 1 when any check errors, so a script or a unit
file can gate on it.

## Side effects and the config it reads

Doctor takes no side effects. It reads a config when one
resolves, and it never generates the first-run template. A
fresh host with no default config yet falls back to environment
variables and defaults silently. A config that exists but fails
to load falls back the same way, with a warning row naming the
error. The checks still run either way, so a broken config does
not hide the host's state.

## The dev host

The dev daemon runs bare: `scripts/dev-daemon.sh` points
`MSKSD_CONFIG_DIR` at the devenv root. A hand-run `msksd doctor`
in the shell therefore resolves the same `<repo>/msksd.yaml`
the daemon serves with. The environment the dev script exports
(API port, state dir, egress wiring) wins over file values
under the [precedence](config.md#precedence) the Configuration
chapter documents.
