# The host doctor

![The doctor's report: every row with its marker, the hints
under each miss, and the closing tally](screens/doctor.png)

`msksd doctor` checks the host for the tools and libraries the
daemon and the client exec at runtime. Every reported issue suggests a
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
run adds a warning. Its fix is to log out and back in (the
setup tool adds the directories to `~/.profile`), or to run
`export PATH=$PATH:/usr/sbin:/sbin`. A daemon started by hand
inherits the session's PATH, so the fix reaches that shape too.

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
