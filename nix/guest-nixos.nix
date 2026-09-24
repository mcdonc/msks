# The NixOS workspace guest image (#250).
#
# Second catalog image beside the Debian one: the kernel, initrd,
# and rootfs come from a NixOS system evaluated against the same
# pinned nixpkgs the devenv shell uses (the spike-205 pattern), so
# the guest's packages — console helper, cloud-init, sshd, rsync —
# are built by nixpkgs instead of fetched as Debian artifacts.
# Packaged into the same containerDisk archive contract
# (boot/vmlinuz, boot/initrd.img, disk/rootfs.ext4, disk/image.json
# schema 2) and registered under the `nixos` catalog name. The
# daemon takes no new machinery: every NixOS-vs-Debian difference
# the daemon acts on is declared in image.json (the capabilities),
# never keyed off the image's name.
#
# The rootfs carries the system closure under /nix/store with no
# virtiofs store share and no host store dependency: the image
# boots on any host that imports it. Since #274 it also carries a
# live nix: the closure is registered in a store database
# (`nix-store --load-db` over the closure info, the make-disk-image
# pattern), root's channel profile points at the pinned nixpkgs
# source the build itself used, and /etc/nixos/configuration.nix
# imports the very module this file evaluates — so `nixos-rebuild
# switch` inside a workspace re-evaluates the shipped configuration
# and activates the new system. Rebuilds persist across
# stop/start: the boot cmdline names the system profile
# (/nix/var/nix/profiles/system/init), an activation-maintained
# indirection the image's own profile seeds — no daemon change,
# no bootloader. The VMM still boots the image's frozen kernel and
# initrd, so a rebuilt system's new kernel takes effect only
# through a new image; userspace changes apply fully on reboot.
#
# A NixOS image needs no inode-metadata story (the Debian build
# records and restores every distro inode's mode/uid/gid):
# activation materializes /etc, /var, and the setuid wrappers on
# each workspace's own overlay at boot, so the image ships
# root:root everywhere and carries no setuid bits at all.
#
#   $out/vmlinux            - the NixOS kernel (bzImage; named
#                             "vmlinux" to match the
#                             MSKSD_TEST_VMLINUX contract;
#                             guest-manifest.json records the
#                             actual format).
#   $out/initrd             - NixOS stage-1, gzip, virtio-trimmed.
#   $out/rootfs.ext4        - the system closure as a fresh ext4
#                             image: the pristine base each
#                             workspace's overlay copies on write
#                             from (#14); guests mount it rw.
#   $out/guest-manifest.json - artifact names + the boot cmdline.
#   $out/workspace-nixos-<version>.tar - the catalog archive.
#
# Evaluate through the msks-build-guest script (`msks-build-guest
# nixos`); it pins nixpkgs to the devenv.lock revision.
{
  lib,
  pkgs,
}:

