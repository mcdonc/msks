# The msksd configuration file

`msksd` reads its settings from a YAML file, from `MSKSD_*`
environment variables, and from built-in defaults. The file is the
durable home for a deployment's settings — committed to the
deployment's own storage and human-reviewable — while the
environment variables stay available as per-invocation overrides.

## Precedence

Values resolve in this order (highest first):

1. **Environment variables** (`MSKSD_*`) — override the file
2. **The config file** — the YAML file `msksd` resolves at startup
3. **Built-in defaults** — the settings dataclass defaults

A variable set in the process wins over the same key in the file; a
key set nowhere uses the default. An environment variable set to an
empty string is the unset form: the file value applies.

## The `--config` flag

`msksd` resolves its config file in three modes:

| Invocation                           | Behavior                                                                                                                                                                                                                                                                                                                    |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `msksd`                              | Reads `$MSKSD_CONFIG_DIR/msksd.yaml` (default `~/.config/msksd/msksd.yaml`, honoring `$XDG_CONFIG_HOME`). A missing file is **generated** as a commented template pointing at this chapter — the first run writes the file so its location is discoverable, and the daemon then runs on environment variables and defaults. |
| `msksd --config /path/to/msksd.yaml` | Reads exactly that file. A missing file is a startup error naming the path. Explicit paths are never auto-generated.                                                                                                                                                                                                        |
| `msksd --config=none`                | Reads environment variables and built-in defaults only — the deployment shape for container runs that manage config out-of-band. The appliance generates its file instead (below).                                                                                                                                          |

`MSKSD_CONFIG_DIR` is read before anything else and exists only as
an environment variable: the config file cannot relocate the
directory it lives in, so the tree root must be resolvable before
the file is located.

### The appliance

The appliance writes `/run/msksd/msksd.yaml` — a
service-user-owned `RuntimeDirectory` on the `/run` tmpfs — at
every boot
carrying the build's settings — the state dir, the listener's bind,
and the store paths of the tools the daemon execs — and starts the
daemon with `--config /run/msksd/msksd.yaml`. The file is regenerated at
each boot because the tool paths it names belong to that build; a
file that survived an appliance rebuild would point at dead store
paths. Operator overrides keep riding the kernel cmdline bridge:
every `msksd.<name>=<value>` pair on the appliance's cmdline becomes
an `MSKSD_<NAME>` variable in the daemon's environment, which
outranks the same key in the generated file.

## Key mapping

A config-file key is its `MSKSD_*` variable with the prefix stripped
and lowercased — one rule, no lookup table:

| Environment variable     | Config-file key    |
| ------------------------ | ------------------ |
| `MSKSD_PORT`             | `port`             |
| `MSKSD_VSOCK_SHELL_PORT` | `vsock_shell_port` |
| `MSKSD_K8S_NAMESPACE`    | `k8s_namespace`    |
| `MSKSD_EGRESS_SUBNET`    | `egress_subnet`    |
| `MSKSD_STATE_DIR`        | `state_dir`        |

The file is flat — one key per setting, no sections. For example:

```yaml
host: 0.0.0.0
port: 8660
vsock_shell_port: 1023
```

sets the same settings `MSKSD_HOST`, `MSKSD_PORT`, and
`MSKSD_VSOCK_SHELL_PORT` would. Either spelling is recoverable from
the other by the rule, and the daemon enforces it in code: the
key↔variable table is derived mechanically, so the two forms cannot
drift apart.

One mapping detail worth knowing: **`state_dir` places the database
too.** `MSKSD_STATE_DIR` feeds both the local driver's workspace
artifacts and the server's sqlite database (`<state_dir>/msks.db`);
there is no separate `db_path` key, matching the environment
variable, which also drives both.

### Native scalar types

Numeric, boolean, and string fields accept their natural YAML
scalars: `port: 8660`, `access_log: true`,
`socket_wait_timeout_s: 12.5`. Quoted strings (`port: "8660"`) work
everywhere and parse identically. A key with no value
(`bootstrap_token:`) is the unset form — the environment (for its
variable) and then the default apply. Values must be scalars: a list
or mapping where a number, boolean, or string belongs is a startup
error, and so is a duplicate key — a second `port:` does not silently
win. Merge keys (`<<: *anchor`) are refused with their own message: the
file is flat and every key is spelled out.

