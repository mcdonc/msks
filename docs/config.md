# The msksd configuration file

`msksd` reads its settings from a YAML file, from `MSKSD_*`
environment variables, and from built-in defaults. The file is the
durable home for a deployment's settings, committed to the
deployment's own storage and human-reviewable, while the
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
| `msksd --config=none`                | Reads environment variables and built-in defaults only — the in-tree user is the smoke-test daemon; the deployment-host module passes an explicit `--config` path.                                                                                                                                                          |

`MSKSD_CONFIG_DIR` is read before anything else and exists only as
an environment variable: the config file cannot relocate the
directory it lives in, so the tree root must be resolvable before
the file is located.

## `msksd doctor`

`msksd doctor` checks the host for the tools the daemon and the
client exec, reading the config the same way the daemon does —
the `--config` flag, the environment, and the file, under the
precedence above. The [host doctor chapter](doctor.md) owns the
check rows, the report's markers and hints, and the exit codes.

## Key mapping

A config-file key is its `MSKSD_*` variable with the prefix stripped
and lowercased, one rule with no lookup table:

| Environment variable  | Config-file key |
| --------------------- | --------------- |
| `MSKSD_PORT`          | `port`          |
| `MSKSD_EGRESS_SUBNET` | `egress_subnet` |
| `MSKSD_STATE_DIR`     | `state_dir`     |

The file is flat: one key per setting, no sections. For
example:

```yaml
host: 0.0.0.0
port: 8660
```

sets the same settings `MSKSD_HOST`, `MSKSD_PORT`, and
`MSKSD_CONSOLE_WAIT_TIMEOUT_S` would. Either spelling is recoverable from
the other by the rule, and the daemon enforces it in code: the
key↔variable table is derived mechanically, so the two forms cannot
drift apart.

The underscore is the canonical spelling, and a hyphen may be
written for it: `egress-dns-upstream` and `egress_dns_upstream`
are one key spelled two ways. The `llm_models` entries' keys
follow the same rule, so an operator used to kebab-case YAML
can write it naturally. Both spellings of one key in the same file is a startup
error naming the two spellings, and the valid-keys list an unknown
key reports is the snake_case one.

One mapping detail worth knowing: **`state_dir` places the database
too.** `state_dir` feeds both the local driver's workspace
artifacts and the server's sqlite database (`<state_dir>/msks.db`);
there is no separate `db_path` key, matching the environment
variable, which also drives both.

### Native scalar types

Numeric, boolean, and string fields accept their natural YAML
scalars: `port: 8660`, `access_log: true`,
`socket_wait_timeout_s: 12.5`. Quoted strings (`port: "8660"`) work
everywhere and parse identically. A key with no value
(`bootstrap_token:`) is the unset form; the environment (for
its
variable) and then the default apply. Values must be scalars: a list
or mapping where a number, boolean, or string belongs is a startup
error, and so is a duplicate key: a second `port:` does not
silently
win. Merge keys (`<<: *anchor`) are refused with their own message: the
file is flat and every key is spelled out.

Booleans deserve care. `true` and `false` are the spellings to use;
PyYAML also parses the YAML 1.1 forms `yes`/`no`/`on`/`off` as
booleans, so those work too. The single letters `y` and `n` are plain strings to YAML 1.1,
so a boolean key set to either reads as **false**; the
environment variable follows the same string rule. A bare `1`
or `0` is an integer, which also reads as false.
Write `true` or `false`.

### Unknown keys fail fast

A key the daemon does not know is a startup error naming the key and
the valid ones, so a typo'd `prot` fails at boot instead of
being
silently ignored. The environment has no such guard: a typo'd variable name is
simply never read. Invalid values fail the same way whichever
source they came from, and the error message names the
`MSKSD_*` variable —
`vmm_driver: firecracker` reports `MSKSD_VMM_DRIVER must be one of
('local',)`. Non-finite numbers (`.nan`, `.inf`) are rejected
from either source.

## Key reference

The tables below group the keys by the subsystem that reads them;
the file itself carries them all at one level.

### The API listener

