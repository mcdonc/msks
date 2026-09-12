# msks

Microvm workspace daemon — a Python analogue of klangkd that runs
workspaces as cloud-hypervisor microvms instead of podman containers.

## Development

Enter the environment (Python 3.14, uv venv, cloud-hypervisor +
ch-remote, qemu, the pytest toolchain, xenon):

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

### Host prerequisite for VM smoke tests

Booting a real microvm requires `/dev/kvm`; make sure your user is in
the `kvm` group (`users.users.<name>.extraGroups = [ "kvm" ];` on
NixOS, then re-login). Smoke tests that need a VM skip themselves when
the `MSKSD_TEST_*` variables are unset.

### The microvm seam (#1)

`msks.microvm.Microvm(app)` is the single VM lifecycle surface —
`launch` / `info` / `shutdown` / `kill` / `cleanup` — dispatched to the
driver named by the live setting `settings.vmm.driver` (`local` runs
one cloud-hypervisor process per workspace; `k8s` runs one runner pod
per workspace). Opt-in smoke tests:

- local: `MSKSD_TEST_VMLINUX` + `MSKSD_TEST_ROOTFS` (plus `/dev/kvm`)
- k8s: `MSKSD_TEST_KUBECONFIG` (and optional `MSKSD_TEST_NAMESPACE`);
  `nixos/k3s-dev.nix` provides a single-node k3s role for dev hosts.
