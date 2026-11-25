# The host doctor

![The doctor's report: every row with its marker, the hints
under each miss, and the closing tally](screens/doctor.png)

`msksd doctor` checks the host for the tools and libraries the
daemon and the client exec at runtime. Every reported issue suggests a
fix.

Note that msksd doctor is not very useful on macOS: use
`msks-setup-macos-host --check` instead.

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
admin directories (`/usr/local/sbin`, `/usr/sbin`,
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
upstream static release.

On Linux, three more rows read the system deployment's own
state. `msks-group` checks that the `msks` access group exists
and that the account running doctor holds it (root passes
without holding it). `msksd-unit` checks that the `msksd`
system unit is active. `shared-socket` checks that
`/run/msks/msksd.sock` names a socket a serving daemon left
behind. All three are warning grade: a daemon serving TCP alone
(the dev daemon, a remote deployment) keeps a green report
without them.

One row appears only while the `nested` setting is on
(`MSKSD_NESTED=true`, or `nested: true` in the config file
(#1140)), and only on an x86_64 Linux host, where the kernel
publishes KVM's `nested` switch as the vendor module's parameter.
`MSKSD_NESTED` is green when the host's KVM lets its guests nest.
It is a warning when the host does not nest, with the module
option that turns nesting on. There every workspace still boots,
without VMX or SVM. The daemon logs the same warning at startup.
It is also a warning when no `kvm_intel` or `kvm_amd`
parameter is readable under `/sys/module`, and the row asks for
the module and a visible `/sys` before a re-run. On other machines
the kernel publishes no switch, so the row stays off.

The run ends with a tally (`20 passed`, or `18 passed, 1
error, 1 warning`). When anything failed, a closing block
repeats every miss with its fix. The exit code is 0 when every
check passes or only warns, and 1 when any check errors.
