# Cloning workspaces

Cloning copies one workspace's state into a new workspace. The new
workspace boots with a copy of the source's disks and the source's create-time
configuration, under its own id and a new name.

Use the CLI form `msks clone ws ws2` to clone `ws` into `ws2`. The TUI's
workspace page offers the same action (`Clone`) with a name prompt.

## What a clone carries

- **Both disks.** The daemon copies the root overlay and the
  `/home` volume. Everything the guest wrote arrives with the
  clone: installed packages, `/etc` changes, repos and files
  under `/home`. The copy is a reflink where the state
  filesystem supports one (the blocks are shared, the copy costs
  its metadata).
- **The create-time config.** The root and home sizes, the CPU and memory
  topology, the egress mode, the host-share mounts, the login user, and the
  `user_data` payload all copy. The clone's own factory reset re-provisions
  from the payload, exactly like the source's would.

## What stays behind

- **The egress allowlist.** The clone's list starts empty. Its
  posture is the mode it inherited over nothing allowed, so a
  fork under `interactive` mode earns its own destinations. A
  fork under `static` mode answers every name NXDOMAIN until the
  operator sets its list — an offline workspace reachable only
  by ssh, the same posture `msks egress mode static --offline`
  builds. `msks clone seed fork --copy-allowlist` opts the clone
  into the source's list verbatim, for the seed-to-fleet case
  where the seed's approved set is meant to propagate.
- **In-effect egress verdicts.** Every `allow forever` the source
  earned under `interactive` mode stays with the source. The
  clone re-earns each allow, with or without `--copy-allowlist`.
- **Workspace-scoped secret placeholders.** A placeholder minted
  against the source's id does not swap on the clone's tap. Mint
  or re-scope one for the clone when it needs the credential.
  Daemon-wide placeholders cover the clone with nothing to
  decide, like they cover every workspace.

## The stopped-source rule

The source must be stopped to be cloned. The daemon refuses a clone of a
running workspace.

## In-guest secrets duplicate

A clone copies everything the source's disks carry, including secrets
the guest generated itself. The clone and the source carry the
same ssh host keys, the same `machine-id`, and the same
application secrets any first-boot process minted (session
signing keys, self-signed certificates). Anything that trusts the
source's identity silently trusts the clone too: a
`known_hosts` entry, a deploy key.

There is no automatic regeneration. Rotate what matters before
trusting the clone as a separate machine: regenerate the guest's
ssh host keys, re-key anything the workspace registered with an
external service, or treat the clone as the same trust domain as
its source.

## Typical uses

- **Seed workspace.** Prepare one workspace with the toolchain,
  repos, and login state, then clone it per task or per agent
  with `--copy-allowlist`: the seed's approved destinations
  propagate, and each clone still re-earns its interactive
  verdicts.
- **Experiment fork.** Clone before a risky session; the
  original keeps its state while the clone takes the risk.
