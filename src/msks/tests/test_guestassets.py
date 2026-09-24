"""Tests for msks.guestassets — discovery of the nix-built guest
assets (#5)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from msks.guestassets import GuestAssets

from msks import guestassets


def guest_dir(root: Path) -> Path:
    """The default guest state dir below ``root`` (no env override)."""
    return root / ".devenv" / "state" / "guest"


def write_manifest(
    root: Path, *, guest: Path | None = None, **overrides: object
) -> None:
    """Write a valid manifest into ``root``'s guest state dir, then
    patch fields (``guest`` targets a different dir — the
    relocation tests)."""
    guest = guest if guest is not None else guest_dir(root)
    guest.mkdir(parents=True, exist_ok=True)
    for name in ("vmlinux", "initrd", "rootfs.ext4"):
        (guest / name).write_bytes(b"artifact")
    manifest = {
        "schema": 1,
        "kernel_version": "6.18.50",
        "kernel_format": "bzImage",
        "cmdline": "console=ttyS0 root=/dev/vda ro",
        "vmlinux": "vmlinux",
        "initrd": "initrd",
        "rootfs": "rootfs.ext4",
    }
    manifest.update(overrides)
    (guest / "guest-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def test_load_returns_assets(tmp_path: Path) -> None:
    write_manifest(tmp_path)
    assets = guestassets.load_guest_assets(tmp_path)
    assert assets == GuestAssets(
        vmlinux=guest_dir(tmp_path) / "vmlinux",
        initrd=guest_dir(tmp_path) / "initrd",
        rootfs=guest_dir(tmp_path) / "rootfs.ext4",
        cmdline="console=ttyS0 root=/dev/vda ro",
        kernel_version="6.18.50",
    )


def test_load_without_initrd(tmp_path: Path) -> None:
    write_manifest(tmp_path, initrd=None)
    assets = guestassets.load_guest_assets(tmp_path)
    assert assets is not None
    assert assets.initrd is None


def test_load_missing_manifest(tmp_path: Path) -> None:
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_invalid_json(tmp_path: Path) -> None:
    guest = guest_dir(tmp_path)
    guest.mkdir(parents=True)
    (guest / "guest-manifest.json").write_text("not json", encoding="utf-8")
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_unreadable_manifest(tmp_path: Path) -> None:
    # A directory in place of the file: reading it raises OSError
    # (EISDIR) for every user — root included, unlike a chmod 000 file.
    guest = guest_dir(tmp_path)
    guest.mkdir(parents=True)
    (guest / "guest-manifest.json").mkdir()
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_manifest_is_not_a_mapping(tmp_path: Path) -> None:
    guest = guest_dir(tmp_path)
    guest.mkdir(parents=True)
    (guest / "guest-manifest.json").write_text("[1, 2]", encoding="utf-8")
    assert guestassets.load_guest_assets(tmp_path) is None


@pytest.mark.parametrize("schema", [2, None, "1"])
def test_load_unknown_schema(tmp_path: Path, schema: object) -> None:
    write_manifest(tmp_path, schema=schema)
    assert guestassets.load_guest_assets(tmp_path) is None


@pytest.mark.parametrize("name", ["../vmlinux", "sub/vmlinux", "/etc/passwd"])
def test_load_rejects_artifact_names_outside_guest_dir(
    tmp_path: Path, name: str
) -> None:
    write_manifest(tmp_path, vmlinux=name)
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_missing_artifact_file(tmp_path: Path) -> None:
    write_manifest(tmp_path)
    (guest_dir(tmp_path) / "rootfs.ext4").unlink()
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_non_string_fields(tmp_path: Path) -> None:
    write_manifest(tmp_path, cmdline=7, kernel_version=False)
    assert guestassets.load_guest_assets(tmp_path) is None


def test_load_defaults_to_devenv_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(tmp_path)
    monkeypatch.setenv("DEVENV_ROOT", str(tmp_path))
    assets = guestassets.load_guest_assets()
    assert assets is not None
    assert assets.rootfs == guest_dir(tmp_path) / "rootfs.ext4"


def test_load_defaults_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(tmp_path)
    monkeypatch.delenv("DEVENV_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert guestassets.load_guest_assets() is not None


# --- the MSKS_GUEST_DIR relocation (#156) -----------------------------


def test_guest_dir_default(tmp_path: Path) -> None:
    assert guestassets.guest_dir(tmp_path) == guest_dir(tmp_path)


def test_guest_dir_env_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(guestassets.GUEST_DIR_ENV, "/elsewhere/guest")
    assert guestassets.guest_dir(tmp_path) == Path("/elsewhere/guest")


def test_guest_dir_env_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(guestassets.GUEST_DIR_ENV, "other-guest")
    assert guestassets.guest_dir(tmp_path) == tmp_path / "other-guest"


def test_load_honors_guest_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MSKS_GUEST_DIR relocates everything the loader reads."""
    moved = tmp_path / "relocated"
    monkeypatch.setenv(guestassets.GUEST_DIR_ENV, str(moved))
    write_manifest(tmp_path, guest=moved)
    assets = guestassets.load_guest_assets(tmp_path)
    assert assets is not None
    assert assets.rootfs == moved / "rootfs.ext4"