let
  # The port the guest's vsock console listens on; the daemon dials
  # it after the CONNECT handshake (#21). Same fixed port, same
  # manifest field, as the Debian image.
  vsockShellPort = 1023;

  # First-boot provisioning (#41): the seed-disk consumer is
  # NixOS's own cloud-init (nixpkgs builds it) — the declared
  # provisioner stays cloud-init, and the create-time contract
  # matches the Debian image exactly.
  imageProvisioner = "cloud-init";

  imageName = "nixos";

  # The console helper's sources, filtered for the store hop the
  # /etc/nixos bake (#274) makes: nothing but VCS noise and Rust's
  # target/ may ride along, or the image would embed whatever a
  # dev tree happened to have built.
  consoleHelperSrc = lib.cleanSourceWith {
    src = ../src/console-helper;
    filter =
      path: type: lib.cleanSourceFilter path type && baseNameOf path != "target";
  };

  # The /etc/nixos entry point (#274): imports the module the
  # image itself evaluated. The module, its package files, and the
  # console helper's sources ship beside it in the rootfs tree
  # below — a workspace rebuild evaluates exactly what the image
  # build did.
  configurationEntry = pkgs.writeText "configuration.nix" ''
    # The msks workspace's system configuration, as shipped by the
    # image build (#274). This file imports the same module the
    # image was built from; edit the module (or add settings here)
    # and run `sudo nixos-rebuild switch` to activate them.
    { ... }:
    {
      imports = [ ./nix/guest-nixos-configuration.nix ];
    }
  '';

  nixos =
    (import (pkgs.path + "/nixos") {
      system = "x86_64-linux";
      configuration = ./guest-nixos-configuration.nix;
    }).config;

  toplevel = nixos.system.build.toplevel;
  kernel = nixos.boot.kernelPackages.kernel;
  kernelFile = nixos.system.boot.loader.kernelFile;
  initrd = "${nixos.system.build.initialRamdisk}/initrd";
  kernelVersion = kernel.modDirVersion;

  # The pinned nixpkgs source as the guest-side input (#274): the
  # same revision the image was built from, staged the channel way
  # (make-disk-image's channelSources pattern) so <nixpkgs>
  # resolves for workspace rebuilds without a network. The
  # .version-suffix matches what the image build itself evaluated,
  # so `nixos-version` reports the same string after a rebuild.
  channelSources =
    pkgs.runCommand "nixos-${nixos.system.nixos.version}"
      {
        preferLocalBuild = true;
      }
      ''
        mkdir -p "$out"
        cp -prd ${pkgs.path} "$out"/nixos
        chmod -R u+w "$out"/nixos
        echo -n ${nixos.system.nixos.versionSuffix} > "$out"/nixos/.version-suffix
        ln -s nixos "$out"/nixpkgs
      '';

  # Catalog identity: the NixOS release plus the toplevel's short
  # hash — the release label alone (26.05pre…) stays constant across
  # months of pin bumps while the closure changes, and two different
  # builds must never share a name:version in the catalog.
  imageVersion =
    let
      toplevelHash = builtins.substring 0 8 (
        lib.head (lib.splitString "-" (baseNameOf (toString toplevel)))
      );
    in
    "${nixos.system.nixos.version}-${toplevelHash}";

  # Same boot shape as the Debian image plus the stage-2 init:
  # NixOS's own init must be named on the cmdline (there is no
  # bootloader to encode it), and it goes through the system
  # profile (#274) — the activation-maintained indirection a
  # `nixos-rebuild switch` re-points — so a rebuilt system, not
  # the image's frozen toplevel, boots after a workspace
  # stop/start. The store path itself resolves INSIDE the guest's
  # rootfs: the archive is self-contained on any host that
  # imports it.
  kernelCmdline = "console=ttyS0 root=/dev/vda rootfstype=ext4 rw init=/nix/var/nix/profiles/system/init";

  # The system closure plus the channel source, resolved by nix:
  # store-paths lists every path stage-2 activation, the units,
  # and the guest-side rebuild need.
  closureInfo = pkgs.closureInfo {
    rootPaths = [
      toplevel
      channelSources
    ];
  };

  # The NixOS root tree: the closure under /nix/store, the
  # registered store database, the nix profiles, and /etc/nixos;
  # activation materializes everything else (/etc, /var, the
  # wrappers) on the workspace's own overlay at boot.
  nixosRoot =
    pkgs.runCommand "msks-nixos-root"
      {
        inherit
          closureInfo
          toplevel
          channelSources
          configurationEntry
          consoleHelperSrc
          ;
        guestConfiguration = ./guest-nixos-configuration.nix;
        consoleHelperPkgFile = ./console-helper-pkg.nix;
        agentToolchainFile = ./agent-toolchain.nix;
        piExtensionFile = ./guest-pi-extension.ts;
        shrinkwrapPatchFile = ./pi-shrinkwrap-patch.py;
        shrinkwrapTableFile = ./pi-shrinkwrap-integrity.json;
        # binutils: readelf for the claude linkage guard below;
        # nix + sqlite: the store-db registration.
        nativeBuildInputs = [
          pkgs.gnutar
          pkgs.binutils
          pkgs.nix
          pkgs.sqlite
        ];
      }
      ''
        set -eu
        root=root-tree
        mkdir -p "$root"/nix/store \
          "$root"/{boot,dev,etc,home,proc,root,run,srv,sys,tmp,var}
        while read -r p; do
          cp -a "$p" "$root"/nix/store/
        done < "$closureInfo"/store-paths

        # The store database (#274): every shipped path registered
        # valid, so the guest's nix answers `nix-store -q
        # --references` and a rebuild reuses the shipped closure
        # instead of refetching it. The load runs against a
        # scratch state dir and the finished db lands in the tree;
        # registrationTime is zeroed and the file vacuumed, and a
        # second load of the same input must hash identically —
        # the build stays deterministic (a nix change that stamped
        # other build-time state into the db fails here, not as a
        # reproducibility mystery later).
        state1=$(pwd)/nix-state-1
        state2=$(pwd)/nix-state-2
        NIX_STATE_DIR="$state1" nix-store --load-db < "$closureInfo"/registration
        sqlite3 "$state1"/db/db.sqlite \
          'update ValidPaths set registrationTime = 0; vacuum;'
        NIX_STATE_DIR="$state2" nix-store --load-db < "$closureInfo"/registration
        sqlite3 "$state2"/db/db.sqlite \
          'update ValidPaths set registrationTime = 0; vacuum;'
        test "$(sha256sum < "$state1"/db/db.sqlite | cut -d' ' -f1)" \
          = "$(sha256sum < "$state2"/db/db.sqlite | cut -d' ' -f1)"
        mkdir -p "$root"/nix/var/nix/db
        cp "$state1"/db/db.sqlite "$root"/nix/var/nix/db/db.sqlite

        # The nix profiles (#274), in the exact shape nix-env
        # leaves: a generation symlink to the store, the profile
        # symlink to the generation. `system` is the init the
        # kernel cmdline names (nixos-rebuild switch re-points
        # it); root's `channels` is the baked pin the default
        # NIX_PATH resolves <nixpkgs> through. gcroots/profiles
        # keeps a guest-run nix-collect-garbage from collecting
        # the live system, and root's .nix-defexpr/channels gives
        # <nixpkgs> a fallback for shells sudo reset the session
        # NIX_PATH out of.
        mkdir -p "$root"/nix/var/nix/profiles/per-user/root \
          "$root"/nix/var/nix/gcroots "$root"/root/.nix-defexpr
        ln -s "$toplevel" "$root"/nix/var/nix/profiles/system-1-link
        ln -s system-1-link "$root"/nix/var/nix/profiles/system
        ln -s "$channelSources" \
          "$root"/nix/var/nix/profiles/per-user/root/channels-1-link
        ln -s channels-1-link \
          "$root"/nix/var/nix/profiles/per-user/root/channels
        ln -s /nix/var/nix/profiles "$root"/nix/var/nix/gcroots/profiles
        ln -s /nix/var/nix/profiles/per-user/root/channels \
          "$root"/root/.nix-defexpr/channels

        # /etc/nixos (#274): configuration.nix plus the import
        # chain it pulls — the module, its package files, the
        # shrinkwrap pair the toolchain build reads, and the
        # console helper's sources at the ../src path the helper
        # package expects to find beside ../nix. The repo layout
        # under /etc/nixos is what keeps the module's relative
        # imports resolving identically at image build and inside
        # a rebuilt workspace. Store inputs carry their hash
        # basenames, so each copy names its destination; the
        # u+w pass leaves the files editable for a workspace
        # user's own rebuild edits.
        mkdir -p "$root"/etc/nixos/nix "$root"/etc/nixos/src/console-helper
        cp "$configurationEntry" "$root"/etc/nixos/configuration.nix
        cp "$guestConfiguration" \
          "$root"/etc/nixos/nix/guest-nixos-configuration.nix
        cp "$consoleHelperPkgFile" \
          "$root"/etc/nixos/nix/console-helper-pkg.nix
        cp "$agentToolchainFile" \
          "$root"/etc/nixos/nix/agent-toolchain.nix
        cp "$piExtensionFile" \
          "$root"/etc/nixos/nix/guest-pi-extension.ts
        cp "$shrinkwrapPatchFile" \
          "$root"/etc/nixos/nix/pi-shrinkwrap-patch.py
        cp "$shrinkwrapTableFile" \
          "$root"/etc/nixos/nix/pi-shrinkwrap-integrity.json
        cp -a "$consoleHelperSrc"/. "$root"/etc/nixos/src/console-helper/
        chmod -R u+w "$root"/etc/nixos

        # A bootable tree: stage-2 init present, and the units the
        # contract names are wanted at boot — the console service,
        # cloud-init, sshd. A config that silently dropped one (the
        # enable flipped off, the wantedBy lost) fails the build
        # here, not a workspace's first boot.
        test -x "$toplevel"/init
        test -e "$toplevel"/etc/systemd/system/multi-user.target.wants/msks-console.service
        test -e "$toplevel"/etc/systemd/system/multi-user.target.wants/cloud-init.service
        test -e "$toplevel"/etc/systemd/system/multi-user.target.wants/sshd.service
        grep -q msks-console-helper "$closureInfo"/store-paths
        # Sanity: the baked agent toolchain (#266, #268) — an
        # upstream package or profile change must fail the build
        # here, not boot a workspace with a broken agent (the #36
        # bug class). Each sw/bin link resolves its whole symlink
        # chain — claude's runs down through the loader-patched
        # platform binary — and the tmpfiles conf plus the
        # extension's own store path prove the planting rules
        # ride the closure.
        for bin in node npm npx pi herdr claude fd rg; do
          test -x "$toplevel"/sw/bin/$bin \
            || { echo "sw/bin/$bin missing from the system profile" >&2; exit 1; }
        done
        # The rebuild posture (#274): nix answers, the rebuild
        # driver rides the profile, the store db registers the
        # shipped closure exactly, the profiles resolve, and
        # /etc/nixos carries the module chain a rebuild
        # re-evaluates.
        for bin in nix nixos-rebuild; do
          test -x "$toplevel"/sw/bin/$bin \
            || { echo "sw/bin/$bin missing from the system profile" >&2; exit 1; }
        done
        # The search path the rebuild tool rides (#274): its nix
        # calls run with a stripped environment, so the nix.conf
        # entry is the one every lookup falls back to — including
        # the nixos-system entrypoint it prefers.
        grep -q 'nixos-system=/nix/var/nix/profiles/per-user/root/channels/nixos/nixos' \
          "$toplevel"/etc/nix/nix.conf
        grep -q 'nixos-config=/etc/nixos/configuration.nix' \
          "$toplevel"/etc/nix/nix.conf
        db_rows=$(sqlite3 "$root"/nix/var/nix/db/db.sqlite \
          'select count(*) from ValidPaths')
        shipped=$(wc -l < "$closureInfo"/store-paths)
        test "$db_rows" -eq "$shipped" \
          || { echo "store db registers $db_rows paths, the tree ships $shipped" >&2; exit 1; }
        test -x "$root"/nix/var/nix/profiles/system/init
        test -f "$root"/nix/var/nix/profiles/per-user/root/channels/nixos/default.nix
        # The defexpr link's target is absolute (a guest-root path);
        # test the link itself, not -e through it — following it here
        # would answer for the BUILD HOST's /nix/var, not the tree's.
        test "$(readlink "$root"/root/.nix-defexpr/channels)" \
          = /nix/var/nix/profiles/per-user/root/channels
        grep -q 'guest-nixos-configuration.nix' "$root"/etc/nixos/configuration.nix
        test -f "$root"/etc/nixos/nix/guest-nixos-configuration.nix
        test -f "$root"/etc/nixos/nix/console-helper-pkg.nix
        test -f "$root"/etc/nixos/nix/agent-toolchain.nix
        test -f "$root"/etc/nixos/nix/guest-pi-extension.ts
        test -f "$root"/etc/nixos/nix/pi-shrinkwrap-patch.py
        test -f "$root"/etc/nixos/nix/pi-shrinkwrap-integrity.json
        test -f "$root"/etc/nixos/src/console-helper/Cargo.toml
        # The claude ELF's linkage (#268 review): the loader-patched
        # binary must resolve everything inside the closure — its
        # interpreter, every NEEDED soname, and every version symbol
        # it asks a library for. A pin bump onto a libc newer than
        # nixpkgs' glibc fails here, not at a workspace's first
        # launch (the same guard the Debian build gives rsync and
        # the toolchain). Every NEEDED soname this binary carries is
        # glibc-internal, so the loader's own glibc directory is the
        # tree's copy to ask.
        claudeElf="$root$(readlink -f "$toplevel"/sw/bin/claude)"
        interp=$(readelf -l "$claudeElf" \
          | awk '/interpreter/{gsub(/[\[\]]/,"",$NF); print $NF}')
        test -e "$root""$interp" \
          || { echo "claude loader $interp absent from the tree" >&2; exit 1; }
        glibcLib=$(dirname "$root""$interp")
        while read -r so ver; do
          [ -n "$so" ] || continue
          lib="$glibcLib"/"$so"
          if [ ! -e "$lib" ]; then
            lib=$(find "$root"/nix/store -maxdepth 5 -name "$so" | head -1)
          fi
          [ -n "$lib" ] && [ -e "$lib" ] \
            || { echo "claude needs $so, absent from the tree" >&2; exit 1; }
          readelf --version-info "$lib" | grep -q "Name: $ver" \
            || { echo "claude needs $ver from $so; the tree's copy is older" \
                 >&2; exit 1; }
        done <<<"$(readelf --version-info "$claudeElf" \
          | awk '/^Version needs section/ {needs=1; next} \
                 /^Version [a-z]+ section/ {needs=0} \
                 needs && /File: / {f=$5} \
                 needs && /Name: / {print f, $3}')"
        grep -Rq 'llm-models.ts' "$toplevel"/etc/tmpfiles.d/
        grep -q 'guest-pi-extension' "$closureInfo"/store-paths
        # The launchers must execute (#272): a profile link proves
        # presence, not a working interpreter — the bug this closes
        # shipped a cli.js whose shebang named a store node no guest
        # carries, and every launcher passed test -x. The store
        # binaries resolve against their own closure, so each runs
        # directly; pi runs under the profile's node with a scratch
        # HOME — cli.js is what sw/bin/pi execs through its env
        # shebang on a real guest. The version greps pin output
        # shape, not version: the pins live in nix/agent-toolchain.nix.
        nodeBin=$(readlink -f "$toplevel"/sw/bin/node)
        "$nodeBin" --version | grep -q '^v[0-9][0-9.]*$'
        mkdir pi-home
        HOME="$PWD"/pi-home "$nodeBin" \
          "$(readlink -f "$toplevel"/sw/bin/pi)" --version \
          | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'
        HOME="$PWD"/pi-home \
          "$(readlink -f "$toplevel"/sw/bin/claude)" --version \
          | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+'
        "$(readlink -f "$toplevel"/sw/bin/herdr)" --version \
          | grep -q '^herdr [0-9]'
        "$(readlink -f "$toplevel"/sw/bin/fd)" --version | head -1 \
          | grep -q '^fd [0-9]'
        "$(readlink -f "$toplevel"/sw/bin/rg)" --version | head -1 \
          | grep -q '^ripgrep [0-9]'
        mkdir -p "$out"
        du -s --apparent-size --block-size=4096 "$root" | cut -f1 > "$out"/tree-blocks
        # The opaque-tar hop (the same discipline as the Debian
        # build): the tree rides the store as one blob, never as a
        # tree — the store's auto-optimise hardlinks identical
        # files inside tree-shaped paths, and mke2fs -d packs
        # hardlink groups as one inode the guest's runtime writes
        # would collide in. --hard-dereference flattens any
        # hardlink a builder left inside a store path for the same
        # reason; sorted member order keeps the hop deterministic.
        tar --sort=name --hard-dereference --owner=0 --group=0 \
          --numeric-owner -C "$root" -cf "$out"/root.tar .
      '';

  # mke2fs -d packs the tree into an ext4 image without mounting
  # anything — the build stays unprivileged and host-independent
  # (see the Debian build's rootfs for the fakeroot mechanics).
  # One fakeroot session owns the tree: chown -R 0:0 is the whole
  # metadata story (no inode manifest — see the header).
  packScript = pkgs.writeText "msks-nixos-rootfs-pack.sh" ''
    set -eu
    tree="''${PACK_TREE:?}"
    img="''${PACK_IMG:?}"
    blocks="''${PACK_BLOCKS:?}"
    fake_epoch="''${PACK_FAKE_EPOCH:?}"
    chown -R 0:0 "$tree"
    E2FSPROGS_FAKE_TIME="$fake_epoch" mke2fs -q -t ext4 -b 4096 -I 256 \
      -L msks-rootfs \
      -E hash_seed=00000000-0000-0000-0000-000000000000 \
      -d "$tree" "$img" "$blocks"
    E2FSPROGS_FAKE_TIME="$fake_epoch" tune2fs -U clear "$img" >/dev/null
  '';

  rootfs =
    pkgs.runCommand "msks-guest-nixos-rootfs"
      {
        inherit
          nixosRoot
          packScript
          ;
        nativeBuildInputs = [
          pkgs.e2fsprogs
          pkgs.fakeroot
          pkgs.gnutar
        ];
        fakeEpoch = 1262304000;
      }
      ''
        set -eu
        mkdir -p "$out"
        mkdir work
        tar -C work -xf "$nixosRoot"/root.tar
        chmod -R u+w work
        # Content plus 1G of slack, like the Debian image: the
        # base keeps room for activation writes, and the
        # per-workspace overlay (#14) carries whatever the guest
        # writes beyond it — a rebuild's new store paths included.
        PACK_TREE=work \
          PACK_IMG="$out/rootfs.ext4" \
          PACK_BLOCKS=$(( $(cat "$nixosRoot"/tree-blocks) + 262144 )) \
          PACK_FAKE_EPOCH="$fakeEpoch" \
          fakeroot -- /bin/sh -e "$packScript"
      '';

  bootTree =
    pkgs.runCommand "msks-image-nixos-boot-tree"
      {
        inherit
          rootfs
          initrd
          imageName
          imageVersion
          kernelCmdline
          vsockShellPort
          kernelVersion
          ;
        vmlinuz = "${kernel}/${kernelFile}";
      }
      ''
        set -eu
        mkdir -p "$out"/boot "$out"/disk
        cp "$vmlinuz" "$out"/boot/vmlinuz
        cp "$initrd" "$out"/boot/initrd.img
        cp "${rootfs}/rootfs.ext4" "$out"/disk/rootfs.ext4
        # Self-describing (#40): the archive alone builds a boot
        # spec. The capabilities carry the whole NixOS-vs-Debian
        # difference the daemon acts on — the same declared
        # cloud-init provisioner, the same prelude-v1 console, the
        # same bzImage direct boot.
        cat > "$out"/disk/image.json <<EOF
        {
          "schema": 2,
          "name": "${imageName}",
          "version": "${imageVersion}",
          "cmdline": "${kernelCmdline}",
          "vsock_shell_port": ${toString vsockShellPort},
          "console_protocol": "prelude-v1",
          "console_users": ["root", "msks"],
          "kernel_version": "${kernelVersion}",
          "kernel_format": "bzImage",
          "capabilities": {"provisioner": "${imageProvisioner}"}
        }
        EOF
      '';

  imageArchive = (pkgs.callPackage ./image-archive.nix { }).mkImageArchive {
    inherit
      bootTree
      imageName
      imageVersion
      ;
  };

