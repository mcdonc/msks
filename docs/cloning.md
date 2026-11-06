# Cloning workspaces

`msks clone seed task-42` copies one stopped workspace into a new
one. A clone is a full fork: the new workspace boots with the
source's disks and the source's create-time configuration, under
its own freshly minted id and the name you gave it. The TUI's
workspace page offers the same action (`Clone`) with a name
prompt.

## What a clone carries

- **Both disks.** The daemon copies the root overlay and the
  `/home` volume. Everything the guest wrote — installed
  packages, `/etc` changes, repos and files under `/home` —
  arrives with the clone. The copy is a reflink where the state
  filesystem supports one (the blocks are shared, the copy costs
  its metadata); a plain windowed copy serves elsewhere at the
  source's allocated cost, and sparse files stay sparse.
- **The create-time config.** The image and its catalog pin, the
  root and home sizes, the CPU and memory topology, the egress
  mode, the host-share mounts, the login user, and the `user_data`
  payload all copy. The clone's own factory reset re-provisions
  from the payload, exactly like the source's would.
- **The ssh identity's key.** The operator's public key copies
  verbatim (it is the operator's key, not the workspace's); only
  the daemon's provenance comment names the clone.

## What stays behind

- **The egress allowlist.** The clone's list starts empty. Its
  posture is the mode it inherited over nothing allowed, so a
  fork doing different things than its source earns its own
  destinations. `msks clone seed fork --copy-allowlist` opts the
  clone into the source's list verbatim, for the seed-to-fleet
  case where the seed's approved set is meant to propagate.
- **In-effect egress verdicts.** Every `allow forever` the source
  earned under `interactive` mode stays with the source. The
  clone re-earns each allow, with or without `--copy-allowlist`.
- **Workspace-scoped secret placeholders.** A placeholder minted
  against the source's id does not swap on the clone's tap. Mint
  or re-scope one for the clone when it needs the credential.
  Daemon-wide placeholders cover the clone with nothing to
  decide, like they cover every workspace.
- **The egress address slice and the attached-mounts record.**
  The clone's tap gets its own address at its first attach, and
  its mounts attach at its first start.

## The stopped-source rule

The source must be stopped (`created`, `stopped`, or `absent`).
The daemon refuses a clone of a running workspace with the
reason, the same refusal shape the home-volume endpoints give.
The refusal exists because the copy reads both disks at rest: a
copy taken while the guest writes would be torn, with the overlay
and the volume disagreeing about what the last write was.

## In-guest secrets duplicate

A full fork shares everything the disks carry, including secrets
the guest generated itself. The clone and the source carry the
same ssh host keys, the same `machine-id`, and the same
application secrets any first-boot process minted (session
signing keys, self-signed certificates). Anything that trusts the
source's identity — a `known_hosts` entry, a deploy key —
silently trusts the clone too.

There is no automatic regeneration. Rotate what matters before
trusting the clone as a separate machine: regenerate the guest's
ssh host keys, re-key anything the workspace registered with an
external service, or treat the clone as the same trust domain as
its source.

## Typical uses

- **Seed to fleet.** Prepare one workspace — toolchain, repos,
  login state — and clone it per task or per agent with
  `--copy-allowlist`: the seed's approved destinations propagate,
  and each clone still re-earns its interactive verdicts.
- **Experiment fork.** Clone before a risky session; the original
  keeps its state while the clone takes the risk. Snapshots
  (when they land) solve the same worry by rolling a workspace
  back in place; the two share only the disk-copy primitive.