| Key               | Environment variable    | Type   | Default     | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ----------------- | ----------------------- | ------ | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `host`            | `MSKSD_HOST`            | string | `127.0.0.1` | The HTTPS + WSS listener's bind address.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `port`            | `MSKSD_PORT`            | int    | `8660`      | The listener's port.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `socket`          | `MSKSD_SOCKET`          | string | _(unset)_   | Binds a second, local listener at this unix socket path: same-host clients connect through it with peer-credential authentication — no token, no TLS. Unset binds the default path, `$XDG_RUNTIME_DIR/msks/msksd.sock`; a daemon without a runtime dir (a container, a hand-written system unit pointing `MSKSD_SOCKET` at a path its `RuntimeDirectory` creates) serves TCP alone until this setting names a path; the empty string always serves TCP alone. The socket file is mode 0660 — chgrp it to a shared group to admit that group's members. The systemd **user** unit `msks-setup-debian-host` generates creates `$XDG_RUNTIME_DIR/msks` for this listener through its `RuntimeDirectory` — the client's own default candidate path. Read at startup; a reload applies a change at restart. |
| `tls_cert`        | `MSKSD_TLS_CERT`        | string | _(unset)_   | Path to the operator-provided TLS certificate. Both cert and key unset: a self-signed CA is generated on first run and its fingerprint printed for trust-on-first-use pinning.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `tls_key`         | `MSKSD_TLS_KEY`         | string | _(unset)_   | Path to the operator-provided TLS key.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `event_poll_s`    | `MSKSD_EVENT_POLL_S`    | float  | `1.0`       | Seconds between watcher scans — workspace status reconciles and the state-disk pressure probe — read at loop start; a running daemon applies a change at restart.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `bootstrap_token` | `MSKSD_BOOTSTRAP_TOKEN` | string | _(unset)_   | Seeds the first bearer token at first boot. The plaintext must fit the HTTP `token` grammar (letters, digits, and the separator characters RFC 9110 allows): the token rides the `Authorization` header on the REST surface and the websocket handshake alike, whose value grammar rejects the rest.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `access_log`      | `MSKSD_ACCESS_LOG`      | bool   | `false`     | Writes uvicorn's access log — request lines (method, path, query), never headers. Websocket tokens ride the `Authorization` header, not the URL, so the log holds no credentials.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |

### The local cloud-hypervisor driver

