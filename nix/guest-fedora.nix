# Guest VM assets for direct kernel boot on cloud-hypervisor, the
# Fedora 44 shape (#651).
#
# Patterned after guest-debian.nix: everything a microvm needs comes
# out of this file as plain store paths, built from the pinned
# nixpkgs by pure derivations — the build runs on any Linux host
# with nix and touches nothing outside the repo.
#
# The root filesystem is Fedora 44, straight from Fedora's official
# Generic cloud image: real Fedora with PID 1, dnf, and Fedora's own
# modules. Two upstream shapes differ from Debian's and the build
# adapts each:
#
#   * The cloud image's root filesystem is btrfs (Fedora's cloud
#     default). The build extracts the tree with `btrfs restore`
#     — unprivileged, no mount — and packs a FRESH ext4 from the
#     tree, exactly the deb path's debugfs-rdump + mke2fs dance
#     with btrfs legs. The image's root is a btrfs subvolume set
#     (root, boot, home, var); the restore flattens the set into
#     the plain directory tree a direct-boot guest wants.
#   * Fedora ships no separate kernel artifact: the kernel, its
#     config, and the module tree ride inside the image
#     (/usr/lib/modules/<kver>/vmlinuz). One dated pin covers
#     them; the compose is pinned by its dl.fedoraproject.org URL
#     and sha256 ("latest" is a moving pointer; released composes
#     stay published).
#
# Fedora's kernel config favors direct boot more than Debian's
# generic flavor did: ext4 with jbd2 and crc32c, virtio, virtio_pci,
# virtio_blk, the 8250 console, and the ACPI button/evdev pair are
# all BUILT IN, so the minimal initramfs carries zero modules —
# busybox mounts the ext4 root and switch_roots into systemd.
#
# Metadata: the extraction runs inside ONE fakeroot session, and
# `btrfs restore -m` restores owner, group, and mode — the
# special bits included (sudo comes back 4111; the build asserts
# it) — into the session's faked view. A faked view cannot
# outlive its session, so the session writes its final view out
# as a manifest and the pack stage re-applies it under its own
# fakeroot (see treeMeta) — the deb path's manifest walk with the
# walk replaced by the session's own find.
#
#   $out/vmlinux            - Fedora's kernel (bzImage, EFI-stub
#                             entry point). Named "vmlinux" to
#                             match the TEST_VMLINUX contract;
#                             guest-manifest.json records the actual
#                             format.
#   $out/initrd             - msks-built minimal initramfs: busybox
#                             and an init that mounts /dev/vda rw
#                             and switch_roots into systemd — no
#                             modules, everything it touches is
#                             built into Fedora's kernel.
#   $out/rootfs.ext4        - the extracted Fedora tree as a fresh
#                             ext4 image: the pristine base each
#                             workspace's overlay copies on write
#                             from (#14); guests mount it rw.
#   $out/guest-manifest.json - artifact names + the boot cmdline.
#
# Evaluate through the msks-build-guest script (it pins nixpkgs
# to the devenv.lock revision); `nix-build nix/guest.nix -A
# guest-fedora` with plain NIX_PATH also works when the pinned
# channel is acceptable.
{
  lib,
  pkgs,
  # The guest's architecture, uname machine names; the default
  # follows the evaluating host so a bare build always produces
  # an image bootable there (the minimal initramfs's busybox is a
  # native build — a cross-eval would ship a foreign-arch shell
  # into the initrd). The explicit attrs in guest.nix pin either
  # arch for the channel legs.
  arch ? builtins.head (lib.splitString "-" pkgs.stdenv.hostPlatform.system),
}:

