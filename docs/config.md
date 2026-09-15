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
| `msksd --config=none`                | Reads environment variables and built-in defaults only — the deployment shape for appliance and container runs that manage config out-of-band.                                                                                                                                                                              |

`MSKSD_CONFIG_DIR` is read before anything else: the config file
cannot relocate the directory it lives in, so the tree root must be
resolvable before the file is located.

## Key mapping

The file mirrors the settings tree: four sections, one per settings
group, and keys named after the settings fields in `snake_case`.
Every key has a matching `MSKSD_*` environment variable (the tables
below give both). For example:

```yaml
server:
  host: 0.0.0.0
  port: 8660
vmm:
  vsock_shell_port: 1023
```

sets the same settings `MSKSD_HOST`, `MSKSD_PORT`, and
`MSKSD_VSOCK_SHELL_PORT` would.

Two mapping details worth knowing:

- **`vmm.state_dir` places the database too.** `MSKSD_STATE_DIR`
  feeds both the local driver's workspace artifacts and the server's
  sqlite database (`<state_dir>/msks.db`). The key lives under `vmm:`
  and there is no separate `db_path` key — matching the environment
  variable, which also drives both.
- **`net:` keys use the field names.** The egress variables carry an
  `EGRESS_` prefix (`MSKSD_EGRESS_SUBNET`), but the file keys are the
  field names: `pool`, `uplink`, `enabled`.

### Native scalar types

Numeric, boolean, and string fields accept their natural YAML
scalars: `port: 8660`, `access_log: true`,
`socket_wait_timeout_s: 12.5`. Quoted strings (`port: "8660"`) work
everywhere and parse identically. A key with no value
(`bootstrap_token:`) is the unset form — the environment (for its
variable) and then the default apply. Values must be scalars: a list
or mapping where a number, boolean, or string belongs is a startup
error.

### Unknown keys fail fast

