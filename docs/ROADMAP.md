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
- **Kubernetes: supported, low priority.** The k8s driver exists as a
  day-one requirement and stays working (v1 maps one VM to one pod;
  multiple VMs per runner pod is a documented later variant via an
  in-pod agent), but the product direction is the local/rootless
  daemon. RBAC and cert-kubeconfig support are tracked in #7.

## Phases

1. **Development environment** — done (#2, PR #3).
2. **Microvm seam** — `Microvm(app)` with local (cloud-hypervisor
   REST) and k8s (runner pod) backends, review-hardened against the
   real v52 binary (#1, PR #4).
3. **Self-contained guest assets** — nix-built kernel/initrd/rootfs,
   smoke tests self-provision, `msks:demo-vm` zero-setup task (#5).
4. **CI** — the unit-test workflow, same invocation as local (#6).
5. **Daemon scaffold** — msksd around the seam: live-swappable
   settings (SIGHUP posture), model layer + Alembic, minimal auth.
6. **Guest agent + terminal** — vsock agent serving PTYs; the TUI
   bridges to it (the klangk wshandler role, minus browsers).
7. **Networking + egress consent** — pre-created tap pool, per-VM
   nftables/NFQUEUE, consent UI in the TUI, audit rows.
8. **Client polish** — textual TUI parity with klangkc's workflow
   list/forms/spatial navigation.

Later / optional: pre-warmed VMs via snapshot/restore, docker
deployment of msksd (`/dev/kvm` passthrough), k8s runner hardening
(#7), a web frontend if ever wanted.

## Non-goals (for now)

Full klangk feature parity, the browser frontend, OIDC/DPoP/consent
beyond egress, multi-node scheduling, production multi-tenancy.