let
  # The per-arch surface (#637): Fedora's own artifact names, the
  # dated pins and their hashes, and the boot facts that differ —
  # the console device (cloud-hypervisor's aarch64 machine presents
  # a PL011 UART, which Linux names ttyAMA0; x86_64's 8250 is
  # ttyS0) and the kernel format (arm64's vmlinuz is a flat Image,
  # not a bzImage). The x86_64 pins are the ones this build has
  # actually carried; the aarch64 facts mirror Debian's (the
  # compose is pinned, and the arch guard in the initramfs keeps a
  # cross-eval from shipping a foreign-arch busybox).
  archFacts = {
    x86_64 = {
      imageSuffix = "x86_64";
      imageHash = "sha256:28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f";
      fdRpm = "fd-find-10.4.2-1.fc44.x86_64.rpm";
      fdHash = "sha256:8b40b738246cdd2e5a50dda968be66bf9c5fed168f83b999823d5c7fee48054a";
      rgRpm = "ripgrep-14.1.1-4.fc44.x86_64.rpm";
      rgHash = "sha256:fc31651b27814d7860cd14124e0e1db3c4b6c2d43b46c2463ad440fb8626e099";
      jemallocRpm = "jemalloc-5.3.0-14.fc44.x86_64.rpm";
      jemallocHash = "sha256:32c49c3f26c24f0e2fc0968f483d31d52ba4543e0da25a4ef0773cff662090fe";
      console = "ttyS0";
      kernelFormat = "bzImage";
      ociArch = "amd64";
      nodeTar = "x64";
      nodeHash = "sha256-EISqNhlrukw6Xmmh7jiKbk/3KdrQlEX7zUNLKP48JK8=";
    };
    aarch64 = {
      imageSuffix = "aarch64";
      imageHash = "sha256:55c60a3b80d3616a08705afd0459e75fe9f03c54aba7a46e4002a41a72fa0d5b";
      fdRpm = "fd-find-10.4.2-1.fc44.aarch64.rpm";
      fdHash = "sha256:6ce6152fd8e3ea2280b6369e54a374ce94d7e801052083a245541f14b563a40f";
      rgRpm = "ripgrep-14.1.1-4.fc44.aarch64.rpm";
      rgHash = "sha256:92b40484c2e3c92884b64b9fbd0e6893e0762fcb7ab8c1fe06199c21ccc982a3";
      jemallocRpm = "jemalloc-5.3.0-14.fc44.aarch64.rpm";
      jemallocHash = "sha256:e8f25a060e1c7aa638414f70105464bf701a95850b328289c9514884610c94a3";
      console = "ttyAMA0";
      kernelFormat = "Image";
      ociArch = "arm64";
      nodeTar = "arm64";
      nodeHash = "sha256-XO0tSNHXGYc5t/hoBN4Bca77aCO2hLEjQdMyGvw8sLI=";
    };
  };
  facts =
    archFacts.${arch} or (throw "guest arch '${arch}' carries no Fedora pin set");

  # The per-arch entries of the runtime-module closure that
  # survived the kernel's built-ins: Fedora's x86_64 kernel builds
  # crc32c and the ACPI button/evdev pair in (Debian carried them
  # as modules; the shared list below carries button/evdev/crc16
  # anyway, where a builtin resolves to nothing and a module
  # ships), so the arch extras are the nested-KVM pair alone;
  # arm64 adds its kvm module and the gpio-keys power-button
  # driver — cloud-hypervisor's aarch64 power button is a
  # device-tree gpio-keys device, and without the module
  # logind's power-key handling never sees the button (the x86
  # ACPI button is one of the builtins above).
  archModules =
    {
      x86_64 = "kvm-intel kvm-amd";
      aarch64 = "kvm gpio_keys";
    }
    .${arch};

  # The nested-KVM unit's modprobe line (#637): the x86 pair with
  # its fallbacks; the arm64 module alone.
  kvmModprobe =
    {
      x86_64 = "modprobe kvm-intel || modprobe kvm-amd || true";
      aarch64 = "modprobe kvm || true";
    }
    .${arch};

  # The dynamic loader's path inside the tree: Fedora's x86_64
  # keeps it under /lib64, arm64 under /lib.
  elfLoader =
    {
      x86_64 = "/lib64/ld-linux-x86-64.so.2";
      aarch64 = "/lib/ld-linux-aarch64.so.1";
    }
    .${arch};

  # Fedora's shared-library directories (no multiarch triplet):
  # /usr/lib64 carries the 64-bit libraries on both arches, and
  # aarch64's dynamic loader additionally lives in /usr/lib
  # (glibc's aarch64 install root — Fedora's aarch64 fd even NEEDS
  # the loader soname itself, which is how the split surfaced).
  # The staged-rpm soname guards resolve against the list; the
  # 64-bit staging path (jemalloc) stays usr/lib64 everywhere.
  libDirs =
    {
      x86_64 = "usr/lib64";
      aarch64 = "usr/lib64 usr/lib";
    }
    .${arch};

  # The official Fedora 44 Generic cloud image (#651): Fedora with
  # systemd, cloud-init, NetworkManager, rsync, sudo, and sshd —
  # the pieces the msks overlay configures rather than replaces.
  # Pinned by compose URL and sha256; released composes stay
  # published.
  fedoraImage = pkgs.fetchurl {
    urls = [
      "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/${facts.imageSuffix}/images/Fedora-Cloud-Base-Generic-44-1.7.${facts.imageSuffix}.qcow2"
    ];
    hash = facts.imageHash;
  };

  # First-boot provisioning (#41): the image's declared seed-disk
  # consumer — cloud-init, shipped by the Generic cloud image.
  # Both payload forms work: cloud-config YAML and #! scripts.
  imageProvisioner = "cloud-init";

  # The workspace image identity (#40): the catalog reference is
  # <name>:<version>.
  imageName = "fedora";
  imageVersion = "44";

  # Root boots read-write (#14): the per-workspace qcow2 overlay
  # absorbs writes over this pristine base. selinux=0: the build
  # reconstructs the filesystem from the image WITHOUT SELinux
  # xattrs (btrfs restore drops them, and mke2fs cannot write them
  # unprivileged), so every file would boot unlabeled and
  # enforcing policy freezes PID 1 before the first getty — the
  # same posture as the Debian and NixOS images, which carry no
  # MAC: the workspace's isolation boundary is the VM itself.
  kernelCmdline = "console=${facts.console} root=/dev/vda rootfstype=ext4 rw selinux=0";

  # Fedora's own fd-find and ripgrep (#272): pi resolves its fd and
  # rg tools from PATH (fd, fdfind, or rg) and downloads them from
  # GitHub releases when it finds none — a download that would sit
  # on the critical path of a fresh workspace's first agent start,
  # behind the egress interceptor, against the GitHub API quota
  # every workspace behind one address shares. The image ships
  # neither tool; the distro's own packages, pinned from the F44
  # Everything repo by URL and sha256 (Fedora's fd binary is named
  # fd, the name pi probes first), put both on PATH and the first
  # start needs no downloads.
  fdFindRpm = pkgs.fetchurl {
    url = "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/${facts.imageSuffix}/os/Packages/f/${facts.fdRpm}";
    hash = facts.fdHash;
  };

  ripgrepRpm = pkgs.fetchurl {
    url = "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/${facts.imageSuffix}/os/Packages/r/${facts.rgRpm}";
    hash = facts.rgHash;
  };

  # fd-find's allocator dependency: Fedora's fd links libjemalloc,
  # which the cloud image does not carry — the soname guard below
  # caught exactly that. Staged from the distro's own package,
  # pinned like fd and rg themselves; the library lands where the
  # tree's other shared libraries live.
  jemallocRpm = pkgs.fetchurl {
    url = "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/${facts.imageSuffix}/os/Packages/j/${facts.jemallocRpm}";
    hash = facts.jemallocHash;
  };

  # The agent toolchain's Node pin (#266): the official standalone
  # release, digest-pinned, that the overlay stages under
  # /usr/local (same pin as the Debian image — the platform's own
  # packaging does not exist for Node on Fedora either).
  agentNodeTarball = pkgs.fetchurl {
    url =
      "https://nodejs.org/dist/v22.23.3/"
      + "node-v22.23.3-linux-${facts.nodeTar}.tar.gz";
    hash = facts.nodeHash;
  };

  # The rest of the agent toolchain (#266, #268): the shared pins
  # and offline builds — pi, herdr (binary plus license), Claude
  # Code — in nix/agent-toolchain.nix, staged the same way the
  # Debian image stages them.
  toolchain = pkgs.callPackage ./agent-toolchain.nix { inherit arch; };

  # The minimal initramfs (#37's shape, the Fedora cut): busybox
  # and an init that mounts /dev/vda rw and switch_roots into
  # systemd — no modules, because Fedora's kernel builds in
  # everything the root mount needs (ext4 with jbd2, crc32c, and
  # crc16; virtio, virtio_pci, virtio_blk). The stock dracut
  # archive this replaces carries a MODULES=most set measured in
  # hundreds of megabytes.
  minimalInitrd =
    pkgs.runCommand "msks-minimal-initrd-fedora"
      {
        # Static: the initramfs has no dynamic loader. nixpkgs'
        # default busybox links against a store glibc.
        busybox = pkgs.pkgsStatic.busybox;
        nativeBuildInputs = [
          pkgs.cpio
          pkgs.file
          pkgs.gzip
        ];
      }
      ''
        set -eu
        mkdir -p "$out"/tree/bin "$out"/tree/proc \
          "$out"/tree/dev "$out"/tree/newroot
        # A cross-eval would ship a foreign-arch busybox into the
        # initrd (pkgsStatic builds for the evaluating host): the
        # guard turns the silent mismatch into a named refusal at
        # build time (#637).
        case "${arch}" in
          x86_64) file_machine="x86-64" ;;
          *) file_machine="${arch}" ;;
        esac
        file -b "$busybox"/bin/busybox | grep -qw "$file_machine" || {
          echo "busybox is not ${arch}: cross-evaluating the ${arch} guest needs an evaluator of that arch" >&2
          exit 1
        }
        cp "$busybox"/bin/busybox "$out"/tree/bin/busybox
        cat > "$out"/tree/init <<'INIT'
        #!/bin/busybox sh
        # Mount root and hand off to systemd (#37): keep this as small
        # as it looks — every millisecond here delays the console. On
        # any failure, a shell beats a silent hang in the ~1MB
        # initramfs (the serial console is reachable). rw matches the
        # image cmdline (#14): the overlay carries the writes. No
        # modules to load: Fedora's kernel builds ext4 and virtio_blk
        # in (#651).
        /bin/busybox mount -t proc proc /proc \
          && /bin/busybox mount -t devtmpfs devtmpfs /dev \
          && /bin/busybox mount -t ext4 -o rw /dev/vda /newroot \
          || exec /bin/busybox sh
        /bin/busybox mount --move /dev /newroot/dev
        exec /bin/busybox switch_root /newroot /sbin/init
        INIT
        chmod +x "$out"/tree/init
        (cd "$out"/tree && find . | cpio -o -H newc --quiet | gzip -9) \
          > "$out"/initrd
        rm -rf "$out"/tree
      '';

  # The msks additions, staged as an overlay tree: the Fedora
  # mirror of the Debian overlay — the serial-console autologin on
  # the console device (#481), the net module load, the
  # nested-KVM module and inner-egress stack a workspace running
  # msksd itself needs (#82), a stable hostname, the sshd posture
  # the TCP service plane rides (#110), the sudoers grant behind
  # the workspace user's sudo (#169), and the agent toolchain
  # (#266). The NIC-management half DIFFERS: the Debian overlay
  # enables systemd-networkd; Fedora's image runs NetworkManager,
  # whose default policy already takes a DHCP address on any
  # ethernet it finds — an egress workspace's NIC configures
  # itself with no msks-side unit, and cloud-init's network
  # rendering stays off the same way so NM's default stands.
  guestOverlay =
    pkgs.runCommand "msks-guest-overlay-fedora"
      { nativeBuildInputs = [ pkgs.gnutar ]; }
      ''
        set -eu
        mkdir -p \
          $out/home \
          $out/usr/bin \
          $out/etc/cloud/cloud.cfg.d \
          $out/etc/sudoers.d \
          $out/etc/ssh/sshd_config.d \
          $out/etc/systemd/system/serial-getty@${facts.console}.service.d \
          $out/etc/systemd/system/ssh.service.d \
          $out/etc/systemd/system/multi-user.target.wants \
          $out/etc/modules-load.d

        printf 'msks-guest\n' > $out/etc/hostname

        # The agent toolchain (#266): pinned Node from the official
        # tarball and pi in npm's global layout, both under /usr/local —
        # system-wide, root-owned, on every account's default PATH, so
        # a workspace boots with a working agent toolchain and no
        # per-user installer steps. The pins move with an image
        # rebuild; a workspace that already booted keeps what it has.
        mkdir -p $out/usr/local/lib/node_modules
        tar -xzf ${agentNodeTarball} \
          -C $out/usr/local --strip-components=1
        cp -r "${toolchain.piPackage}/lib/node_modules/pi-coding-agent" \
          $out/usr/local/lib/node_modules/
        ln -s ../lib/node_modules/pi-coding-agent/dist/bundle/cli.js \
          $out/usr/local/bin/pi

        # herdr (#266): the pinned static binary, executable as-is,
        # with its license beside it (Apache-2.0's notice travels
        # with redistribution).
        install -D -m 0755 ${toolchain.herdrPackage}/bin/herdr \
          $out/usr/local/bin/herdr
        install -D -m 0644 \
          ${toolchain.herdrPackage}/share/doc/herdr/LICENSE \
          $out/usr/local/share/doc/herdr/LICENSE

        # Claude Code (#266): the staged npm tree, merged into
        # /usr/local.
        cp -r "${toolchain.claudePackage}/lib/." $out/usr/local/lib/
        ln -s \
          ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe \
          $out/usr/local/bin/claude

        # The model-discovery extension (#266): into /etc/skel — every
        # account the identity seed provisions copies the skeleton —
        # and into root's own home, since root seeds no skeleton. pi
        # discovers extensions from per-user directories only, so this
        # is the image-side home of the file each login user ends up
        # with; a user's later edits are theirs (nothing ever
        # re-overwrites it).
        install -D -m 0644 ${./guest-pi-extension.ts} \
          $out/etc/skel/.pi/agent/extensions/llm-models.ts
        install -D -m 0644 ${./guest-pi-extension.ts} \
          $out/root/.pi/agent/extensions/llm-models.ts
        # The default user settings (#616): pi hides the model's
        # thinking blocks in the transcript. Copy-once like the
        # extension — a user's later edits are theirs.
        install -D -m 0644 ${./guest-pi-settings.json} \
          $out/etc/skel/.pi/agent/settings.json
        install -D -m 0644 ${./guest-pi-settings.json} \
          $out/root/.pi/agent/settings.json

        # The sshd posture (#110): every login is a key login. The
        # image ships sshd enabled; the dropin states the full
        # contract where sshd reads it first (Include is the config's
        # opening line, and first match wins). Root may log in with a
        # key — the console-planted identity of #110's tests, the
        # msksd-minted key of #111 — and never with a password.
        #
        # Authentication policy only, never algorithm policy: no cipher,
        # MAC, key-exchange, or host-key lists here, so a FIPS-restricted
        # OpenSSH (a distro crypto provider) narrows itself without
        # config surgery (#115).
        printf '%s\n' \
          '# msks (#110): key-only login; the forward is the road in.' \
          'PasswordAuthentication no' \
          'KbdInteractiveAuthentication no' \
          'PermitRootLogin prohibit-password' \
          > $out/etc/ssh/sshd_config.d/00-msks.conf

        # The admin group's sudo (#169, #248): passwordless root for
        # every member of wheel — the group Fedora's image ships at
        # gid 10 (the build asserts it), carrying the shipped
        # workspace user and any login user the identity seed (#248)
        # joins at first boot. The grant rides the GROUP because the
        # group is image-shipped state the policy names: the seed
        # creates accounts and joins them, never sudo configuration.
        # The password is locked by design (the console helper and
        # ssh keys are the road in), so NOPASSWD is the only form
        # that can ever run. The pack stage sets the 0440 sudoers
        # mode.
        printf '%s\n' \
          '# msks (#169): the wheel group administers this VM.' \
          '%wheel ALL=(ALL) NOPASSWD:ALL' \
          > $out/etc/sudoers.d/msks

        # Host keys come from the image's own first-boot generation
        # (sshd-keygen@.service targets, wanted by sshd.service on
        # ConditionFirstBoot): they land on the root overlay, so
        # they survive stop/start with the overlay (#14) and each
        # workspace owns its own keys — nothing here to stage.

        # sshd waits for the interface to have its address (#110): the
        # forward path dials the guest's tap address, and the ordering
        # puts listening behind DHCP instead of ahead of it. A scoped
        # oneshot, NOT systemd's wait-online: this guest boots with no
        # NIC at all in the no-egress posture, and a link-less
        # NetworkManager never reaches "online" — wait-online would
        # stall those boots at network-online.target. The NIC check
        # lives in ExecCondition: with no NIC beyond lo the unit is
        # skipped and sshd listens immediately; the 15s ceiling never
        # blocks the port beyond a slow DHCP.
        cat > $out/etc/systemd/system/msks-wait-address.service <<'WAITUNIT'
        [Unit]
        Description=msks: sshd listens once the NIC has its address
        After=NetworkManager.service
        Before=ssh.service ssh.socket

        [Service]
        Type=oneshot
        RemainAfterExit=yes
        ExecCondition=/bin/sh -c 'ip -o link show 2>/dev/null | grep -qv "lo:"'
        ExecStart=/bin/sh -c 'for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do ip -4 -o addr show scope global 2>/dev/null | grep -q . && exit 0; sleep 1; done'
        WAITUNIT
        printf '%s\n' \
          '[Unit]' \
          'Wants=msks-wait-address.service' \
          'After=msks-wait-address.service' \
          > $out/etc/systemd/system/ssh.service.d/10-after-address.conf

        # cloud-init (#41): the workspace's cidata seed is NoCloud's own
        # format. Two dropins pin the behavior the msks contract needs:
        # the datasource list stops cloud-init probing EC2/OpenStack/
        # network sources (the seed disk answers immediately), and
        # network rendering stays off — NetworkManager's default policy
        # owns whatever NIC appears, taking its address from the
        # daemon's own DHCP (#52), so cloud-init must not fight it with
        # a renderer.
        printf '%s\n' \
          '# msks: the cidata seed disk is the only datasource.' \
          'datasource_list: [ NoCloud, None ]' \
          > $out/etc/cloud/cloud.cfg.d/99-msks-datasources.cfg
        printf '%s\n' \
          '# msks: NetworkManager owns the NIC; no renderer runs.' \
          'network: {config: disabled}' \
          > $out/etc/cloud/cloud.cfg.d/99-msks-network.cfg
        # /etc/hosts carries the workspace's name (#376):
        # manage_etc_hosts in `localhost` mode makes the base
        # image's update_etc_hosts module add the conventional
        # 127.0.1.1 record naming the host, in place, whenever the
        # entry does not already name the host — the file's other
        # entries stay in place.
        printf '%s\n' \
          '# msks (#376): update_etc_hosts adds the conventional' \
          '# 127.0.1.1 record naming the host, in place.' \
          'manage_etc_hosts: localhost' \
          > $out/etc/cloud/cloud.cfg.d/99-msks-etc-hosts.cfg
        # cloud-init creates no accounts (#171): the image ships the
        # msks workspace user (#63), and the identity seed makes its
        # home. The Generic image's cloud.cfg carries a `users:` list
        # of its own; the later-sorted dropin's empty list wins and no
        # default account ever comes into being.
        printf '%s\n' \
          '# msks (#171): the image ships the msks user (#63);' \
          '# cloud-init creates no accounts and the identity seed' \
          '# makes the home.' \
          'users: []' \
          > $out/etc/cloud/cloud.cfg.d/99-msks-users.cfg

        # The image's fstab mounts the btrfs subvolumes by UUID; a
        # direct kernel boot presents a bare ext4, so those device
        # jobs can never start and systemd stalls at boot. The
        # kernel cmdline names the root device; systemd mounts the
        # pseudo-filesystems itself.
        #
        # /home is the workspace's second persistent disk (#14): the
        # host-side ext4 volume, labeled msks-home at mkfs time and
        # attached as a second virtio-blk disk. Mounting by label (not
        # /dev/vdb) keeps /home on the right device even if the disk
        # order ever shifts. nofail plus a device timeout keeps a boot
        # without the volume (a demo VM, a pre-#14 image) moving
        # instead of stalling the default 90s.
        printf '%s\n' \
          '# msks: root comes from the kernel cmdline; no swap.' \
          '# /home is the second persistent disk (#14), labeled msks-home.' \
          'LABEL=msks-home /home ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2' \
          > $out/etc/fstab

        # Egress networking (#52): a workspace created with egress boots
        # with a virtio-net NIC; everything else presents none. The
        # module loads at boot either way (NetworkManager would
        # trigger udev's autoload on device discovery too), so nothing
        # waits on a cold probe.
        printf '%s\n' \
          '# The virtio-net driver for egress NICs (#52).' \
          'virtio_net' \
          > $out/etc/modules-load.d/msks-net.conf

        # The nested-msks stack (#82): a workspace running msksd needs
        # the same kernel modules a deployment host loads — tun for the
        # per-inner-workspace taps, the nftables/NAT set the daemon's
        # rulesets name — so a workspace can host workspaces itself.
        # The interactive-consent pieces (reject/NFQUEUE) stay out of
        # this deterministic load set: the daemon's ruleset autoloads
        # them from the shipped module tree when it installs (#552).
        # KVM does NOT ride this file: which flavor loads depends on the
        # host CPU, and a modules-load.d entry that fails leaves
        # systemd-modules-load.service failed (a degraded boot) — the
        # oneshot service below picks the flavor and swallows a host
        # without nested virt.
        printf '%s\n' \
          '# msks: the inner-egress stack (#82); KVM loads via its unit.' \
          'tun' \
          'nf_tables' \
          'nft_chain_nat' \
          'nft_masq' \
          'nft_ct' \
          'nf_nat' \
          'nf_conntrack' \
          > $out/etc/modules-load.d/msks-egress.conf

        # The nested-KVM module for inner workspace VMs (#82): which
        # flavor loads depends on the host CPU, so a shell picks, and a
        # workspace booted where vmx does not reach (a host without
        # nested virt) still boots — the unit stays active (exited)
        # and /dev/kvm simply never appears.
        printf '%s\n' \
          '[Unit]' \
          'Description=msks nested-KVM module (inner workspace VMs, #82)' \
          'After=systemd-modules-load.service' \
          ''' \
          '[Service]' \
          'Type=oneshot' \
          'ExecStart=/bin/sh -c "${kvmModprobe}"' \
          'RemainAfterExit=yes' \
          ''' \
          '[Install]' \
          'WantedBy=multi-user.target' \
          > $out/etc/systemd/system/msks-kvm.service
        ln -s ../msks-kvm.service \
          $out/etc/systemd/system/multi-user.target.wants/msks-kvm.service

        # The console (#481): the console device carries both the
        # kernel console and an autologin root getty — the daemon
        # bridges the serial device's socket to the client. TERM is
        # pinned because a bare drop-in inherits none; readline turns
        # off without it (#61). systemd-getty-generator instantiates
        # serial-getty@ttyS0 from the console= kernel argument, so
        # the dropin applies the moment the generator creates the
        # unit. The After= mirrors the Debian image's console
        # drop-in: the prompt is the readiness signal, so it waits
        # for cloud-init's init stage (Fedora names it
        # cloud-init-main.service; the seed's hostname — an
        # interactive bash freezes $HOSTNAME at startup) and for
        # home.mount to settle (a write before the volume mounts
        # lands on the overlay and disappears behind the mount).
        printf '%s\n' \
          '[Unit]' \
          'After=cloud-init-main.service home.mount' \
          '[Service]' \
          'Environment=TERM=xterm' \
          'ExecStart=' \
          'ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM' \
          > $out/etc/systemd/system/serial-getty@${facts.console}.service.d/autologin.conf

      '';

  # Parse sfdisk --json: print the byte offset of the Linux root
  # partition. Fedora's cloud images type the root partition with
  # the same per-arch Linux-root GPT GUIDs Debian's do, so the
  # parser is the Debian one verbatim. A separate file (not
  # inline) because nix ''-string de-indentation would mangle a
  # python block.
  partitionOffset = pkgs.writeText "msks-root-partition-offset.py" ''
    import json
    import sys

    table = json.load(sys.stdin)["partitiontable"]
    # The cloud images type the root partition by CPU: the x86-64
    # and ARM64 root GUIDs (GPT's own per-arch "Linux root" types)
    # — either names this image's root.
    root_types = {
        "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709",  # Linux root (x86-64)
        "B921B045-1DF0-41C3-AF44-4C6F280D3FAE",  # Linux root (ARM64)
    }
    root = None
    for part in table["partitions"]:
        if part["type"].upper() in root_types:
            root = part
            break
    assert root is not None, "no Linux root partition found"
    print(root["start"] * table.get("sectorsize", 512))
  '';

  # The inode-metadata restoration data, the btrfs cut of #169/
  # #179: the fakeroot session around `btrfs restore -m` holds
  # the distro's own owner/group/mode (special bits included) in
  # its faked view — but a faked view cannot outlive its session,
  # and untarring it under a NEW session lands the special modes
  # on the real inodes (the build user owns them, so the chmod
  # passes through for real), leaving setuid binaries genuinely
  # unreadable: mke2fs -d copies file CONTENTS and its open()
  # runs against the real mode. So the session writes its final
  # view out as a manifest (find -printf under fakeroot reads the
  # fakes), and the pack stage untars with relaxed real modes and
  # re-applies the manifest as fakes — content readable, metadata
  # authoritative. A separate file (not inline) because nix
  # ''-string de-indentation would mangle a python block.
  treeMeta = pkgs.writeText "msks-tree-meta.py" ''
    import os
    import sys

    # Load-bearing invariants of the round trip, asserted after
    # apply (#169's class): sudo and mount come back setuid-root
    # — a parse regression or a base-image change fails the build
    # here, not a workspace's sudo.
    PINS = {"/usr/bin/sudo": 0o4111, "/usr/bin/mount": 0o4755}

    # The ownership drift net (#179's class): the manifest is the
    # session's own view, not an independent walk of the source
    # image, so a restore regression that flattened every owner to
    # root would re-ship silently without a non-root pin. Fedora 44
    # ships no setgid files at all (verified against its rpm db);
    # the unbound state dir is the shipped non-root:non-root path
    # (the chrony and sssd dirs the rpm db names are tmpfiles-
    # created at boot, not in the image).
    OWNER_PINS = {"/var/lib/unbound": 0o755}

    def main():
        argv = sys.argv[1:]
        usage = (
            f"usage: {sys.argv[0]} apply <manifest> <tree>"
        )
        if len(argv) != 3 or argv[0] != "apply":
            raise SystemExit(usage)
        manifest, tree = argv[1], argv[2]
        restored = 0
        skipped = 0
        pins = {}
        # find's %y type letter -> the stat type bits it names.
        types = {"f": 0o100000, "d": 0o040000, "l": 0o120000}
        with open(manifest) as entries:
            for line in entries:
                fields = line.rstrip("\n").split(" ", 4)
                if len(fields) != 5:
                    raise SystemExit(
                        f"unparsable manifest line: {line!r}"
                    )
                mode_s, type_c, uid_s, gid_s, path = fields
                path = "/" + path
                mode = int(mode_s, 8)
                uid = int(uid_s)
                gid = int(gid_s)
                if path in PINS:
                    pins[path] = mode
                if not os.path.lexists(tree + path):
                    # Paths the build deleted or replaced between
                    # the manifest and the pack are expected
                    # absences — unless the inode carried a
                    # special bit (#169's guarantee).
                    if mode & 0o7000:
                        raise SystemExit(
                            f"special-mode path absent from the "
                            f"tree: {path}"
                        )
                    skipped += 1
                    continue
                if type_c == "l" or os.path.islink(tree + path):
                    # A symlink's stored mode is always 0777 and
                    # chmod follows links — it would wreck the
                    # target — so a symlink on EITHER side of the
                    # manifest/pack gap only chowns the link.
                    os.chown(
                        tree + path, uid, gid, follow_symlinks=False
                    )
                else:
                    # Neither side is a symlink: the manifest and
                    # the tree must agree on the inode TYPE — an
                    # untraversable 0640 directory must not ship
                    # because a regular file stood where the
                    # manifest records a directory. Types find has
                    # no letter for (sockets and devices the
                    # distro image does not carry through a
                    # btrfs restore) skip the check.
                    want_type = types.get(type_c)
                    if want_type is not None and want_type != (
                        os.lstat(tree + path).st_mode & 0o170000
                    ):
                        raise SystemExit(
                            f"inode type drift between the manifest "
                            f"and the tree: {path}"
                        )
                    os.chmod(tree + path, mode & 0o7777)
                    os.chown(
                        tree + path, uid, gid, follow_symlinks=False
                    )
                restored += 1
        if not restored:
            raise SystemExit("tree metadata manifest restored nothing")
        for path, want in PINS.items():
            got = pins.get(path)
            if got is None:
                raise SystemExit(
                    f"metadata pin {path} is absent from the "
                    f"manifest; the find -printf parse or the "
                    f"image changed"
                )
            if (got & 0o7777) != want:
                raise SystemExit(
                    f"{path} is {got & 0o7777:04o} in the manifest, "
                    f"expected {want:04o}"
                )
            st = os.lstat(tree + path)
            if (
                not (st.st_mode & 0o4000)
                or st.st_uid != 0
                or st.st_gid != 0
            ):
                raise SystemExit(
                    f"metadata pin did not land on the tree: {path}"
                )
        for path, want in OWNER_PINS.items():
            st = os.lstat(tree + path)
            if (
                (st.st_mode & 0o7777) != want
                or st.st_uid == 0
                or st.st_gid == 0
            ):
                raise SystemExit(
                    f"ownership pin did not land on the tree: {path} "
                    f"(want {want:04o} non-root:non-root, tree has "
                    f"{st.st_mode & 0o7777:04o} {st.st_uid}:{st.st_gid})"
                )
        print(
            f"restored {restored} inodes "
            f"({skipped} absent since the manifest)"
        )

    if __name__ == "__main__":
        main()
  '';

  # The inner half of the Fedora root build: everything between
  # the sliced btrfs partition and the tarball runs inside ONE
  # fakeroot session (see fedoraRoot) — a separate file because
  # the wrapper and the session body read clearer apart, and a
  # quoted-shell-free one at that: nix interpolates the store
  # paths at eval time, the shell keeps its own $-uses.
  rootInner = pkgs.writeText "msks-fedora-root-inner.sh" ''
    set -eu
    tree=root-tree

    # Resolve a soname inside the tree (the guard helper below):
    # the first library directory that carries it, empty when
    # none does.
    resolve_lib() {
      found=
      for d in ${libDirs}; do
        if [ -e "$1"/"$d"/"$2" ]; then
          found="$1"/"$d"/"$2"
          break
        fi
      done
    }

    # btrfs -> tree: a bare restore walks the filesystem and lands
    # every subvolume as a plain directory (root/, boot/, home/,
    # var/); -m restores owner/group/mode — the special bits
    # included — into the fakeroot session's view (the build
    # sandbox cannot set them for real; the session carries them
    # to the tarball below), -S restores symlinks, -i tolerates
    # the unreadable-but-harmless bits every image carries.
    mkdir all
    btrfs restore -m -S -i root.part all/
    rm -f all/config.partids

    # Assemble the flat tree: the root subvolume IS /; the var
    # subvolume's content merges into the mountpoint the root
    # subvolume carries (the workspace's /var content — rpm state
    # markers, systemd's own — rides inside). The boot subvolume
    # contributes the kernel config; its grub trees, UKI legs,
    # initramfs, and vmlinuz never reach a direct-boot guest (the
    # kernel itself lives in the module dir). The home subvolume
    # is empty — the identity seed makes the home on the
    # persistent volume (#14/#171).
    mv all/root "$tree"
    mkdir -p "$tree"/var "$tree"/boot
    cp -a all/var/. "$tree"/var/
    cp all/boot/config-* "$tree"/boot/

    # The zero-module initramfs rides on Fedora building its whole
    # root-mount path in (#651; the #36 drift class): a compose
    # bump that flips any of these to a module would hang every
    # boot at the busybox shell — the initrd loads nothing. The
    # console device's UART differs by arch (8250 on x86_64,
    # PL011 on aarch64); everything else is shared. crc32c is
    # CRYPTO_CRC32C (ext4's metadata_csum asks the crypto API for
    # it), which also backstops archModules dropping Debian's
    # crc32c-* entries.
    case "${arch}" in
      x86_64) console_uart=SERIAL_8250 ;;
      *) console_uart=SERIAL_AMBA_PL011 ;;
    esac
    for symbol in EXT4_FS JBD2 CRYPTO_CRC32C VIRTIO VIRTIO_PCI \
      VIRTIO_BLK "$console_uart"; do
      grep -q "^CONFIG_''${symbol}=y$" "$tree"/boot/config-* \
        || { echo "CONFIG_''${symbol} is not built in; the " \
             "zero-module initramfs needs it" >&2; exit 1; }
    done

    # The msks overlay. cp -a PRESERVES ownership deliberately: the
    # store paths are root-owned for real, and the fakeroot session
    # records the copies as root:root — the same baseline the
    # distro tree carries, with no manifest needed (the deb path's
    # chown-0:0-baseline + manifest apply, folded into one step
    # because extraction and overlay share the session).
    # The store freezes its outputs read-only (files 0444, dirs
    # 0555) and cp -a is mode-faithful, so the chmod pass below
    # puts the operator modes back on the paths the overlay owns —
    # including its directories, which cp -a applied over the
    # distro's own (etc/ would ship 0555 otherwise, and
    # hostnamectl refuses to write /etc/hostname at 0444).
    cp -a ${guestOverlay}/. "$tree"/
    chmod -R u+rw "$tree"/usr/local "$tree"/etc/skel \
      "$tree"/root/.pi "$tree"/etc/cloud \
      "$tree"/etc/ssh/sshd_config.d "$tree"/etc/sudoers.d \
      "$tree"/etc/systemd/system
    chmod u+rw "$tree"/etc "$tree"/etc/hostname "$tree"/etc/fstab

    # The msks workspace user (#63): uid/gid 1000, locked password
    # (no sign-in — ssh keys are the road in), home on the
    # persistent /home volume (#14) — the identity seed creates it
    # from /etc/skel on first boot (#171) — and bash as the shell.
    # The image ships no regular accounts (the Generic image's
    # default user never comes into being under the users: []
    # dropin), which makes this the only regular account the guest
    # boots with — load-bearing for host shares (#631): the
    # daemon's fsdaemons.LOGIN_USER_UID expects the first
    # seed-provisioned login user to take uid 1001.
    grep -q '^msks:' "$tree"/etc/passwd || printf '%s\n' \
      'msks:x:1000:1000:msks workspace user:/home/msks:/bin/bash' \
      >> "$tree"/etc/passwd
    grep -q '^msks:' "$tree"/etc/shadow || printf '%s\n' \
      'msks:!:19700:0:99999:7:::' \
      >> "$tree"/etc/shadow
    grep -q '^msks:' "$tree"/etc/group || printf '%s\n' \
      'msks:x:1000:' \
      >> "$tree"/etc/group
    grep -q '^msks:' "$tree"/etc/gshadow || printf '%s\n' \
      'msks:!::' \
      >> "$tree"/etc/gshadow

    # The admin group's membership (#169, #248): Fedora ships wheel
    # at gid 10 with no members — the build asserts the gid (a
    # base image change that moved it fails here, not the grant)
    # and joins the workspace user to it; the identity seed joins
    # the workspace's login user at first boot. sed -i replaces the
    # file, so the chown/chmod put the session's view back — a
    # fresh inode would otherwise carry the build user.
    grep -q '^wheel:x:10:$' "$tree"/etc/group
    sed -i 's/^wheel:x:10:$/wheel:x:10:msks/' "$tree"/etc/group
    chown 0:0 "$tree"/etc/group
    chmod 0644 "$tree"/etc/group

    # The console prompt contract: the smoke suite's guest-up
    # needle (and the shared console surface across families) is
    # Debian's `root@host:~#` — Fedora's default is
    # `[root@host ~]#`. The appended PS1 restores the shared
    # shape for root and every account the identity seed copies
    # from the skeleton. The .bashrc files (not profile.d) carry
    # the line: Fedora's interactive chain sources /etc/bashrc
    # AFTER profile.d, and it re-sets PS1.
    prompt_line="PS1='\\u@\\h:\\w\\$ '"
    for f in "$tree"/root/.bashrc "$tree"/etc/skel/.bashrc; do
      grep -qF "$prompt_line" "$f" 2>/dev/null \
        || printf '\n# msks: the shared console prompt shape (root@host:~#)\n%s\n' \
          "$prompt_line" >> "$f"
    done

    # The kernel and its module tree come from inside the image
    # (no separate artifact): /usr/lib/modules/<kver>/vmlinuz is
    # the kernel, and the module tree beside it carries depmod's
    # own metadata (unlike Debian's deb, which generates it at
    # install). The guest's runtime needs are the modprobe closure
    # of the modules it loads — virtio_net (egress NICs, #52),
    # isofs (the #41 NoCloud seed disk is iso9660), and the L3
    # recursion set (#82): the nested-KVM modules (a workspace
    # running msksd boots inner workspace VMs through /dev/kvm)
    # and the egress stack (tun for per-inner-workspace taps plus
    # the nftables/NAT and consent modules the daemon's rulesets
    # need). Everything else the Debian closure listed is BUILT IN
    # to Fedora's kernel (ext4, virtio_blk, crc32c); the ACPI
    # button/evdev pair and crc16 ride the list anyway — a builtin
    # resolves to nothing and ships nothing, and the day a compose
    # flips one to a module the closure carries it instead of
    # failing the boot. The closure is resolved mechanically
    # with modprobe --show-depends over the image's own depmod
    # metadata and pinned at build time by comparing the full
    # tree's closure against the shipped tree's (the assert
    # below) — the set cannot drift from the kernel's own
    # dependency facts (the #36 bug class).
    runtimeModules="
      virtio_net
      virtiofs
      isofs
      binfmt_misc
      button
      evdev
      crc16
      ${archModules}
      tun
      nf_tables
      nft_chain_nat
      nft_masq
      nft_ct
      nft_reject
      nft_reject_inet
      nf_reject_ipv4
      nf_nat
      nf_conntrack
      nfnetlink_queue
      nft_queue
      nft_redir
    "
    kver=$(ls "$tree"/usr/lib/modules | head -1)
    find "$tree"/usr/lib/modules -type d -exec chmod u+w {} +
    # Every listed name must resolve (a typo'd entry feeds
    # modprobe's FATAL through awk's success and silently drops
    # from the closure — the closure-equality assert then compares
    # two sets that both lack it). Fedora spells the virtiofs
    # module `virtiofs`, one word, unlike the virtio_fs config
    # symbol.
    for mod in $runtimeModules; do
      modprobe -d "$tree" -S "$kver" --show-depends "$mod" \
        | grep -q '^insmod \|^builtin ' \
        || { echo "runtime module $mod does not resolve in the " \
             "image's module tree" >&2; exit 1; }
    done
    full_closure=$(for mod in $runtimeModules; do
      modprobe -d "$tree" -S "$kver" --show-depends "$mod" \
        | awk '/^insmod /{print $2}'
    done | xargs -n1 basename | sort -u)
    keep=$(for mod in $runtimeModules; do
      modprobe -d "$tree" -S "$kver" --show-depends "$mod" \
        | awk '/^insmod /{print $2}'
    done | sort -u)
    find "$tree"/usr/lib/modules/"$kver"/kernel -type f \
      ! -name "*.ko.*" -delete 2>/dev/null || true
    while read -r ko; do
      [ -n "$ko" ] || continue
      rel=''${ko#*modules/"$kver"/}
      mkdir -p shipped/"$kver"/"$(dirname "$rel")"
      cp "$ko" shipped/"$kver"/"$rel"
    done <<<"$keep"
    rm -rf "$tree"/usr/lib/modules/"$kver"/kernel
    mv shipped/"$kver"/kernel "$tree"/usr/lib/modules/"$kver"/kernel
    # modprobe reads modules.builtin to skip built-ins: without
    # the file, every builtin alias resolves to a missing module.
    depmod -b "$tree" "$kver"
    test -s "$tree"/usr/lib/modules/"$kver"/modules.dep
    tree_closure=$(for mod in $runtimeModules; do
      modprobe -d "$tree" -S "$kver" --show-depends "$mod" \
        | awk '/^insmod /{print $2}'
    done | xargs -n1 basename | sort -u)
    if [ "$full_closure" != "$tree_closure" ]; then
      echo "guest module tree closure mismatch:" >&2
      echo "full tree resolves: $full_closure" >&2
      echo "shipped tree resolves: $tree_closure" >&2
      exit 1
    fi
    # The closure guard cannot see autoload names: the rulesets
    # ask for modules by nft expression/chain alias, and no
    # runtimeModules root depends on the expression modules —
    # the first cut of #552 shipped the backing libraries and
    # still could not install its ruleset. Every autoload name
    # the daemon's rulesets carry (nft.py's queue_gate and
    # armed_rules) must resolve against the SHIPPED tree's own
    # depmod metadata or the build fails (#552).
    rulesetAliases="
      nft-expr-1-reject
      nft-expr-queue
      nfnetlink-subsys-3
      nft-expr-redir
      nft-expr-masq
      nft-expr-ct
      nft-chain-1-nat
    "
    for alias in $rulesetAliases; do
      if ! modprobe -d "$tree" -S "$kver" --show-depends "$alias" \
        | grep -q '^insmod '; then
        echo "guest module tree lacks a module for autoload alias: $alias" >&2
        exit 1
      fi
    done

    # Boot diet (#37's class): drop the wants of units a workspace
    # never uses. chronyd has no served clock until egress exists
    # and its pool probes cost boot time — the timesyncd
    # equivalent the Debian diet drops. sshd, NetworkManager, and
    # the resolver pieces stay.
    wants="$tree"/etc/systemd/system
    chmod u+w "$wants"/*.target.wants "$wants"/*.target.requires 2>/dev/null || true
    rm -f "$wants"/multi-user.target.wants/chronyd.service
    # zram swap: the generator's config would spawn a dev-zram0.device
    # job that no workspace VM satisfies (no zram module, no device) —
    # the job stalls the boot ~45s waiting for a device that never
    # appears. The workspace's memory is host-managed; swap on a
    # zram device adds nothing.
    rm -f "$tree"/usr/lib/systemd/zram-generator.conf

    # fd-find and ripgrep (#272): the distro's own binaries from
    # the pinned rpms (see fdFindRpm/ripgrepRpm) — Fedora's fd
    # binary is named fd, the name pi probes first — staged
    # binaries-only, with the same linkage guard the Debian
    # image's deb-staged tools get: every NEEDED soname resolves
    # inside the tree, the ELF interpreter resolvable, and every
    # version symbol the binary requires defined by the tree's
    # copy of that library (a rebase onto newer symbols than the
    # image ships is the #36 bug class). Only the "Version needs"
    # section is parsed: a binary that also defines versioned
    # symbols would otherwise feed its own definitions back in as
    # needs. The strip regex must be a bracket expression
    # ([\[\]], the set of both brackets) — the two-character
    # escape would match only adjacent [], the unstripped [libx.so]
    # word is a glob, and stdenv's nullglob then deletes it from
    # the `for` word list — the loop silently checks nothing.
    rpm_elf_guard() {
      bin=$1
      name=$(basename "$bin")
      for so in $(readelf -d "$bin" \
        | awk '/NEEDED/{gsub(/[\[\]]/,"",$NF); print $NF}'); do
        resolve_lib "$tree" "$so"
        [ -n "$found" ] \
          || { echo "$name needs $so, absent from the tree" >&2; \
               exit 1; }
      done
      interp=$(readelf -l "$bin" \
        | awk '/interpreter/{gsub(/[\[\]]/,"",$NF); print $NF}')
      # A static pin would leave interp empty and pass the check
      # below vacuously — the rpm's shape changed, fail the build
      # and restage deliberately.
      [ -n "$interp" ] \
        || { echo "$name has no interpreter; the rpm went static" \
             >&2; exit 1; }
      test -e "$tree""$interp" \
        || { echo "$name loader $interp absent from the tree" >&2; \
             exit 1; }
      reqs=$(readelf --version-info "$bin" \
        | awk '/Version needs section/{need=1; next} \
            /Version definition section/{need=0} \
            need && /File: /{f=$5} \
            need && /  Name: /{print f, $3}')
      while read -r so ver; do
        [ -n "$so" ] || continue
        resolve_lib "$tree" "$so"
        lib=$found
        [ -n "$lib" ] \
          || { echo "$name needs $so, absent from the tree" >&2; \
               exit 1; }
        # Capture then grep — no pipe: `grep -q` exits at its
        # first match, and under pipefail a readelf still writing
        # its large output takes the SIGPIPE and the pipeline
        # reports failure DESPITE the match.
        readelf --version-info "$lib" > verinfo.tmp || {
          echo "readelf failed on $lib" >&2; exit 1; }
        grep -q "Name: $ver" < verinfo.tmp || {
          echo "$name needs $ver from $so; the tree's " \
               "copy is older" >&2; exit 1; }
      done <<<"$reqs"
    }
    mkdir fd-rpm rg-rpm jemalloc-rpm
    (cd fd-rpm && rpm2cpio ${fdFindRpm} | cpio -idm --quiet)
    (cd rg-rpm && rpm2cpio ${ripgrepRpm} | cpio -idm --quiet)
    (cd jemalloc-rpm && rpm2cpio ${jemallocRpm} | cpio -idm --quiet)
    cp -a fd-rpm/usr/bin/fd "$tree"/usr/bin/fd
    cp -a rg-rpm/usr/bin/rg "$tree"/usr/bin/rg
    cp -a jemalloc-rpm/usr/lib64/libjemalloc.so.2 \
      "$tree"/usr/lib64/libjemalloc.so.2
    test -x "$tree"/usr/bin/fd
    test -x "$tree"/usr/bin/rg
    rpm_elf_guard "$tree"/usr/bin/fd
    rpm_elf_guard "$tree"/usr/bin/rg

    # The metadata pins (#169's class, the btrfs cut): restore -m
    # carries the special bits into the fakeroot session's view,
    # and the pins assert they arrived — sudo's setuid grant and
    # mount's, plus a floor on the setuid population so a restore
    # regression that dropped the lot fails here, not a
    # workspace's sudo.
    test "$(( 0$(stat -c %a "$tree"/usr/bin/sudo) & 04000 ))" -ne 0 \
      || { echo "sudo is $(stat -c %a "$tree"/usr/bin/sudo) on the " \
           "tree; the restore -m metadata pass broke" >&2; exit 1; }
    test "$(( 0$(stat -c %a "$tree"/usr/bin/mount) & 04000 ))" -ne 0 \
      || { echo "mount is $(stat -c %a "$tree"/usr/bin/mount) on the " \
           "tree; the restore -m metadata pass broke" >&2; exit 1; }
    setuid_n=$(find "$tree" -xdev -perm -4000 -type f | wc -l)
    [ "$setuid_n" -ge 5 ] \
      || { echo "only $setuid_n setuid files on the tree; the " \
           "restore metadata pass broke" >&2; exit 1; }

    # No baked host keys — ever (#110): each workspace generates
    # its own on first boot; an upstream image that started
    # shipping some would give every workspace the same keys.
    ! ls "$tree"/etc/ssh/ssh_host_* >/dev/null 2>&1

    # Sanity: this must be a bootable Fedora.
    test -x "$tree"/sbin/init
    test -x "$tree"/usr/bin/rsync
    test -x "$tree"/usr/sbin/sshd
    # Sanity: the baked agent toolchain (#266) — an upstream
    # tarball or layout change must fail the build here, not
    # boot a workspace with a broken agent (the #36 bug class).
    # claude is a symlink chain down into its npm tree; test -x
    # resolves it, proving the whole chain.
    test -x "$tree"/usr/local/bin/node
    test -x "$tree"/usr/local/bin/npm
    test -x "$tree"/usr/local/bin/npx
    test -x "$tree"/usr/local/bin/pi
    test -x "$tree"/usr/local/bin/herdr
    test -x "$tree"/usr/local/bin/claude
    test -x \
      "$tree"/usr/local/lib/node_modules/@anthropic-ai/claude-code/node_modules/@anthropic-ai/${toolchain.claudePlatformName}/claude
    test -f \
      "$tree"/etc/skel/.pi/agent/extensions/llm-models.ts
    test -f "$tree"/root/.pi/agent/extensions/llm-models.ts
    test -f "$tree"/etc/skel/.pi/agent/settings.json
    test -f "$tree"/root/.pi/agent/settings.json
    # The toolchain linkage guard (#267 review): the same checks
    # the staged rpms get, for the two dynamically linked tools —
    # every NEEDED soname resolves in the tree, the interpreter
    # exists, and every version symbol the binary asks a library
    # for is defined by the tree's copy of it. A future pin bump
    # onto a newer libc or a musl build fails here, not at the
    # workspace's first launch. herdr is asserted static: it
    # needs nothing from the tree.
    for bin in "$tree"/usr/local/bin/node \
      "$tree"/usr/local/lib/node_modules/@anthropic-ai/claude-code/node_modules/@anthropic-ai/${toolchain.claudePlatformName}/claude; do
      for so in $(readelf -d "$bin" \
        | awk '/NEEDED/{gsub(/[\[\]]/,"",$NF); print $NF}'); do
        resolve_lib "$tree" "$so"
        [ -n "$found" ] \
          || { echo "$bin needs $so, absent from the tree" >&2; \
               exit 1; }
      done
      interp=$(readelf -l "$bin" \
        | awk '/interpreter/{gsub(/[\[\]]/,"",$NF); print $NF}')
      test -e "$tree""$interp" \
        || { echo "$bin loader $interp absent from the tree" >&2; \
             exit 1; }
      # Only the "Version needs" section: the toolchain binaries
      # also carry version *definitions* (bun-profile, BUN_1.2)
      # whose lines would masquerade as needs.
      reqs=$(readelf --version-info "$bin" \
        | awk '/Version needs section/{need=1} \
            /Version definition section/{need=0} \
            need && /File: /{f=$5} \
            need && /  Name: /{print f, $3}')
      while read -r so ver; do
        [ -n "$so" ] || continue
        resolve_lib "$tree" "$so"
        lib=$found
        [ -n "$lib" ] \
          || { echo "$bin needs $so, absent from the tree" >&2; \
               exit 1; }
        # Capture then grep — no pipe (the SIGPIPE-under-pipefail
        # race the deb guard hit; same class, same fix).
        readelf --version-info "$lib" > verinfo.tmp || {
          echo "readelf failed on $lib" >&2; exit 1; }
        grep -q "Name: $ver" < verinfo.tmp \
          || { echo "$bin needs $ver from $so; the tree's copy is older" \
               >&2; exit 1; }
      done <<<"$reqs"
    done
    ! readelf -l "$tree"/usr/local/bin/herdr | grep -q interpreter \
      || { echo "herdr is not static; the pin changed shape" >&2; \
           exit 1; }
    test -f "$tree"/usr/local/share/doc/herdr/LICENSE
    # The launchers must execute (#272): test -x proves a symlink
    # chain, not a working interpreter — the bug this closes
    # shipped a cli.js whose shebang named a store node no guest
    # carries, and every launcher passed test -x. The build
    # sandbox runs no /usr/bin/env and no loader, so the env
    # shebang is asserted byte-exact and each dynamic tool runs
    # through the tree's own loader with the tree's libraries —
    # the same world the guest execs it in. (herdr is static and
    # runs as-is; pi runs under the staged node, the interpreter
    # its shebang resolves to on a real guest.) A scratch HOME
    # keeps the JS tools from writing into the build user's home.
    # The version greps pin output shape, not version: the pins
    # live in this file and nix/agent-toolchain.nix.
    cli="$tree"/usr/local/lib/node_modules/pi-coding-agent/dist/bundle/cli.js
    [ "$(head -n 1 "$cli")" = '#!/usr/bin/env node' ] \
      || { echo "pi cli.js lost its env-node shebang" >&2; exit 1; }
    npmcli="$tree"/usr/local/lib/node_modules/npm/bin/npm-cli.js
    [ "$(head -n 1 "$npmcli")" = '#!/usr/bin/env node' ] \
      || { echo "npm-cli.js lost its env-node shebang" >&2; exit 1; }
    ldso="$tree"${elfLoader}
    libpath=""
    for d in ${libDirs}; do
      libpath="$libpath:$tree"/"$d"
    done
    run_tool() { "$ldso" --library-path "$libpath" "$@"; }
    run_tool "$tree"/usr/local/bin/node --version \
      | grep -q '^v[0-9][0-9.]*$'
    mkdir pi-home
    HOME="$PWD"/pi-home run_tool "$tree"/usr/local/bin/node \
      "$tree"/usr/local/bin/pi --version \
      | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'
    "$tree"/usr/local/bin/herdr --version \
      | grep -q '^herdr [0-9]'
    HOME="$PWD"/pi-home run_tool "$tree"/usr/local/bin/claude \
      --version | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+'
    run_tool "$tree"/usr/bin/fd --version | head -1 \
      | grep -q '^fd [0-9]'
    run_tool "$tree"/usr/bin/rg --version | head -1 \
      | grep -q '^ripgrep [0-9]'
    # The dropin's load-bearing line, not just the file's
    # existence: a typo'd printf must fail the build here, not
    # in the opt-in smoke.
    grep -q '^users: \[\]' \
      "$tree"/etc/cloud/cloud.cfg.d/99-msks-users.cfg
    grep -q '^manage_etc_hosts: localhost$' \
      "$tree"/etc/cloud/cloud.cfg.d/99-msks-etc-hosts.cfg
    # The image's own trees carry no home for the workspace user
    # (the seed makes it on the persistent volume) and none for a
    # cloud-image default account either (#171).
    ! test -e "$tree"/home/msks
    ! test -e "$tree"/home/fedora
    # The version guard (#40's drift class): the catalog label
    # must match the Fedora this tree actually is.
    grep -q '^Fedora release ${imageVersion} ' \
      "$tree"/etc/fedora-release

    # The kernel bits beside the tar: the pack stages and the
    # archive's boot tree copy them.
    cp "$tree"/usr/lib/modules/"$kver"/vmlinuz vmlinuz-out
    cp "$tree"/boot/config-* config-out
    printf '%s' "$kver" > kver-out

    # Size the final image from the tree (content-derived, no
    # magic constant).
    du -s --apparent-size --block-size=4096 "$tree" | cut -f1 \
      > tree-blocks
    # The session's faked view, written out for the pack stage
    # (see treeMeta): every inode's mode/uid/gid, read through
    # fakeroot's stat so the distro's own metadata — restore -m's
    # view, the overlay's root:root, the staged rpms — is what
    # lands in the manifest.
    find "$tree" -printf '%m %y %U %G %P\n' | sort -k5 > inode-meta
    # The intermediate rides the store as ONE opaque blob, never
    # as a tree (the store's auto-optimise hardlinks identical
    # files inside tree-shaped paths, and mke2fs -d packs
    # hardlink groups as one inode — a tarball cannot be deduped
    # from inside; btrfs restore flattens all hardlinks anyway,
    # so none are recorded). Sorted member order keeps the
    # tarball deterministic; the mtimes inside it are NOT
    # (depmod's outputs are build-time), and need not be —
    # byte-stability of the final image comes from
    # E2FSPROGS_FAKE_TIME plus the archive tar's --mtime=@1, not
    # from this hop.
    tar --sort=name --numeric-owner \
      -C "$tree" -cf root.tar .
  '';

  # The Fedora root tree: convert the qcow2 to raw, slice the root
  # partition out (offset from the partition table, not
  # hardcoded), and run the extraction/overlay/pack inner script
  # inside ONE fakeroot session — the session's faked view (the
  # distro's own owner/group/mode, special bits included) is the
  # metadata authority the tarball carries to the ext4 pack. A
  # fresh ext4 is built from the tree later, so the session never
  # writes a filesystem.
  fedoraRoot =
    pkgs.runCommand "msks-fedora-root"
      {
        nativeBuildInputs = [
          pkgs.binutils # readelf: the staged-rpm soname guards
          pkgs.btrfs-progs
          pkgs.cpio
          pkgs.fakeroot
          pkgs.gnutar
          pkgs.kmod
          pkgs.qemu
          pkgs.rpm # rpm2cpio for the staged fd/rg rpms
          pkgs.util-linux
          (pkgs.python3.withPackages (ps: [ ]))
        ];
      }
      ''
        set -eu
        # The tree builds in the derivation's scratch cwd (discarded
        # with it); the retained output is a single opaque root.tar
        # beside the kernel bits the pack stages copy.
        mkdir -p "$out"

        # qcow2 -> raw
        qemu-img convert -O raw ${fedoraImage} fedora.raw

        # Slice the root partition: the GPT partition typed Linux
        # root for the arch (the cloud image keeps EFI + BIOS boot
        # partitions around it, which direct kernel boot does not
        # need).
        offset=$(sfdisk --json fedora.raw | python3 ${partitionOffset})
        dd if=fedora.raw of=root.part bs=512 skip=$((offset / 512)) status=none

        # Everything else happens inside the fakeroot session
        # (bash: the inner script uses its here-string forms, the
        # same as the Debian builder).
        fakeroot -- ${pkgs.bash}/bin/bash -e ${rootInner}

        mv root.tar "$out"/root.tar
        mv tree-blocks "$out"/tree-blocks
        mv inode-meta "$out"/inode-meta
        cp vmlinuz-out "$out"/vmlinuz
        cp config-out "$out"/kernel-config
        cp kver-out "$out"/kernel-version
      '';

  # mke2fs -d packs a directory into an ext4 image without mounting
  # anything — the whole build stays unprivileged and
  # host-independent. The untar runs OUTSIDE fakeroot and the
  # chmod -R u+rw relaxes the real modes (the tarball's stored
  # modes can land unreadable setuid binaries for real — the
  # build user owns them, so tar's chmods pass through — and
  # mke2fs -d's open() reads the real mode); the fakeroot session
  # then re-applies the session view the manifest carries, and
  # mke2fs -d's stat() reads the fakes — content readable,
  # metadata authoritative, exactly the deb path's two halves.
  packScript = pkgs.writeText "msks-rootfs-pack-fedora.sh" ''
    set -eu
    tree="''${PACK_TREE:?}"
    img="''${PACK_IMG:?}"
    blocks="''${PACK_BLOCKS:?}"
    fake_epoch="''${PACK_FAKE_EPOCH:?}"
    meta="''${PACK_META:?}"
    applier="''${PACK_APPLIER:?}"
    python3 "$applier" apply "$meta" "$tree"
    chmod 0640 "$tree"/etc/shadow "$tree"/etc/gshadow
    chmod 0440 "$tree"/etc/sudoers.d/msks
    E2FSPROGS_FAKE_TIME="$fake_epoch" mke2fs -q -t ext4 -b 4096 -I 256 \
      -L msks-rootfs \
      -E hash_seed=00000000-0000-0000-0000-000000000000 \
      -d "$tree" "$img" "$blocks"
    E2FSPROGS_FAKE_TIME="$fake_epoch" tune2fs -U clear "$img" >/dev/null
  '';

  rootfs =
    pkgs.runCommand "msks-guest-rootfs-fedora"
      {
        inherit fedoraRoot packScript treeMeta;
        nativeBuildInputs = [
          pkgs.e2fsprogs
          pkgs.fakeroot
          pkgs.gnutar
          pkgs.python3
        ];
        fakeEpoch = 1262304000;
      }
      ''
        set -eu
        mkdir -p "$out"
        # The tree arrives as one opaque tarball (see fedoraRoot):
        # every path untars to its own inode, and the store cannot
        # dedup inside a blob — so mke2fs -d can never pack a
        # fabricated shared inode for the guest's runtime writes to
        # collide in. The untar runs as the build user (no
        # --same-owner: ownership rides the manifest), and u+rw
        # keeps every real file readable for mke2fs -d's open();
        # the fakeroot session below restores the metadata the
        # manifest carries. Content plus 1G of slack: the base
        # keeps room for image updates, and the per-workspace
        # overlay (#14) carries whatever the guest writes beyond
        # it.
        mkdir work
        tar -C work -xf "$fedoraRoot/root.tar"
        chmod -R u+rw work
        PACK_TREE="$PWD"/work \
          PACK_IMG="$out/rootfs.ext4" \
          PACK_BLOCKS=$(( $(cat "$fedoraRoot"/tree-blocks) + 262144 )) \
          PACK_FAKE_EPOCH="$fakeEpoch" \
          PACK_META="$fedoraRoot/inode-meta" \
          PACK_APPLIER="${treeMeta}" \
          fakeroot -- ${pkgs.bash}/bin/bash -e "$packScript"
      '';

  # The canonical image artifact (#40): a container-image tar
  # (`podman load` compatible) in the containerDisk convention — one
  # layer carrying boot/ (kernel, initrd) and disk/ (rootfs.ext4,
  # image.json schema 2). Importable with podman/skopeo/plain tar.
  bootTree =
    pkgs.runCommand "msks-image-boot-tree-fedora"
      {
        inherit
          fedoraRoot
          rootfs
          minimalInitrd
          ;
        inherit
          imageName
          imageVersion
          kernelCmdline
          ;
      }
      ''
        set -eu
        vmlinuz="${fedoraRoot}/vmlinuz"
        initrd="${minimalInitrd}/initrd"
        mkdir -p "$out"/boot "$out"/disk
        cp "$vmlinuz" "$out"/boot/vmlinuz
        cp "$initrd" "$out"/boot/initrd.img
        cp "${rootfs}/rootfs.ext4" "$out"/disk/rootfs.ext4
        kernel_version=$(cat "${fedoraRoot}/kernel-version")
        # Guard against version drift: the catalog label must match
        # the Fedora tree this image actually wraps. /etc/fedora-
        # release is a symlink (../usr/lib/fedora-release); tar -xOf
        # names members, so the regular file behind it is the one
        # to read.
        shipped=$(tar -xOf "$fedoraRoot/root.tar" ./usr/lib/fedora-release \
          | awk '{print $3}')
        if [ "$shipped" != "${imageVersion}" ]; then
          echo "imageVersion ${imageVersion} != /etc/fedora-release $shipped" >&2
          exit 1
        fi
        # Self-describing (#40 review): the archive alone builds a boot
        # spec — no sidecar metadata for foreign imports to miss.
        cat > "$out"/disk/image.json <<EOF
        {
          "schema": 2,
          "arch": "${arch}",
          "name": "${imageName}",
          "version": "${imageVersion}",
          "cmdline": "${kernelCmdline}",
          "kernel_version": "$kernel_version",
          "kernel_format": "${facts.kernelFormat}",
          "description": "Fedora ${imageVersion} cloud image (compose 1.7), kernel $kernel_version, cloud-init, the egress module set and consent stack",
          "capabilities": {"provisioner": "${imageProvisioner}"}
        }
        EOF
      '';

  # The image archive: the shared containerDisk packer (#40
  # review, shared since #250 — nix/image-archive.nix owns the
  # format: the podman-save layout, the uncompressed byte-stable
  # layer, the per-name:version image id).
  imageArchive = (pkgs.callPackage ./image-archive.nix { }).mkImageArchive {
    inherit
      bootTree
      imageName
      imageVersion
      ;
    architecture = facts.ociArch;
  };

in
pkgs.runCommand "msks-guest-fedora"
  {
    inherit
      fedoraRoot
      rootfs
      imageArchive
      imageName
      imageVersion
      ;
    passthru = {
      inherit
        fedoraImage
        imageArchive
        ;
      inherit
        kernelCmdline
        ;
    };
  }
  ''
    set -eu
    mkdir -p "$out"
    # Fedora's in-image kernel (#651: no separate artifact — the
    # vmlinuz rides in /usr/lib/modules) plus the minimal
    # initramfs; the version string names the kernel the guest
    # actually boots.
    version=$(cat "${fedoraRoot}/kernel-version")
    cp "${fedoraRoot}/vmlinuz" "$out/vmlinux"
    cp "${minimalInitrd}/initrd" "$out/initrd"
    cp "${rootfs}/rootfs.ext4" "$out/rootfs.ext4"
    # The canonical artifact: named by name-version, OCI layout inside.
    cp "${imageArchive}" "$out/workspace-''${imageName}-''${imageVersion}.tar"
    printf '%s' "workspace-''${imageName}-''${imageVersion}.tar" > "$out"/image-archive-name
    printf '%s' "$version" > "$out"/kernel-version
    # An unquoted heredoc: $version expands in the shell; the
    # cmdline was interpolated by nix at eval time.
    cat > "$out"/guest-manifest.json <<EOF
    {
      "schema": 1,
      "arch": "${arch}",
      "kernel_version": "$version",
      "kernel_format": "${facts.kernelFormat}",
      "cmdline": "${kernelCmdline}",
      "vmlinux": "vmlinux",
      "initrd": "initrd",
      "rootfs": "rootfs.ext4"
    }
    EOF
  ''
