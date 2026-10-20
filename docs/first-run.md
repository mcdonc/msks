# First run

The installation process has resulted in a running `msksd` systemd user
service.

To see status and log output, from the account the daemon runs as (the service
is a systemd user unit, lingered so it survives logout):

```bash
  systemctl --user status msksd
  journalctl --user -u msksd -f          # follow the daemon's log
```

When msksd is first started, it creates a state dir
(`~/.local/state/msksd`). The `msksd.yaml` config file is generated as a
commented template (see [Configuration](config.md)) within the directory.

The unix listener binds `$XDG_RUNTIME_DIR/msks/msksd.sock`. Same-host clients
connect through it on peer-credential authentication (while remote can, if
configiured, contact a TLS/https listener with a bearer token).

With the host prepared, the service lingered, and an image imported, invoking
the `msks` (the client) on the same account will find the socket.

On first run, a workspace image has not been imported, so you are not able to
create guest VMs. A workspace image arrives either from the published channel
(`msks image import` in [the TUI](tui.md), or the [images](images.md) chapter's
URL form) or from a local build.