def test_kvm_available_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guestassets.os.path, "exists", lambda _: True)
    monkeypatch.setattr(guestassets.os, "access", lambda *_: True)
    assert guestassets.kvm_available()


def test_kvm_available_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guestassets.os.path, "exists", lambda _: False)
    assert not guestassets.kvm_available()


def test_kvm_available_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guestassets.os.path, "exists", lambda _: True)
    monkeypatch.setattr(guestassets.os, "access", lambda *_: False)
    assert not guestassets.kvm_available()


def _assets(**kwargs: object) -> GuestAssets:
    fields = {
        "vmlinux": Path("/a/vmlinux"),
        "initrd": Path("/a/initrd"),
        "rootfs": Path("/a/rootfs.ext4"),
        "cmdline": "console=ttyS0",
        "kernel_version": "6.18.50",
    }
    fields.update(kwargs)
    return GuestAssets(**fields)  # type: ignore[arg-type]


def _force_kvm(monkeypatch: pytest.MonkeyPatch, usable: bool) -> None:
    monkeypatch.setattr(guestassets.os.path, "exists", lambda _: usable)
    monkeypatch.setattr(guestassets.os, "access", lambda *_: usable)


def test_smoke_env_with_assets_and_kvm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_kvm(monkeypatch, True)
    env = guestassets.smoke_env_defaults(_assets())
    assert env == {
        "MSKSD_TEST_VMLINUX": "/a/vmlinux",
        "MSKSD_TEST_INITRD": "/a/initrd",
        "MSKSD_TEST_ROOTFS": "/a/rootfs.ext4",
        "MSKSD_TEST_CMDLINE": "console=ttyS0",
    }