| Key                       | Environment variable            | Type   | Default                | What it does                                                                                                                                                                                                                                                           |
| ------------------------- | ------------------------------- | ------ | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vmm_driver`              | `MSKSD_VMM_DRIVER`              | string | `local`                | The backend that runs workspaces: `local` (cloud-hypervisor).                                                                                                                                                                                                          |
| `state_dir`               | `MSKSD_STATE_DIR`               | string | `~/.local/state/msksd` | The daemon's state directory: the sqlite database (`<state_dir>/msks.db`) and per-workspace artifacts. `~` expands.                                                                                                                                                    |
| `cloud_hypervisor`        | `MSKSD_CLOUD_HYPERVISOR`        | string | `cloud-hypervisor`     | Path to the cloud-hypervisor binary the local driver execs.                                                                                                                                                                                                            |
| `socket_wait_timeout_s`   | `MSKSD_SOCKET_WAIT_TIMEOUT_S`   | float  | `10.0`                 | Seconds the driver waits for the VMM's API socket at workspace start.                                                                                                                                                                                                  |
| `request_timeout_s`       | `MSKSD_REQUEST_TIMEOUT_S`       | float  | `5.0`                  | Seconds per cloud-hypervisor API request.                                                                                                                                                                                                                              |
| `shutdown_timeout_s`      | `MSKSD_SHUTDOWN_TIMEOUT_S`      | float  | `20.0`                 | Seconds a workspace stop waits for the guest to power off.                                                                                                                                                                                                             |
| `console_wait_timeout_s`  | `MSKSD_CONSOLE_WAIT_TIMEOUT_S`  | float  | `15.0`                 | Seconds to wait for the guest's console device at boot (generous: nested-virt guests arm the device slower).                                                                                                                                                           |
| `forward_wait_timeout_s`  | `MSKSD_FORWARD_WAIT_TIMEOUT_S`  | float  | `15.0`                 | Seconds a forward websocket retries its guest dial at boot (a freshly booted guest races DHCP against its services); past the deadline the refusal names the cause.                                                                                                    |
| `console_stall_timeout_s` | `MSKSD_CONSOLE_STALL_TIMEOUT_S` | float  | `60.0`                 | Seconds a console session stays open after client input drew no guest bytes; then the websocket closes with 4502. `0` disables the close. An idle session (no input) never trips it.                                                                                   |
| `move_wait_timeout_s`     | `MSKSD_MOVE_WAIT_TIMEOUT_S`     | float  | `120.0`                | Seconds a boot or volume move waits for the workspace's other volume move (a stalled export reader holds its lock as long as its connection lives); the waiter answers a named 409 past the bound instead of hanging. `0` is fail-fast.                                |
| `default_image`           | `MSKSD_DEFAULT_IMAGE`           | string | _(unset)_              | A container-image tar imported into the catalog and designated default on first boot.                                                                                                                                                                                  |
| `seed_images`             | `MSKSD_SEED_IMAGES`             | list   | _(unset)_              | Container-image tars imported into the catalog at every start, warm when unchanged; never designated default. A bare-name image reference resolves the newest version, with import recency breaking ties between builds whose versions carry the same leading numbers. |
| `qemu_img`                | `MSKSD_QEMU_IMG`                | string | `qemu-img`             | Path to qemu-img, which builds the per-workspace root overlay.                                                                                                                                                                                                         |
| `mkfs_ext4`               | `MSKSD_MKFS_EXT4`               | string | `mkfs.ext4`            | Path to mkfs.ext4, which builds the per-workspace `/home` volume.                                                                                                                                                                                                      |
| `resize2fs`               | `MSKSD_RESIZE2FS`               | string | `resize2fs`            | Path to resize2fs, which moves a `/home` volume's size.                                                                                                                                                                                                                |
| `e2fsck`                  | `MSKSD_E2FSCK`                  | string | `e2fsck`               | Path to e2fsck, which quiets a volume before a resize.                                                                                                                                                                                                                 |
| `mkisofs`                 | `MSKSD_MKISOFS`                 | string | `mkisofs`              | Path to mkisofs, which builds the `user_data` cidata seed disk.                                                                                                                                                                                                        |
| `host_name`               | `MSKSD_HOST_NAME`               | string | _(the hostname)_       | The host name recorded as owning locally-created workspaces.                                                                                                                                                                                                           |
| `root_mib`                | `MSKSD_ROOT_MIB`                | int    | `10240`                | Default workspace root overlay size, MiB (a per-create request overrides).                                                                                                                                                                                             |
| `home_mib`                | `MSKSD_HOME_MIB`                | int    | `20480`                | Default workspace `/home` volume size, MiB (a per-create request overrides).                                                                                                                                                                                           |
| `storage_warn_pct`        | `MSKSD_STORAGE_WARN_PCT`        | int    | `90`                   | State-disk percentage used that moves pressure to `warn`; 1–99.                                                                                                                                                                                                        |
| `storage_floor_mib`       | `MSKSD_STORAGE_FLOOR_MIB`       | int    | `512`                  | Free state-disk MiB below which pressure is `critical` and workspace creates, image imports, and home-volume imports answer `507`.                                                                                                                                     |
| `image_import_max_mib`    | `MSKSD_IMAGE_IMPORT_MAX_MIB`    | int    | `8192`                 | The size ceiling a URL image import accepts: a download whose declared or streamed size passes it is refused by name. The storage floor still counts the imported archive twice, like a path import.                                                                   |
| `image_import_timeout_s`  | `MSKSD_IMAGE_IMPORT_TIMEOUT_S`  | float  | `600.0`                | The overall deadline a URL image import download runs under; a slow source fails with a named error instead of hanging the request.                                                                                                                                    |

### Per-workspace egress networking

| Key                             | Environment variable                  | Type   | Default         | What it does                                                                                                                                                                                                                                       |
| ------------------------------- | ------------------------------------- | ------ | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `egress_enabled`                | `MSKSD_EGRESS_ENABLED`                | bool   | `false`         | Arms per-workspace NICs, DHCP, NAT egress, and the DNS forwarder at startup (needs `CAP_NET_ADMIN`); a running daemon applies a change at restart.                                                                                                 |
| `egress_subnet`                 | `MSKSD_EGRESS_SUBNET`                 | string | `172.31.0.0/16` | The IPv4 pool per-workspace /30 slices are carved from.                                                                                                                                                                                            |
| `egress_uplink`                 | `MSKSD_EGRESS_UPLINK`                 | string | `eth0`          | The host interface egress is NAT-masqueraded out of (the base NAT table applies at startup; per-workspace rules read the live value).                                                                                                              |
| `egress_dns_upstream`           | `MSKSD_EGRESS_DNS_UPSTREAM`           | string | _(unset)_       | The resolver the daemon's DNS forwarder relays to; unset reads the host's own `/etc/resolv.conf`.                                                                                                                                                  |
| `ip_tool`                       | `MSKSD_IP_TOOL`                       | string | `ip`            | Path to the `ip` binary (taps and addresses).                                                                                                                                                                                                      |
| `nft_tool`                      | `MSKSD_NFT_TOOL`                      | string | `nft`           | Path to the `nft` binary (per-VM firewall tables).                                                                                                                                                                                                 |
| `egress_lease_s`                | `MSKSD_EGRESS_LEASE_S`                | int    | `3600`          | DHCP lease seconds offered to guests.                                                                                                                                                                                                              |
| `egress_dns_timeout_s`          | `MSKSD_EGRESS_DNS_TIMEOUT_S`          | float  | `3.0`           | Seconds the forwarder waits on the upstream resolver.                                                                                                                                                                                              |
| `egress_mode`                   | `MSKSD_EGRESS_MODE`                   | string | `allow`         | The consent mode workspaces get at create when the request names none: `allow`, `static`, or `interactive`.                                                                                                                                        |
| `egress_consent_timeout_s`      | `MSKSD_EGRESS_CONSENT_TIMEOUT_S`      | float  | `120.0`         | Seconds a held first packet waits for a decider before the hold expires to a deny (inside the kernel's ~127 s SYN budget).                                                                                                                         |
| `egress_consent_rate_limit`     | `MSKSD_EGRESS_CONSENT_RATE_LIMIT`     | int    | `8`             | The per-workspace pending-prompt cap (the prompt-spam bound); `0` removes it.                                                                                                                                                                      |
| `egress_consent_retention_days` | `MSKSD_EGRESS_CONSENT_RETENTION_DAYS` | int    | `30`            | Days a consent row lives past its terminal timestamp; `0` disables retention pruning.                                                                                                                                                              |
| `egress_consent_row_cap`        | `MSKSD_EGRESS_CONSENT_ROW_CAP`        | int    | `1000`          | The per-workspace consent-row ceiling (the flood bound); `0` disables the cap.                                                                                                                                                                     |
| `egress_queue_base`             | `MSKSD_EGRESS_QUEUE_BASE`             | int    | `1024`          | The base per-workspace NFQUEUE numbers derive from (`base + pool slice`); must leave room under 65535.                                                                                                                                             |
| `conntrack_tool`                | `MSKSD_CONNTRACK_TOOL`                | string | `conntrack`     | The tool revocation uses to drop a revoked destination's established connections.                                                                                                                                                                  |
| `interceptor_port`              | `MSKSD_INTERCEPTOR_PORT`              | int    | `8643`          | The TCP port every armed workspace's interceptor listener binds on its own tap address.                                                                                                                                                            |
| `probe_port`                    | `MSKSD_PROBE_PORT`                    | int    | `8644`          | The TCP port every workspace's probe service listener binds on its own tap address. The guest still dials 443 (the redirect catches it); the interceptor repoints its upstream dial to this port, so a host service on 443 cannot break the probe. |
| `audit_hmac_key`                | `MSKSD_AUDIT_HMAC_KEY`                | string | _(unset)_       | When set, every consent row is written with an HMAC-SHA256 tag over its data columns (tamper-evident audit); unset stores no tags.                                                                                                                 |

### Host share mounts

The named host trees a workspace may mount into its guest ([mounts
chapter](mounts.md) for the behavior). Each entry carries `name`
and `path`, and two bounds: `writable` (default `true`) and
`multi` (default `true`).

| Key            | Environment variable | Type   | Default     | What it does                                                                                                                                                                                                                                                                             |
| -------------- | -------------------- | ------ | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mount_shares` | `MSKSD_MOUNT_SHARES` | list   | _(unset)_   | The shares: entries of `name`, `path`, `writable` (default `true` — a listed share serves read-write), and `multi` (default `true` — a share attaches read-write to any number of workspaces; `false` makes read-write attaches exclusive). The environment form is a JSON array string. |
| `virtiofsd`    | `MSKSD_VIRTIOFSD`    | string | `virtiofsd` | Path to the virtiofsd binary each attached share spawns (Debian's `virtiofsd` package; an older suite's build under `/usr/libexec` is named here).                                                                                                                                       |