Booleans deserve care. `true` and `false` are the spellings to use;
PyYAML also parses the YAML 1.1 forms `yes`/`no`/`on`/`off` as
booleans, so those work too. The single letters `y` and `n` are
plain strings to YAML 1.1 — a boolean key set to either reads as
**false**, the same string rule the environment variable follows —
and a bare `1` or `0` is an integer, which also reads as false.
Write `true` or `false`.

### Unknown keys fail fast

A key the daemon does not know is a startup error naming the key and
the valid ones — a typo'd `prot` fails at boot instead of being
silently ignored. (The environment has no such guard: a typo'd
variable name is simply never read.)

Invalid values fail the same way whichever source they came from,
and the error message names the `MSKSD_*` variable —
`vmm_driver: firecracker` reports `MSKSD_VMM_DRIVER must be one of
('local', 'k8s')`. Non-finite numbers (`.nan`, `.inf`) are rejected
from either source.

## Key reference

The tables below group the keys by the subsystem that reads them;
the file itself carries them all at one level.

### The API listener

| Key               | Environment variable    | Type   | Default     | What it does                                                                                                                                                                   |
| ----------------- | ----------------------- | ------ | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `host`            | `MSKSD_HOST`            | string | `127.0.0.1` | The HTTPS + WSS listener's bind address.                                                                                                                                       |
| `port`            | `MSKSD_PORT`            | int    | `8660`      | The listener's port.                                                                                                                                                           |
| `tls_cert`        | `MSKSD_TLS_CERT`        | string | _(unset)_   | Path to the operator-provided TLS certificate. Both cert and key unset: a self-signed CA is generated on first run and its fingerprint printed for trust-on-first-use pinning. |
| `tls_key`         | `MSKSD_TLS_KEY`         | string | _(unset)_   | Path to the operator-provided TLS key.                                                                                                                                         |
| `event_poll_s`    | `MSKSD_EVENT_POLL_S`    | float  | `1.0`       | Seconds between watcher scans — workspace status reconciles and the state-disk pressure probe (#184) — read at loop start; a running daemon applies a change at restart.       |
| `bootstrap_token` | `MSKSD_BOOTSTRAP_TOKEN` | string | _(unset)_   | Seeds the first bearer token at first boot.                                                                                                                                    |
| `access_log`      | `MSKSD_ACCESS_LOG`      | bool   | `false`     | Writes uvicorn's access log. Off by default: the events websocket carries its token in the query string, which the access log would persist.                                   |

### The local cloud-hypervisor driver

| Key                       | Environment variable            | Type   | Default                | What it does                                                                                                                                                                                                                                                    |
| ------------------------- | ------------------------------- | ------ | ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vmm_driver`              | `MSKSD_VMM_DRIVER`              | string | `local`                | The backend that runs workspaces: `local` (cloud-hypervisor) or `k8s`.                                                                                                                                                                                          |
| `state_dir`               | `MSKSD_STATE_DIR`               | string | `~/.local/state/msksd` | The daemon's state directory: the sqlite database (`<state_dir>/msks.db`) and per-workspace artifacts. `~` expands.                                                                                                                                             |
| `cloud_hypervisor`        | `MSKSD_CLOUD_HYPERVISOR`        | string | `cloud-hypervisor`     | Path to the cloud-hypervisor binary the local driver execs.                                                                                                                                                                                                     |
| `socket_wait_timeout_s`   | `MSKSD_SOCKET_WAIT_TIMEOUT_S`   | float  | `10.0`                 | Seconds the driver waits for the VMM's API socket at workspace start.                                                                                                                                                                                           |
| `request_timeout_s`       | `MSKSD_REQUEST_TIMEOUT_S`       | float  | `5.0`                  | Seconds per cloud-hypervisor API request.                                                                                                                                                                                                                       |
| `shutdown_timeout_s`      | `MSKSD_SHUTDOWN_TIMEOUT_S`      | float  | `20.0`                 | Seconds a workspace stop waits for the guest to power off.                                                                                                                                                                                                      |
| `vsock_shell_port`        | `MSKSD_VSOCK_SHELL_PORT`        | int    | `1023`                 | The vsock port the guest's console shell listens on.                                                                                                                                                                                                            |
| `vsock_wait_timeout_s`    | `MSKSD_VSOCK_WAIT_TIMEOUT_S`    | float  | `15.0`                 | Seconds to wait for the guest's vsock console at boot (generous: nested-virt guests arm the device slower).                                                                                                                                                     |
| `forward_wait_timeout_s`  | `MSKSD_FORWARD_WAIT_TIMEOUT_S`  | float  | `15.0`                 | Seconds a forward websocket retries its guest dial at boot (a freshly booted guest races DHCP against its services); past the deadline the refusal names the cause.                                                                                             |
| `console_stall_timeout_s` | `MSKSD_CONSOLE_STALL_TIMEOUT_S` | float  | `60.0`                 | Seconds a console session stays open after client input drew no guest bytes; then the websocket closes with 4502. `0` disables the close. An idle session (no input) never trips it. Keep below the guest helper's own 300 s teardown so the close stays named. |
| `move_wait_timeout_s`     | `MSKSD_MOVE_WAIT_TIMEOUT_S`     | float  | `120.0`                | Seconds a boot or volume move waits for the workspace's other volume move (a stalled export reader holds its lock as long as its connection lives); the waiter answers a named 409 past the bound instead of hanging. `0` is fail-fast.                         |
| `default_image`           | `MSKSD_DEFAULT_IMAGE`           | string | _(unset)_              | A container-image tar imported into the catalog and designated default on first boot.                                                                                                                                                                           |
| `qemu_img`                | `MSKSD_QEMU_IMG`                | string | `qemu-img`             | Path to qemu-img, which builds the per-workspace root overlay.                                                                                                                                                                                                  |
| `mkfs_ext4`               | `MSKSD_MKFS_EXT4`               | string | `mkfs.ext4`            | Path to mkfs.ext4, which builds the per-workspace `/home` volume.                                                                                                                                                                                               |
| `mkisofs`                 | `MSKSD_MKISOFS`                 | string | `mkisofs`              | Path to mkisofs, which builds the `user_data` cidata seed disk.                                                                                                                                                                                                 |
| `host_name`               | `MSKSD_HOST_NAME`               | string | _(the hostname)_       | The host name recorded as owning locally-created workspaces.                                                                                                                                                                                                    |
| `root_mib`                | `MSKSD_ROOT_MIB`                | int    | `10240`                | Default workspace root overlay size, MiB (a per-create request overrides).                                                                                                                                                                                      |
| `home_mib`                | `MSKSD_HOME_MIB`                | int    | `2048`                 | Default workspace `/home` volume size, MiB (a per-create request overrides).                                                                                                                                                                                    |
| `storage_warn_pct`        | `MSKSD_STORAGE_WARN_PCT`        | int    | `90`                   | State-disk percentage used that moves pressure to `warn` (#184); 1–99.                                                                                                                                                                                          |
| `storage_floor_mib`       | `MSKSD_STORAGE_FLOOR_MIB`       | int    | `512`                  | Free state-disk MiB below which pressure is `critical` and workspace creates, image imports, and home-volume imports answer `507` (#184).                                                                                                                       |
| `ssh_key_type`            | `MSKSD_SSH_KEY_TYPE`            | string | `ed25519`              | The identity type minted at create (#111): `ed25519` (the default, #138 — FIPS-approvable, and accepted by ssh clients restricted to the common `ssh-ed25519,ssh-rsa` set), `ecdsa` (P-256), or `rsa` (3072-bit).                                               |

### The Kubernetes runner driver

| Key                         | Environment variable              | Type   | Default                      | What it does                                                                                        |
| --------------------------- | --------------------------------- | ------ | ---------------------------- | --------------------------------------------------------------------------------------------------- |
| `k8s_namespace`             | `MSKSD_K8S_NAMESPACE`             | string | `msks`                       | The namespace workspace pods and claims live in.                                                    |
| `k8s_runner_image`          | `MSKSD_K8S_RUNNER_IMAGE`          | string | `registry.k8s.io/pause:3.10` | The pause image workspace pods carry.                                                               |
| `kubeconfig`                | `MSKSD_KUBECONFIG`                | string | _(unset)_                    | A kubeconfig path for the cluster; unset uses the ambient cluster configuration.                    |
| `k8s_api_timeout_s`         | `MSKSD_K8S_API_TIMEOUT_S`         | float  | `30.0`                       | Seconds per Kubernetes API request.                                                                 |
| `k8s_storage_class`         | `MSKSD_K8S_STORAGE_CLASS`         | string | _(unset)_                    | The storage class for per-workspace PVCs; unset asks the cluster's default.                         |
| `k8s_workspace_storage_gib` | `MSKSD_K8S_WORKSPACE_STORAGE_GIB` | int    | _(unset)_                    | A fixed per-workspace PVC size, GiB; unset derives the size from the workspace's root + home disks. |

### Per-workspace egress networking

| Key                    | Environment variable         | Type   | Default         | What it does                                                                                                                                       |
| ---------------------- | ---------------------------- | ------ | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `egress_enabled`       | `MSKSD_EGRESS_ENABLED`       | bool   | `false`         | Arms per-workspace NICs, DHCP, NAT egress, and the DNS forwarder at startup (needs `CAP_NET_ADMIN`); a running daemon applies a change at restart. |
| `egress_subnet`        | `MSKSD_EGRESS_SUBNET`        | string | `172.31.0.0/16` | The IPv4 pool per-workspace /30 slices are carved from.                                                                                            |
| `egress_uplink`        | `MSKSD_EGRESS_UPLINK`        | string | `eth0`          | The appliance interface egress is NAT-masqueraded out of (the base NAT table applies at startup; per-workspace rules read the live value).         |
| `egress_dns_upstream`  | `MSKSD_EGRESS_DNS_UPSTREAM`  | string | _(unset)_       | The resolver the daemon's DNS forwarder relays to; unset reads the appliance's own `/etc/resolv.conf`.                                             |
| `ip_tool`              | `MSKSD_IP_TOOL`              | string | `ip`            | Path to the `ip` binary (taps and addresses).                                                                                                      |
| `nft_tool`             | `MSKSD_NFT_TOOL`             | string | `nft`           | Path to the `nft` binary (per-VM firewall tables).                                                                                                 |
| `egress_lease_s`       | `MSKSD_EGRESS_LEASE_S`       | int    | `3600`          | DHCP lease seconds offered to guests.                                                                                                              |
| `egress_dns_timeout_s` | `MSKSD_EGRESS_DNS_TIMEOUT_S` | float  | `3.0`           | Seconds the forwarder waits on the upstream resolver.                                                                                              |

## SIGHUP reload

Send `SIGHUP` to a running `msksd` and it re-reads the config file
(and the environment) into its live settings. Subsystems read
settings off the app's state at call time, so the swap propagates
with no per-module reconfiguration — a changed `egress_subnet` or
`k8s_namespace` applies to the next request that reads it.

Several things keep their startup values until a restart — a
reload naming a new one changes nothing:

- the listener's address, port, TLS material, and access logging
  (bound — and, for the access log, snapshotted into the listener's
  config — at startup)
- `state_dir` and the database path it places (the engine is open,
  and the local driver resolves every workspace's artifacts from the
  state dir live — moving it mid-run would orphan running
  workspaces, so the daemon latches the startup value)
- `default_image` (imported into the catalog once, at first boot)
- the watcher scan's `event_poll_s` (sampled at loop start): both the
  workspace status reconcile and the state-disk pressure probe
  (#184) ride it
- the egress machinery's startup inputs: `egress_enabled` (the
  subsystem latches its state when the daemon boots) and the base
  NAT masquerade's `egress_uplink` (per-workspace firewall rules
  read the live setting, but the base table that actually
  masquerades out the uplink keeps its startup value — change
  `egress_uplink` only with a restart scheduled)

A config that fails to load or validate is refused: the daemon
reports the error on stderr and keeps the previous settings. A
default-path file deleted since startup is likewise refused rather
than regenerated — a reload is not a first run, and regenerating the
template would silently revert every file-set value to its default.

## Notes for tooling

- A bare `alembic` CLI run derives its database URL the way a bare
  `msksd` would: the default config file when one is present (never
  generated by `alembic`), else environment variables and defaults.
  The daemon always passes its own live path programmatically. Use
  `MSKSD_CONFIG_DIR`/`MSKSD_STATE_DIR` when the hand-run migration
  must reach a database not at the default location.
- The generated template is written once, with `0700` on its
  directory, and never overwrites an existing file — a concurrent
  `msksd` that wins the race is treated as "the file is there now".