A section or key the daemon does not know is a startup error naming
the key and the valid ones — a typo'd `server.prot` fails at boot
instead of being silently ignored. (The environment has no such
guard: a typo'd variable name is simply never read.)

Invalid values fail the same way whichever source they came from,
and the error message names the `MSKSD_*` variable — `vmm.driver:
firecracker` reports `MSKSD_VMM_DRIVER must be one of ('local',
'k8s')`.

## Key reference

### `vmm:` — the local cloud-hypervisor driver

| Key                     | Environment variable          | Type   | Default                | What it does                                                                                                        |
| ----------------------- | ----------------------------- | ------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `driver`                | `MSKSD_VMM_DRIVER`            | string | `local`                | The backend that runs workspaces: `local` (cloud-hypervisor) or `k8s`.                                              |
| `cloud_hypervisor`      | `MSKSD_CLOUD_HYPERVISOR`      | string | `cloud-hypervisor`     | Path to the cloud-hypervisor binary the local driver execs.                                                         |
| `state_dir`             | `MSKSD_STATE_DIR`             | string | `~/.local/state/msksd` | The daemon's state directory: the sqlite database (`<state_dir>/msks.db`) and per-workspace artifacts. `~` expands. |
| `socket_wait_timeout_s` | `MSKSD_SOCKET_WAIT_TIMEOUT_S` | float  | `10.0`                 | Seconds the driver waits for the VMM's API socket at workspace start.                                               |
| `request_timeout_s`     | `MSKSD_REQUEST_TIMEOUT_S`     | float  | `5.0`                  | Seconds per cloud-hypervisor API request.                                                                           |
| `shutdown_timeout_s`    | `MSKSD_SHUTDOWN_TIMEOUT_S`    | float  | `20.0`                 | Seconds a workspace stop waits for the guest to power off.                                                          |
| `vsock_shell_port`      | `MSKSD_VSOCK_SHELL_PORT`      | int    | `1023`                 | The vsock port the guest's console shell listens on.                                                                |
| `vsock_wait_timeout_s`  | `MSKSD_VSOCK_WAIT_TIMEOUT_S`  | float  | `15.0`                 | Seconds to wait for the guest's vsock console at boot (generous: nested-virt guests arm the device slower).         |
| `default_image`         | `MSKSD_DEFAULT_IMAGE`         | string | _(unset)_              | A container-image tar imported into the catalog and designated default on first boot.                               |
| `qemu_img`              | `MSKSD_QEMU_IMG`              | string | `qemu-img`             | Path to qemu-img, which builds the per-workspace root overlay.                                                      |
| `mkfs_ext4`             | `MSKSD_MKFS_EXT4`             | string | `mkfs.ext4`            | Path to mkfs.ext4, which builds the per-workspace `/home` volume.                                                   |
| `mkisofs`               | `MSKSD_MKISOFS`               | string | `mkisofs`              | Path to mkisofs, which builds the `user_data` cidata seed disk.                                                     |
| `host_name`             | `MSKSD_HOST_NAME`             | string | _(the hostname)_       | The host name recorded as owning locally-created workspaces.                                                        |
| `root_mib`              | `MSKSD_ROOT_MIB`              | int    | `10240`                | Default workspace root overlay size, MiB (a per-create request overrides).                                          |
| `home_mib`              | `MSKSD_HOME_MIB`              | int    | `2048`                 | Default workspace `/home` volume size, MiB (a per-create request overrides).                                        |

### `server:` — the API listener

| Key               | Environment variable    | Type   | Default     | What it does                                                                                                                                                                   |
| ----------------- | ----------------------- | ------ | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `host`            | `MSKSD_HOST`            | string | `127.0.0.1` | The HTTPS + WSS listener's bind address.                                                                                                                                       |
| `port`            | `MSKSD_PORT`            | int    | `8660`      | The listener's port.                                                                                                                                                           |
| `tls_cert`        | `MSKSD_TLS_CERT`        | string | _(unset)_   | Path to the operator-provided TLS certificate. Both cert and key unset: a self-signed CA is generated on first run and its fingerprint printed for trust-on-first-use pinning. |
| `tls_key`         | `MSKSD_TLS_KEY`         | string | _(unset)_   | Path to the operator-provided TLS key.                                                                                                                                         |
| `event_poll_s`    | `MSKSD_EVENT_POLL_S`    | float  | `1.0`       | Seconds between workspace status scans (read at loop start; a running daemon applies a change at restart).                                                                     |
| `bootstrap_token` | `MSKSD_BOOTSTRAP_TOKEN` | string | _(unset)_   | Seeds the first bearer token at first boot.                                                                                                                                    |
| `access_log`      | `MSKSD_ACCESS_LOG`      | bool   | `false`     | Writes uvicorn's access log. Off by default: the events websocket carries its token in the query string, which the access log would persist.                                   |

### `k8s:` — the Kubernetes runner driver

| Key                     | Environment variable              | Type   | Default                      | What it does                                                                                        |
| ----------------------- | --------------------------------- | ------ | ---------------------------- | --------------------------------------------------------------------------------------------------- |
| `namespace`             | `MSKSD_K8S_NAMESPACE`             | string | `msks`                       | The namespace workspace pods and claims live in.                                                    |
| `runner_image`          | `MSKSD_K8S_RUNNER_IMAGE`          | string | `registry.k8s.io/pause:3.10` | The pause image workspace pods carry.                                                               |
| `kubeconfig`            | `MSKSD_KUBECONFIG`                | string | _(unset)_                    | A kubeconfig path for the cluster; unset uses the ambient cluster configuration.                    |
| `api_timeout_s`         | `MSKSD_K8S_API_TIMEOUT_S`         | float  | `30.0`                       | Seconds per Kubernetes API request.                                                                 |
| `storage_class`         | `MSKSD_K8S_STORAGE_CLASS`         | string | _(unset)_                    | The storage class for per-workspace PVCs; unset asks the cluster's default.                         |
| `workspace_storage_gib` | `MSKSD_K8S_WORKSPACE_STORAGE_GIB` | int    | _(unset)_                    | A fixed per-workspace PVC size, GiB; unset derives the size from the workspace's root + home disks. |

### `net:` — per-workspace egress networking

| Key             | Environment variable         | Type   | Default         | What it does                                                                                           |
| --------------- | ---------------------------- | ------ | --------------- | ------------------------------------------------------------------------------------------------------ |
| `enabled`       | `MSKSD_EGRESS_ENABLED`       | bool   | `false`         | Arms per-workspace NICs, DHCP, NAT egress, and the DNS forwarder; needs `CAP_NET_ADMIN`.               |
| `pool`          | `MSKSD_EGRESS_SUBNET`        | string | `172.31.0.0/16` | The IPv4 pool per-workspace /30 slices are carved from.                                                |
| `uplink`        | `MSKSD_EGRESS_UPLINK`        | string | `eth0`          | The appliance interface egress is NAT-masqueraded out of.                                              |
| `dns_upstream`  | `MSKSD_EGRESS_DNS_UPSTREAM`  | string | _(unset)_       | The resolver the daemon's DNS forwarder relays to; unset reads the appliance's own `/etc/resolv.conf`. |
| `ip_tool`       | `MSKSD_IP_TOOL`              | string | `ip`            | Path to the `ip` binary (taps and addresses).                                                          |
| `nft_tool`      | `MSKSD_NFT_TOOL`             | string | `nft`           | Path to the `nft` binary (per-VM firewall tables).                                                     |
| `lease_s`       | `MSKSD_EGRESS_LEASE_S`       | int    | `3600`          | DHCP lease seconds offered to guests.                                                                  |
| `dns_timeout_s` | `MSKSD_EGRESS_DNS_TIMEOUT_S` | float  | `3.0`           | Seconds the forwarder waits on the upstream resolver.                                                  |

## SIGHUP reload

Send `SIGHUP` to a running `msksd` and it re-reads the config file
(and the environment) into its live settings. Subsystems read
settings off the app's state at call time, so the swap propagates
with no per-module reconfiguration — a changed `net.pool` or
`k8s.namespace` applies to the next request that reads it.

Three things keep their startup values until a restart:

- the listener's address, port, and TLS material (bound at startup)
- the database path (the engine is open)
- long-lived loops that sampled their interval at start (the
  workspace status scan's `event_poll_s`)

A config that fails to load or validate is refused: the daemon
reports the error on stderr and keeps the previous settings.

## Notes for tooling

- A bare `alembic` CLI run derives its database URL from environment
  variables only (the daemon always passes its own live path
  programmatically). Point `MSKSD_STATE_DIR` at the same tree the
  config file names when running migrations by hand.
- The generated template is written once, with `0700` on its
  directory, and never overwrites an existing file — a concurrent
  `msksd` that wins the race is treated as "the file is there now".