### The placeholder secret store

Where the real secrets behind placeholder tokens live ([secrets
chapter](secrets.md) for the flow). The store is one
per-recipient ciphertext blobs; plaintext returns only through an
operator's unlock
when the first store operation finds it absent.

| Key                             | Environment variable                  | Type        | Default   | What it does                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------- | ------------------------------------- | ----------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `secret_store_unlock_timeout_s` | `MSKSD_SECRET_STORE_UNLOCK_TIMEOUT_S` | float       | `86400.0` | How long unlocked plaintext lives in the daemon's cache after the unlock POST that supplied it; the timer resets on an unlock itself — websocket traffic and keepalives never extend it. Zero holds plaintext only while at least one operator websocket stays connected — the last disconnect drops it, and an unlock over HTTP alone holds nothing past the reply. |
| `llm_port`                      | `MSKSD_LLM_PORT`                      | int         | `8770`    | The port each workspace's LLM proxy listener binds on its tap.                                                                                                                                                                                                                                                                                                       |
| `llm_models`                    | `MSKSD_LLM_MODELS`                    | string/list | `""`      | The model list: `provider/model:api_base:api_key` entries — comma-separated strings, or the file's list of strings and LiteLLM-native dicts; see `docs/llm.md`.                                                                                                                                                                                                      |
| `llm_api_key`                   | `MSKSD_LLM_API_KEY`                   | string      | `""`      | A default key for entries that name none; `file:`/`cmd:` indirection works.                                                                                                                                                                                                                                                                                          |

