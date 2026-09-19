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
- **msksd always runs in a microvm — except on Kubernetes, where the
  cluster is the machine.** On a bare host the daemon is deployed as
  a nix-built appliance VM (the "msks machine", podman-machine style);
  workspace VMs run via nested KVM inside it
  (CPU host-passthrough, `/dev/kvm` in the guest). Rationale: an
  extra security layer — at least two VM boundaries between a
  workspace and the metal — and a pure client/host division: the
  physical host runs only the appliance's supervisor and exposes only
  the appliance's HTTPS listener. The host OS is irrelevant (#10
  revision): the supervisor is the repo's devenv tasks
  (`msks-appliance-build`/`-up`/`-down` driving cloud-hypervisor and
  virtiofsd from the pinned devenv shell) — no NixOS host requirement,
  and no NixOS in the guest either: the appliance image is built like
  the workspace guest (pure nixpkgs derivations, direct kernel boot),
  with its heavy runtime (the msksd closure, the VMM, workspace
  assets) arriving read-only over a virtiofs share of the host's
  /nix/store. The privileged helper (taps +
  nftables for egress consent) lives inside the appliance VM, so no
  msks-owned privileged process ever runs on the host. On Kubernetes
  the appliance layer is redundant: msksd runs as a pod, workspace
  VMs sit **first-level** on the node's real `/dev/kvm` (no nesting
  penalty — a pod is a container, not a VM), and node/pod/PVC provide
  the isolation and persistence the appliance provides locally. Costs
  accepted (bare hosts): nested-virtualization overhead on workspace
  VMs, and a second image flavor (the appliance) in the nix-built
  artifact set (#10).
  Decision (2026-09): the appliance is required for every local
  deployment; there is no first-level local mode. The deciding fact is
  the egress machinery (#52): per-VM taps, nftables/NFQUEUE consent
  enforcement, DHCP, DNS, and NAT are appliance software — a
  first-level mode would install that stack on the operator's host and
  mutate the host firewall on every workspace lifecycle. macOS hosts
  cannot run it host-side at all (the stack is Linux-specific), so the
  appliance is also what makes egress consent uniform across both
  operating systems. The two OS-specific launchers (a
  Virtualization.framework supervisor on macOS 15+/M3+, cloud-
  hypervisor + virtiofsd on Linux) implement one supervisor contract:
  boot the appliance image with the recorded devices and sockets.
- **VMM: cloud-hypervisor.** Chosen for its unix-socket REST API
  (no CLI scraping), first-class virtiofs (the podman-volume
  analogue), and snapshot/restore (pre-warmed instant-start
  workspaces later). Firecracker was rejected because it has no
  shared-filesystem device and its maintainers have twice declined to
  add one; QEMU is the documented fallback (every nix-built guest
  artifact runs on both, and the VMM is isolated behind the
  `Microvm(app)` seam, so the swap is a one-module change).
- **Rootless first.** msksd runs unprivileged: `/dev/kvm`, memfd
  shared memory, unix sockets. Privilege appears only in a small
  setcap'd helper (future) that owns taps and per-VM nftables chains.
- **Guests have no NIC at first.** The guest agent talks over
  **virtio-vsock**; a VM with no network device is also the strongest
  default egress posture. Taps arrive with the consent work.
- **Interactive access rides stock SSH over the forward seam
  (#108–#112); the vsock console stays the failsafe.** Each workspace
  runs an sshd in the guest image (#110: `PasswordAuthentication no`,
  key-only logins, rsync shipped, host keys in the persistent overlay)
  and msksd mints a per-workspace identity at create time (#111:
  Ed25519 by default, #138 — FIPS-approvable per FIPS 186-5, #115 —
  public half seeded through `user_data`, private half served over the
  authenticated API and materialized by the client only for the
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
  self-contained on any Linux host with nix).
- **Egress consent is retained.** klangkd's sidecar enforcement moves
  to the **host side**: nftables + NFQUEUE on each VM's tap, DNS
  served from the DHCP-offered resolver, verdicts/TTLs/revocation in
  msksd (the sidecar-to-daemon WS leg collapses in-process). Guest
  kernels cannot tamper with host-side rules — strictly stronger than
  the in-container sidecar.
- **Data layer: SQLAlchemy 2.0 async ORM** on aiosqlite, Alembic
  migrations, all database access confined to a `model/` layer.
- **House rules**: the klangk conventions in `AGENTS.md` — devenv for
  everything, CI-identical `unit-tests` invocation (`-n auto`, sysmon,
  100% branch coverage), testmon for scoped iteration, xenon rank A,
  the `app`-ownership rule, `MSKSD_*` env naming.
- **Kubernetes: supported, low priority, thin by design.** The k8s
  driver exists and stays working (v1 maps one VM to one pod; multiple
  VMs per runner pod is a documented later variant via an in-pod
  agent), but the product direction is the local/rootless daemon. The
  admin hand-off is the standard third-party-app pattern — one small
  namespace/ServiceAccount/Role manifest the admin applies and audits,
  one token kubeconfig handed back, then `MSKSD_VMM_DRIVER=k8s` +
  `MSKSD_KUBECONFIG` and nothing else (#7 shapes this, including a
  doctor preflight that names failures like missing node KVM). A
  KubeVirt driver (creating `VirtualMachine` CRs against a cluster
  that already runs the KubeVirt operator — the ecosystem's expected
  "VM service") is a possible later `MicrovmDriver` backend, not a
  present dependency; the seam makes it additive.

## Phases

1. **Development environment** — done (#2, PR #3).
2. **Microvm seam** — `Microvm(app)` with local (cloud-hypervisor
   REST) and k8s (runner pod) backends, review-hardened against the
   real v52 binary (#1, PR #4).
3. **Self-contained guest assets** — nix-built kernel/initrd/rootfs
   for workspace guests, smoke tests self-provision, `msks-demo-vm`
   zero-setup task (#5). The **msksd appliance image** follows the
   daemon itself (#10, after #8): an appliance with no daemon to run
   is an artifact nobody can validate.
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

Later / optional: pre-warmed VMs via snapshot/restore, docker
deployment of msksd (`/dev/kvm` passthrough), k8s runner hardening
(#7), a web frontend if ever wanted.

## Non-goals (for now)

Full klangk feature parity, the browser frontend, OIDC/DPoP/consent
beyond egress, multi-node scheduling, production multi-tenancy.
