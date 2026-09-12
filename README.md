# msks

Microvm workspace daemon — a Python analogue of klangkd that runs
workspaces as cloud-hypervisor microvms instead of podman containers.

## Development

Install [nix](https://nix.dev/manual/nix/latest/install/) and
[devenv](https://devenv.sh/getting-started/) on any Linux host — a
NixOS host is not required. Enter the environment (Python 3.14, uv
venv, cloud-hypervisor + ch-remote, qemu, the pytest toolchain,
xenon):

```bash
devenv shell
```

All scripted/CI invocations disable dotenv loading:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- <command>
```

Run the test suite the way CI runs it (`-n auto` is never optional —
see AGENTS.md for the coverage story):

```bash
devenv --quiet -O dotenv.enable:bool false shell -- unit-tests
```

Scoped iteration picks only tests whose coverage touches changed lines:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- testmon
```

Complexity gate (also runs as a pre-commit hook):

```bash
devenv --quiet -O dotenv.enable:bool false shell -- devenv tasks run msks:xenon
```

### VM guest assets

Everything needed to boot a microvm is built by nix from the nixpkgs
revision devenv itself pins — kernel (bzImage with the PVH entry
point), an initrd carrying the virtio/ext4 modules the stock kernel
builds as modules, a read-only ext4 rootfs around a static busybox,
and the k8s vm-runner container archive. Every step of the build runs
inside the repo on any Linux host with nix:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- devenv tasks run msks:build-guest
```

The artifacts (plus a `guest-manifest.json` describing them and the
boot cmdline) land in `.guest/`. Boot one interactive VM from them —

```bash
devenv tasks run msks:demo-vm
```

— which attaches the guest's serial console to your terminal
(`poweroff -f` inside the guest or Ctrl-C stops it) and leaves
`ch-remote` reachable on the printed API socket path.

Boot tests self-provision: when `.guest/` holds built artifacts and
`/dev/kvm` is usable, the smoke tests find them without any exported
variables (`MSKSD_TEST_VMLINUX` / `MSKSD_TEST_INITRD` /
`MSKSD_TEST_ROOTFS` keep precedence when you do export them). When the
artifacts were never built, or `/dev/kvm` is missing or not accessible
to your user (add yourself to the `kvm` group,
`users.users.<name>.extraGroups = [ "kvm" ];` on NixOS, then
re-login), the smoke tests skip themselves.

### k8s (k3s) smoke path

The vm-runner container image comes from the same pinned nixpkgs as
the local backend's cloud-hypervisor:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- devenv tasks run msks:build-runner-image
sudo k3s ctr images import .guest/msks-vm-runner.docker.tar.gz
```

The k8s smoke tests reference the imported `msks-vm-runner:dev` image
automatically once the archive is built; they skip when
`MSKSD_TEST_KUBECONFIG` does not point at a cluster kubeconfig.