## SIGHUP reload

Send `SIGHUP` to a running `msksd` and it re-reads the config file
(and the environment) into its live settings. Subsystems read
settings off the app's state at call time, so the swap propagates
with no per-module reconfiguration: a changed `egress_subnet`
or
`host_name` applies to the next request that reads it.

Several things keep their startup values until a restart. A
reload naming a new one changes nothing:

- the listener's address, port, TLS material, and access
  logging, all bound at startup; the access log is also
  snapshotted into the listener's config
- `state_dir` and the database path it places: the engine is
  open, and the local driver resolves every workspace's
  artifacts from the state dir live, so the daemon latches the
  startup value (moving it mid-run would orphan running
  workspaces)
- `default_image` (imported into the catalog once, at first boot)
- the watcher scan's `event_poll_s` (sampled at loop start): both the
  workspace status reconcile and the state-disk pressure probe
  ride it
- the egress machinery's startup inputs. `egress_enabled`
  latches the subsystem's state when the daemon boots. The base
  NAT masquerade's `egress_uplink` also latches: per-workspace
  firewall rules read the live setting, but the base table
  that actually masquerades out the uplink keeps its startup
  value, so change `egress_uplink` only with a restart
  scheduled.

One reload does re-stamp the base table. A change that alters
the LLM proxy's loopback guard shape (`llm_models` gaining or
losing entries, `llm_port`, `egress_subnet`) re-applies the
table at the next workspace start, masquerade included, from
the live `egress_uplink`.

`mount_shares` reads live after a reload: each workspace's next
start derives its attachments from the shares the reload left —
a share the reload removed reads as a skip at start (the
[mounts chapter](mounts.md) names the skip behavior), and a
share the reload narrowed to read-only refuses the workspace's
`rw` mount the same way. Running VMs keep the devices they
attached. `virtiofsd` reads live too: the next start spawns the
binary the reload names.

A config that fails to load or validate is refused: the daemon
reports the error on stderr and keeps the previous settings. A
default-path file deleted since startup is likewise refused rather
than regenerated, because a reload is not a first run and
regenerating the
template would silently revert every file-set value to its default.
