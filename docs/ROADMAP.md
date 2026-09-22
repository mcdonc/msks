# msks roadmap

The overall goals and direction for msks, kept in the repo so decisions
survive outside any one conversation. Issues track tasks; this file
tracks intent. Update it in the same PR that changes direction.

## What msks is

A Python recreation of klangkd (the klangk daemon) that runs
workspaces as **microvms on cloud-hypervisor** instead of containers
on podman. Minimal core first; parity with klangk grows feature by
feature, not wholesale. Client surface for now is **CLI/TUI only** —
no web frontend yet.

## Standing decisions

- **Client/server split from day one.** The core (`msksd`) exposes
  API endpoints (HTTPS + WSS on one listener) and nothing else; every
  UI surface — CLI, TUI, and any future web client — is a remote
  client speaking to them over HTTPS. The client package imports
  nothing from the server package (the klangk `klangk.cli` isolation
  rule, promoted to the whole architecture), enforced by an
  import-boundary test. Consequences: auth and API versioning land
  with the first endpoints, not later; terminal streaming and consent
  events are API-level commitments (WSS); client config uses its own
  `MSKSC_*` namespace.
- **msksd runs directly on a NixOS deployment host (#229).** Every
  deployment targets a NixOS host: the daemon, cloud-hypervisor, and
  the egress stack are host software configured through the msks
  flake's NixOS module, consumed from GitHub and included into the
  host's own configuration. Rebuilding that configuration is how new
  code and configuration are tested; generations and rollback come
  from the host's NixOS system. Guest VMs run under cloud-hypervisor
  on the host's real `/dev/kvm` — one VM boundary between a
  workspace and the metal — with microvm.nix as the preferred
  plumbing for them (#237 maps the existing workspace accounting onto
  it, keeping msks-owned code only where microvm.nix has no
  equivalent). Routing and kernel configuration belong to the host's
  NixOS configuration — the state `scripts/appliance-host-setup.sh`
  installed by hand (#233); an including host needs no scripted
  setup. The development deployment host is keithmoon
  (bare metal); see the development workflow below.
  Supersession history, recorded for the record: the 2026-09
  decision required the appliance for every local deployment — no
  first-level local mode — on the grounds that the egress machinery
  (#52) was appliance software, a first-level mode would install and
  mutate the egress stack on the operator's host, and a uniform
  supervisor target kept a macOS launcher possible. The #229
  decision (2026-09) replaces it: the appliance's own machinery —
  two store shapes, an in-guest update channel, journal and
  state-disk choreography, the #212–#223 appliance build-out arc —
  is dropped as the #229 shrink lever; the same configuration runs
  natively on any NixOS host through the module, and the macOS
  launcher never shipped. The appliance layer is removed (#232) and
  the NixOS-only constraint is accepted.
- **VMM: cloud-hypervisor.** Chosen for its unix-socket REST API
  (no CLI scraping), first-class virtiofs (the podman-volume
  analogue), and snapshot/restore (pre-warmed instant-start
  workspaces later). Firecracker was rejected because it has no
  shared-filesystem device and its maintainers have twice declined to
  add one; QEMU is the documented fallback (every nix-built guest
  artifact runs on both, and the VMM is isolated behind the
  `Microvm(app)` seam, so the swap is a one-module change).
- **Rootless first.** msksd runs as an unprivileged service user in
  the `kvm` group; its privilege is confined to the service's
  capability set (`CAP_NET_ADMIN` for the egress stack,
  `CAP_NET_BIND_SERVICE` for the low ports), granted through the
  module (#231 carries the appliance-era service configuration
  forward). The setcap'd-helper idea from the appliance era is
  superseded by it. `/dev/kvm`, memfd shared memory, and unix sockets
  remain the daemon's whole hardware footprint.
- **Guests have no NIC at first.** The guest agent talks over
  **virtio-vsock**; a VM with no network device is also the strongest
  default egress posture. Taps arrived with the consent work (#52).
- **Interactive access rides stock SSH over the forward seam
  (#108–#112); the vsock console stays the failsafe.** Each workspace
  runs an sshd in the guest image (#110: `PasswordAuthentication no`,
  key-only logins, rsync shipped, host keys in the persistent overlay)
  and msksd mints a per-workspace identity at create time (#111:
  Ed25519 by default, #138 — FIPS-approvable per FIPS 186-5, #115 —
  public half seeded through `user_data`, private half served over
  the authenticated API and materialized by the client only for the
  connection's duration). The service plane is the TCP forward
  websocket (#109, landed): the caller names a guest port, the daemon
  dials it on the workspace's tap, and pumps raw bytes. Stock ssh then
  provides the shell, pty resize, flow control, agent forwarding,
  rsync/sftp, and `-L`/`-R`; msks stays a byte pipe. `msks ssh`
  (#112) wraps this with the ssh-config alias + ProxyCommand, so ssh
  rides the daemon's single authenticated listener and the daemon
  stays the only inbound path to a workspace. The vsock console
  (#21) settles into the serial-console role: present on every
  workspace, the path an operator uses when sshd is dead or the image
  needs boot-level debugging.
  In the klangk integration (#50's link-don't-dial direction), ssh is
  server-side plumbing, not the client protocol. klangkd hosts the
  forward seam and the workspace keys, and bridges its existing
  authenticated websocket to `ssh <workspace> tmux attach -t <target>`.
  tmux stays inside the guest, so shared/joined sessions and
  `window_watcher` (reading the tmux control socket through the same
  channel) keep working; the TUI's pick-a-terminal flow, the web SPA,
  and agent delivery (an agent proxy fed over the websocket — the
  pattern #108's conversation worked out when it superseded #106)
  all ride the unchanged endpoint contract. The alias is the
  power-user path, serving the CLI/TUI with the user's own ssh
  config while the browser keeps the websocket.
- **Guest images are built by nix** (kernel/initrd/rootfs as store
  paths, direct kernel boot), read-only base plus a per-VM writable
  overlay — no OCI images, no manual downloads (#5 makes this
  self-contained on any Linux host with nix). The debian guest image
  is retained under the deployment-host model and runs there (#234).
- **Egress consent is retained.** klangkd's sidecar enforcement moves
  to the **host side**: nftables + NFQUEUE on each VM's tap, DNS
  served from the DHCP-offered resolver, verdicts/TTLs/revocation in
  msksd (the sidecar-to-daemon WS leg collapses in-process). Under
  the deployment-host model the stack runs in the deployment host's
  kernel, configured through the module; guest kernels cannot tamper
  with host-side rules — strictly stronger than the in-container
  sidecar.
- **Data layer: SQLAlchemy 2.0 async ORM** on aiosqlite, Alembic
  migrations, all database access confined to a `model/` layer.
- **House rules**: the klangk conventions in `AGENTS.md` — devenv for
  everything, CI-identical `unit-tests` invocation (`-n auto`, sysmon,
  100% branch coverage), testmon for scoped iteration, xenon rank A,
  the `app`-ownership rule, `MSKSD_*` env naming.

## Development workflow

The deployment host for development is keithmoon, a bare-metal NixOS
machine. Its configuration includes the msks flake's module; the test
loop is include-the-module-and-rebuild: new msks code and
configuration land through the host's rebuild, workspaces reconcile
with the rebuild (#235), and a bad build rolls back through the
host's NixOS generations. Host-specific configuration stays out of
the repo — the module carries the msks-owned parts, the host's own
files carry everything else. Work that needs no host (the unit
suite, client work) runs from a checkout as before; the bare-host
daemon (`msksd` from a devenv shell) remains the API-only posture
without egress.

## Phases

1. **Development environment** — done (#2, PR #3).
2. **Microvm seam** — `Microvm(app)` with local (cloud-hypervisor
   REST) and k8s (runner pod) backends, review-hardened against
   the real v52 binary (#1, PR #4).
3. **Self-contained guest assets** — nix-built kernel/initrd/rootfs
   for workspace guests, smoke tests self-provision, `msks-demo-vm`
   zero-setup task (#5). The **msksd appliance image** followed the
   daemon itself (#10, after #8).
4. **CI** — the unit-test workflow, same invocation as local (#6).
5. **Daemon scaffold + API surface** — msksd around the seam:
   live-swappable settings (SIGHUP posture), model layer + Alembic,
   the `/api/v1` skeleton with auth (tokens + TLS with a trust story),
   and the WSS event channel. This is the milestone gate: no UI work
   before it.
6. **Guest agent + terminal** — vsock agent serving PTYs; exposed
   through the API as a WSS terminal stream.
7. **Networking + egress consent** — pre-created tap pool, per-VM
   nftables/NFQUEUE, consent requests and decisions as API events,
   audit rows.
8. **Clients** — the CLI and then the textual TUI, both pure API
   clients (TUI parity with klangkc's workflow list/forms/spatial
   navigation).
9. **Deployment-host model (#229)** — the rework in flight: the
   flake and its NixOS module (#231), host networking and kernel
   configuration as Nix (#233), the retained debian guest image
   running under the new setup (#234), VM lifecycle reconciliation
   driven by rebuilds (#235), the workspace-accounting inventory
   against microvm.nix (#237), the appliance removal (#232), and
   the Kubernetes removal (#236).

Later / optional: pre-warmed VMs via snapshot/restore, a web
frontend if ever wanted.

## Retired directions

- **The msksd appliance — retired 2026-09 (#229).** The nix-built
  appliance VM is removed: both store shapes (the dev virtiofs share
  and the deployed erofs base + store volume), the `#220` in-guest
  update channel, the generation trim, and the state-disk and journal
  choreography go with it (#232), along with the `msks-appliance-*`
  scripts. The appliance-era standing decision that required it is
  superseded by the deployment-host decision above.
- **Kubernetes deployment — retired 2026-09 (#229).** msks targets
  the NixOS deployment host; msks is not deployed to Kubernetes. The
  k8s driver, runner code, and their tests are removed (#236), and
  the k8s scale-out path (#15's reconcile loop, #13's scheduling
  correctness) closes with it. Earlier roadmap text describing
  the pod-based deployment — msksd as a pod, workspaces first-level
  on the node's `/dev/kvm`, the admin hand-off manifest of #7 —
  described this retired path.
- **The macOS launcher — retired 2026-09 (#229).** The
  Virtualization.framework supervisor direction is dropped;
  workspaces run with one VM boundary, and macOS machines connect as
  clients to a deployment host.
- **Docker deployment of msksd — retired 2026-09 (#229).** The
  NixOS deployment host is the local deployment target; the
  `/dev/kvm`-passthrough docker image direction (#49) closes with
  it.

## Non-goals (for now)

Full klangk feature parity, the browser frontend, OIDC/DPoP/consent
beyond egress, Kubernetes, multi-node scheduling, production
multi-tenancy.