in
pkgs.runCommand "msks-guest-nixos"
  {
    inherit
      nixosRoot
      rootfs
      imageArchive
      imageName
      imageVersion
      initrd
      kernelVersion
      ;
    vmlinuz = "${kernel}/${kernelFile}";
    passthru = {
      inherit
        imageArchive
        toplevel
        ;
      inherit
        kernelCmdline
        vsockShellPort
        ;
    };
  }
  ''
    set -eu
    mkdir -p "$out"
    cp "$vmlinuz" "$out"/vmlinux
    cp "$initrd" "$out"/initrd
    cp "${rootfs}/rootfs.ext4" "$out"/rootfs.ext4
    # The canonical artifact: named by name-version, OCI layout inside.
    cp "${imageArchive}" "$out/workspace-''${imageName}-''${imageVersion}.tar"
    printf '%s' "workspace-''${imageName}-''${imageVersion}.tar" > "$out"/image-archive-name
    printf '%s' "${kernelVersion}" > "$out"/kernel-version
    cat > "$out"/guest-manifest.json <<EOF
    {
      "schema": 1,
      "kernel_version": "${kernelVersion}",
      "kernel_format": "bzImage",
      "cmdline": "${kernelCmdline}",
      "vmlinux": "vmlinux",
      "initrd": "initrd",
      "rootfs": "rootfs.ext4",
      "vsock_shell_port": ${toString vsockShellPort},
      "console_protocol": "prelude-v1",
      "console_users": ["root", "msks"]
    }
    EOF
  ''