def test_smoke_env_without_initrd(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_kvm(monkeypatch, True)
    env = guestassets.smoke_env_defaults(_assets(initrd=None))
    assert "MSKSD_TEST_INITRD" not in env


def test_smoke_env_without_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_kvm(monkeypatch, True)
    assert guestassets.smoke_env_defaults(None) == {}


def test_smoke_env_without_kvm(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_kvm(monkeypatch, False)
    assert guestassets.smoke_env_defaults(_assets()) == {}


# --- the image-baked agent toolchain (#266) ----------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def test_the_image_ships_the_pi_extension() -> None:
    """The model-discovery extension rides the guest image (#266):
    the file exists beside the image build, reads the MSKSWS_*
    pair the seed exports (never a vendor-shaped name), resolves
    its credential per request from the seeded token file, and
    carries the klangk behavior — provider registration, the
    embed/rerank filter, the quiet no-op when the environment
    names no proxy."""
    ext = (REPO_ROOT / "nix" / "guest-pi-extension.ts").read_text()
    assert "process.env.MSKSWS_BASE_URL" in ext
    assert "process.env.MSKSWS_API_KEY" in ext
    assert "OPENAI_API_KEY" not in ext
    assert 'apiKey: "!cat /etc/msks/llm.token"' in ext
    assert 'pi.registerProvider("msks"' in ext
    assert '"embed"' in ext and '"rerank"' in ext


def test_the_image_bakes_the_agent_toolchain() -> None:
    """The toolchain pins and their staging (#266): the shared
    build fetches the pinned pi package by digest, builds it
    offline against its shrinkwrap, and stages it with pinned
    herdr and Claude Code into the overlay's /usr/local beside the
    Debian-only Node tarball, with the extension planted for root
    and in the skeleton every seed-provisioned account copies."""
    build = (REPO_ROOT / "nix" / "guest-debian.nix").read_text()
    pins = (REPO_ROOT / "nix" / "agent-toolchain.nix").read_text()
    # String fragments, not joined URLs: nixfmt reflows the
    # concatenation layout, and the fragments are the stable atoms.
    assert '"https://nodejs.org/dist/v22.23.3/"' in build
    assert '"node-v22.23.3-linux-x64.tar.gz"' in build
    assert "pi-coding-agent-0.87.1.tgz" in pins
    # A real npmDepsHash, not the placeholder the two-step prefetch
    # starts from.
    assert "AAAAAAAAAAAAAAAAAAAAAAAA" not in pins
    assert '"v0.9.1/herdr-linux-x86_64"' in pins
    # herdr's Apache-2.0 notice travels with the binary, pinned to
    # the same tag.
    assert '"v0.9.1/LICENSE"' in pins
    assert "$out/usr/local/share/doc/herdr/LICENSE" in build
    assert '"claude-code-2.1.281.tgz"' in pins
    assert '"claude-code-linux-x64-2.1.281.tgz"' in pins
    assert "$out/usr/local/bin/claude" in build
    assert "$out/etc/skel/.pi/agent/extensions/llm-models.ts" in build
    assert "$out/root/.pi/agent/extensions/llm-models.ts" in build
    # pi links at its published bin (dist/bundle/cli.js), and the
    # package builds the tree bin-less — each image links it
    # itself (the Debian overlay's /usr/local symlink, the NixOS
    # profile's $out/bin in the shared module).
    assert "pi-coding-agent/dist/bundle/cli.js" in build
    assert "$out/usr/local/bin/pi" in build
    assert "$out/usr/local/bin/herdr" in build
    # The Debian image stages the shared derivations, not its own
    # pins: one file owns every pin, so a bump moves both images.
    assert "pkgs.callPackage ./agent-toolchain.nix" in build
    assert "agentPiPackage" not in build
    # #272: pi's fd and rg come from Debian's own debs — pinned by
    # pool URL and checksum like the kernel and rsync debs, staged
    # with the same linkage guard, in the layout apt leaves (fd's
    # real ELF under usr/lib/cargo/bin, fdfind a symlink).
    assert "fd-find_10.2.0-1+b5_amd64.deb" in build
    assert "ripgrep_14.1.1-1+b4_amd64.deb" in build
    assert '"$root"/usr/lib/cargo/bin/fd' in build
    assert 'ln -s ../lib/cargo/bin/fd "$root"/usr/bin/fdfind' in build
    assert "install -D -m 0755 rg-deb/usr/bin/rg" in build
    # Every launcher executes at build time (#272): the sandbox
    # cannot resolve the env shebang, so it is asserted byte-exact
    # and the dynamic tools run through the tree's own loader.
    assert "'#!/usr/bin/env node'" in build
    assert '"$root"/usr/local/bin/pi --version' in build
    assert 'run_tool "$root"/usr/bin/fdfind --version' in build
    assert 'run_tool "$root"/usr/bin/rg --version' in build
    # The NEEDED strip must be a bracket expression: the
    # two-character escape leaves the brackets, the word becomes a
    # glob, and stdenv's nullglob deletes it — a guard that
    # silently checks nothing.
    assert "gsub(/\\[\\]/" not in build
    assert "gsub(/[\\[\\]]/" in build


def test_the_nixos_image_ships_the_agent_toolchain() -> None:
    """The NixOS flavor's toolchain parity (#268): nixpkgs' own
    Node (the platform's packaging, floor-checked against pi's
    engines) plus the shared pins ride the system profile — every
    login PATH, no /usr/local staging — with Claude Code as the
    loader-patched variant and the extension planted by tmpfiles
    copy-once rules. The build's sanity battery pins the profile
    bins and the planting rules."""
    build = (REPO_ROOT / "nix" / "guest-nixos.nix").read_text()
    pins = (REPO_ROOT / "nix" / "agent-toolchain.nix").read_text()
    # The engines floor: a nixpkgs regression must fail the build,
    # not boot a workspace whose pi refuses to start.
    assert 'lib.versionAtLeast pkgs.nodejs_22.version "22.19.0"' in build
    assert "pkgs.nodejs_22" in build
    # The shared derivations, staged through the system profile:
    # pi, the loader-patched Claude Code (a stock NixOS ships no
    # /lib64 loader shim), and herdr's static binary.
    assert "pkgs.callPackage ./agent-toolchain.nix" in build
    assert "toolchain.piPackage" in build
    assert "toolchain.claudeLoaderPatched" in build
    assert "toolchain.herdrPackage" in build
    assert "patchelf --set-interpreter" in pins
    # The extension: tmpfiles copy-once rules for the skeleton and
    # root's home — real-file copies, not store symlinks, so a
    # user's later edits stay theirs. Fragments, not the joined
    # rule: nixfmt reflows the line.
    assert "C /etc/skel/.pi/agent/extensions/llm-models.ts" in build
    assert "C /root/.pi/agent/extensions/llm-models.ts" in build
    assert "0644 root root - ${piExtension}" in build
    # The sanity battery: the profile bins resolve (claude's chain
    # runs through the loader patch), and the planting rules plus
    # the extension itself ride the closure. Every launcher also
    # executes (#272): node, pi under the profile node, claude,
    # herdr, fd, and rg run at build time, so a staging change that
    # breaks one fails the build, not a workspace's first start.
    assert "for bin in node npm npx pi herdr claude fd rg; do" in build
    assert "grep -Rq 'llm-models.ts' \"$toplevel\"/etc/tmpfiles.d/" in build
    assert "grep -q 'guest-pi-extension'" in build
    assert "\"$nodeBin\" --version | grep -q '^v[0-9][0-9.]*$'" in build
    assert '"$(readlink -f "$toplevel"/sw/bin/herdr)" --version' in build
    assert '"$(readlink -f "$toplevel"/sw/bin/fd)" --version' in build
    assert '"$(readlink -f "$toplevel"/sw/bin/rg)" --version' in build
    # pi's fd/rg tool dependencies (#272): a first pi start would
    # otherwise download both from GitHub — behind the egress
    # interceptor. nixpkgs' own packages put them on the profile
    # PATH instead.
    assert "pkgs.fd" in build
    assert "pkgs.ripgrep" in build


def test_the_extension_bounds_its_single_fetch() -> None:
    """The startup fetch posture (#266 review): one attempt, bounded
    by an abort signal — no retry loop, no sleeps. The environment
    pair is seeded even when the daemon serves no proxy, so the
    fetch, not the shell, discovers the difference; a dropped tap
    must stall pi by at most the bound."""
    ext = (REPO_ROOT / "nix" / "guest-pi-extension.ts").read_text()
    assert "AbortSignal.timeout(1500)" in ext
    assert "setTimeout" not in ext
    assert "for (let attempt" not in ext


def load_integrity_table():
    """The integrity table the patcher consumes, as the tests use
    it: the JSON file in nix/."""
    spec = importlib.util.spec_from_file_location(
        "pi_shrinkwrap_patch", REPO_ROOT / "nix" / "pi-shrinkwrap-patch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    table = mod.load_missing(
        REPO_ROOT / "nix" / "pi-shrinkwrap-integrity.json"
    )
    return mod, table


def test_the_shrinkwrap_patch_pins_every_gap_by_name() -> None:
    """The injector (#266 review): every MISSING name takes its
    sha512, scoped and nested package keys resolve to their
    package names, entries that already carry integrity are left
    alone, and the guard the caller checks counts names — a
    duplicate of one sibling cannot mask another."""
    mod, table = load_integrity_table()

    lock = {
        "packages": {
            "": {"name": "pi-coding-agent"},
            "node_modules/@earendil-works/chord": {
                "resolved": "https://registry.example/chord.tgz",
            },
            "node_modules/chalk/node_modules/@earendil-works/pi-tui": {
                "resolved": "https://registry.example/pi-tui.tgz",
            },
            "node_modules/@earendil-works/pi-ai": {
                "resolved": "https://registry.example/pi-ai.tgz",
                "integrity": "sha512-alreadythere",
            },
            "node_modules/chalk": {"resolved": "https://x/y"},
        }
    }
    patched = mod.patch_lock(lock, table)
    # The nested key resolved to its package name; the
    # already-pinned entry stayed untouched.
    assert patched == {"@earendil-works/chord", "@earendil-works/pi-tui"}
    assert (
        lock["packages"]["node_modules/@earendil-works/chord"]["integrity"]
        == "sha512-" + table["@earendil-works/chord"]
    )
    nested = lock["packages"][
        "node_modules/chalk/node_modules/@earendil-works/pi-tui"
    ]
    assert nested["integrity"] == "sha512-" + table["@earendil-works/pi-tui"]
    already = lock["packages"]["node_modules/@earendil-works/pi-ai"]
    assert already["integrity"] == "sha512-alreadythere"
    # Names the table still expects fail loudly.
    assert patched != set(table)


def test_the_shrinkwrap_patch_strips_dev_dependencies() -> None:
    """The package.json half (#266 review): devDependencies go, the
    rest of the manifest stays byte-identical in content."""
    mod, _table = load_integrity_table()

    pkg = {
        "name": "pi-coding-agent",
        "devDependencies": {"vitest": "^4"},
        "dependencies": {"chalk": "5"},
    }
    assert mod.strip_dev_dependencies(pkg) is True
    assert pkg == {"name": "pi-coding-agent", "dependencies": {"chalk": "5"}}
    assert mod.strip_dev_dependencies(pkg) is False


def test_the_shrinkwrap_patch_values_are_valid_sha512() -> None:
    """Every injected integrity value is well-formed base64 that
    decodes to 64 bytes (#267 review follow-up): a truncated value
    passes the opaque string tests above and fails only inside
    nix's npm cache insert, on CI, far from the cause."""
    import base64
    import json as _json

    table = _json.loads(
        (REPO_ROOT / "nix" / "pi-shrinkwrap-integrity.json").read_text()
    )
    assert table
    for name, value in table.items():
        raw = base64.b64decode(value, validate=True)
        assert len(raw) == 64, name
