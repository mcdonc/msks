# Guest VM assets for direct kernel boot on cloud-hypervisor (#5).
#
# Everything a microvm needs comes out of this file as plain store
# paths, built from the pinned nixpkgs by pure derivations: the build
# runs on any Linux host with nix and touches nothing outside the
# repo.
#
# The root filesystem is Debian 13 (trixie), straight from Debian's
# official genericcloud cloud image (#30, #41): real Debian with
# PID 1, apt, Debian's own modules — and Debian's own socat (built
# WITH_VSOCK) serving the vsock console. The kernel is Debian's
# *generic* flavor of the same upstream version (#96) — the SAME
# pin the appliance image boots, so one deb serves both and the
# appliance's virtiofs/KVM needs drove the choice. The minimal
# initramfs msks builds carries the six modules the generic flavor
# needs to mount the ext4 root (the cloud flavor #37 chose built
# ext4 in; the ~2.5s it dodged was Debian's stock 34MB MODULES=most
# archive, not the flavor — the six-module initrd costs tens of
# milliseconds). The image is pinned by its dated cloud.debian.org
# URL and sha512; the kernel by its deb.debian.org pool URL and
# sha256 ("latest" is a moving pointer; dated builds stay
# published).
#
#   $out/vmlinux            - Debian's generic kernel (bzImage, PVH
#                             entry point; CONFIG_PVH=y). Named
#                             "vmlinux" to match the
#                             MSKSD_TEST_VMLINUX contract;
#                             guest-manifest.json records the actual
#                             format.
#   $out/initrd             - msks-built minimal initramfs: busybox,
#                             the six root-mount modules, mount
#                             root, switch_root.
#   $out/rootfs.ext4        - the extracted Debian tree as a fresh
#                             ext4 image: the pristine base each
#                             workspace's overlay copies on write
#                             from (#14); guests mount it rw.
#   $out/guest-manifest.json - artifact names + the boot cmdline.
#
# Extraction is unprivileged — debugfs rdump, no mount — and rdump
# drops setuid/setgid bits, so the build records the source image's
# own special modes at dump time and the fakeroot pack stage applies
# them with uid-0 ownership (#169): the msks user's sudo (#63)
# depends on the pairing.
#
# Evaluate through the msks-build-guest / msks-build-runner-image
# scripts (they pin nixpkgs to the devenv.lock revision);
# `nix-build nix/guest.nix -A guest` with plain NIX_PATH also works
# when the pinned channel is acceptable.
{
  lib,
  pkgs,
}:

let
  # The official Debian 13 genericcloud image (#41): systemd plus
  # cloud-init and its python3 runtime — the workspace's cidata seed
  # is NoCloud's own format, so first-boot provisioning needs no
  # msks-side consumer. Roughly 130M heavier than the nocloud variant
  # the build used before; the boot diet below keeps the console fast.
  debianImage = pkgs.fetchurl {
    urls = [
      "https://cloud.debian.org/images/cloud/trixie/20260831-2587/debian-13-genericcloud-amd64-20260831-2587.qcow2"
    ];
    hash = "sha512:8ea9faae810043a0b35b0149f05014f26705c2339ffb11ead308f33e844a87cc3ef46ec81d5262b38817b6a88af404874d48a5857ebe072ef6a31dfb6e371f50";
  };

  # The port the guest's vsock console listens on; the daemon dials
  # it after the CONNECT handshake (#21). Fixed, recorded in the
  # manifest, matched by the systemd unit below.
  vsockShellPort = 1023;

  # First-boot provisioning (#41): the image's declared seed-disk
  # consumer — cloud-init, shipped by the genericcloud source. Both
  # payload forms work: cloud-config YAML and #! scripts.
  imageProvisioner = "cloud-init";

  # The workspace image identity (#40): the catalog reference is
  # <name>:<version>.
  imageName = "debian";
  imageVersion = "13.6";

  # Root boots read-write (#14): the per-workspace qcow2 overlay
  # absorbs writes over this pristine base — copy-on-write protects
  # it, an ro mount would block apt and provisioning state.
  kernelCmdline = "console=ttyS0 root=/dev/vda rootfstype=ext4 rw";

  # Debian's GENERIC kernel flavor (#96), the same pin the
  # appliance image boots — one deb fetch and one version pin serve
  # both images; that sharing is the whole motivation (the flavor
  # difference itself costs the guest little: virtio-pci and
  # virtiofs are BUILT IN here, ext4 and virtio_blk are modules —
  # so the initramfs below loads six modules in dependency order,
  # and the guest's runtime module tree is the modprobe closure of
  # the modules it actually loads, see debianRoot). Pinned by pool
  # URL and sha256; the deb carries vmlinuz, its config, and the
  # matching /usr/lib/modules tree.
  genericKernelDeb = pkgs.fetchurl {
    url =
      "https://deb.debian.org/debian/pool/main/l/linux/"
      + "linux-image-6.12.107+deb13-amd64-unsigned_6.12.107-1_amd64.deb";
    hash = "sha256-fRPNgqHTd+QIJsMT9du9su3xtIxxOjdeTP5eIHdJy04=";
  };

  genericKernel =
    pkgs.runCommand "msks-generic-kernel"
      {
        nativeBuildInputs = [ pkgs.dpkg ];
      }
      ''
        set -eu
        dpkg-deb -x ${genericKernelDeb} "$out"
      '';

  # Debian's own rsync (#110), pinned by pool URL and sha256 like the
  # kernel deb above: the binary is dynamically linked against
  # exactly the libraries the Debian tree ships (glibc 2.41 covers
  # the deb's libc6 >= 2.38), so the guest's rsync is the distro's —
  # same build, same flags, same protocol behavior an operator
  # expects from `rsync -e ssh` on any Debian box. The build asserts
  # every NEEDED soname resolves inside the tree (the #36 bug class).
  rsyncDeb = pkgs.fetchurl {
    url =
      "https://deb.debian.org/debian/pool/main/r/rsync/"
      + "rsync_3.4.1+ds1-5+deb13u4_amd64.deb";
    hash = "sha256-iqEi9rqNL/ESxyu5gU7glu5RbwITs2uYu+ecg8kvsiY=";
  };

  # The minimal initramfs (#37's shape, #96's module set): busybox,
  # the six modules the generic kernel needs to mount the ext4
  # root, and an init that mounts /dev/vda rw and switch_roots into
  # systemd. The stock generic Debian initrd this replaces is a 34MB
  # MODULES=most archive and sat ~2.5s deep in the boot critical
  # path; the six hand-ordered insmods cost tens of milliseconds.
  minimalInitrd =
    pkgs.runCommand "msks-minimal-initrd"
      {
        inherit genericKernel;
        # Static: the initramfs has no dynamic loader. nixpkgs'
        # default busybox links against a store glibc.
        busybox = pkgs.pkgsStatic.busybox;
        nativeBuildInputs = [
          pkgs.cpio
          pkgs.gzip
          pkgs.xz
        ];
      }
      ''
        set -eu
        mkdir -p "$out"/tree/bin "$out"/tree/modules \
          "$out"/tree/proc "$out"/tree/dev "$out"/tree/newroot
        cp "$busybox"/bin/busybox "$out"/tree/bin/busybox
        moddir="$genericKernel/usr/lib/modules"
        moddir=$(echo "$moddir"/*)
        # .ko.xz: busybox insmod reads plain modules only. The list
        # is dependency order: ext4's direct deps (crc16, mbcache,
        # jbd2), then ext4 itself, then virtio_blk (independent).
        # crc32c is not a modules.dep edge but a mount-time crypto
        # request — the generic kernel ships it as a module (the
        # cloud kernel built it in) and a metadata_csum rootfs
        # cannot mount without it.
        for ko in \
          kernel/lib/crc16.ko.xz \
          kernel/crypto/crc32c_generic.ko.xz \
          kernel/fs/mbcache.ko.xz \
          kernel/fs/jbd2/jbd2.ko.xz \
          kernel/fs/ext4/ext4.ko.xz \
          kernel/drivers/block/virtio_blk.ko.xz; do
          name=$(basename "$ko" .ko.xz)
          xz -dc "$moddir"/"$ko" > "$out"/tree/modules/"$name".ko
        done
        cat > "$out"/tree/init <<'INIT'
        #!/bin/busybox sh
        # Mount root and hand off to systemd (#37): keep this as small
        # as it looks — every millisecond here delays the console. On
        # any failure, a shell beats a silent hang in the ~1.4MB
        # initramfs (the serial console is reachable). rw matches the
        # image cmdline (#14): the overlay carries the writes.
        /bin/busybox mount -t proc proc /proc \
          && /bin/busybox mount -t devtmpfs devtmpfs /dev \
          && /bin/busybox insmod /modules/crc16.ko \
          && /bin/busybox insmod /modules/crc32c_generic.ko \
          && /bin/busybox insmod /modules/mbcache.ko \
          && /bin/busybox insmod /modules/jbd2.ko \
          && /bin/busybox insmod /modules/ext4.ko \
          && /bin/busybox insmod /modules/virtio_blk.ko \
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

  # The console identity helper (#63): one static binary that owns
  # the vsock listener (replacing the socat EXEC line), negotiates
  # the identity prelude, applies the window size, and drops to the
  # requested user before exec'ing that user's shell.
  #
  # pkgsStatic (musl) makes the static link the default, so nothing
  # depends on glibc's layout; the guest rootfs (Debian) and the host
  # nixpkgs pin ship different glibcs, and a dynamically linked helper
  # would only run on one of them. NSS never matters — the helper
  # parses /etc/passwd and /etc/group itself. Sources and lockfile
  # live in src/console-helper/; the devenv shell (languages.rust)
  # carries the toolchain for local builds and the coverage gate.
  consoleHelper = pkgs.pkgsStatic.rustPlatform.buildRustPackage {
    pname = "msks-console-helper";
    version = "0.1.0";
    src = ../src/console-helper;
    cargoLock.lockFile = ../src/console-helper/Cargo.lock;
    doCheck = false;
  };

  # The msks additions, staged as an overlay tree: the vsock console
  # service, serial-console autologin (the debug console), the vsock
  # and net module loads, the nested-KVM module and inner-egress
  # stack a workspace running msksd itself needs (#82), a stable
  # hostname, the DHCP client an egress workspace (#52) brings up,
  # the sshd posture + rsync the TCP service plane rides (#110),
  # the sudoers grant behind the workspace user's sudo (#169), and
  # the console helper binary.
  # Debian's socat 1.8.x is built WITH_VSOCK, so nothing is
  # cross-compiled in.
  guestOverlay = pkgs.runCommand "msks-guest-overlay" { } ''
    set -eu
    mkdir -p \
      $out/home \
      $out/usr/bin \
      $out/etc/cloud/cloud.cfg.d \
      $out/etc/sudoers.d \
      $out/etc/ssh/sshd_config.d \
      $out/etc/systemd/system/serial-getty@ttyS0.service.d \
      $out/etc/systemd/system/ssh.service.d \
      $out/etc/systemd/system/multi-user.target.wants \
      $out/etc/systemd/system/sockets.target.wants \
      $out/etc/systemd/system/sysinit.target.wants \
      $out/etc/systemd/network \
      $out/etc/modules-load.d

    printf 'msks-guest\n' > $out/etc/hostname

    # The console helper (#63): the only privileged listener in the
    # image. Mode 0755 — it drops privileges itself; it is never
    # setuid.
    cp "${consoleHelper}/bin/msks-console-helper" \
      $out/usr/bin/msks-console-helper
    chmod 0755 $out/usr/bin/msks-console-helper

    # rsync (#110): the sync half of the TCP service plane — Debian's
    # own binary from the pinned deb (see rsyncDeb), staged into the
    # tree below with its NEEDED libraries asserted.

    # The sshd posture (#110): every login is a key login. The
    # genericcloud image ships sshd enabled with its own
    # PasswordAuthentication no; the dropin states the full contract
    # where sshd reads it first (Include is the config's opening
    # line, and first match wins). Root may log in with a key —
    # the console-planted identity of #110's tests, the msksd-minted
    # key of #111 — and never with a password.
    #
    # Authentication policy only, never algorithm policy: no cipher,
    # MAC, key-exchange, or host-key lists here, so a FIPS-restricted
    # OpenSSH (a distro crypto provider) narrows itself without
    # config surgery (#115 — identities ride the mint's setting,
    # Ed25519 by default, #138, and ssh-keygen -A's rsa/ecdsa host
    # keys are FIPS-approvable).
    printf '%s\n' \
      '# msks (#110): key-only login; the forward is the road in.' \
      'PasswordAuthentication no' \
      'KbdInteractiveAuthentication no' \
      'PermitRootLogin prohibit-password' \
      > $out/etc/ssh/sshd_config.d/00-msks.conf

    # The workspace user's sudo (#169): passwordless root for the
    # msks user — the single-user dev VM's standard cloud posture
    # (Debian's own default cloud user carries the same grant). The
    # password is locked by design (the console helper and ssh keys
    # are the road in), so NOPASSWD is the only form that can ever
    # run. The pack stage sets the 0440 sudoers mode: the store
    # rewrites the built file's group bits (0440 lands 0444), and
    # the tar hop's chmod -R u+w would widen it again before
    # mke2fs packs the tree.
    printf '%s\n' \
      '# msks (#169): the workspace user administers this VM.' \
      'msks ALL=(ALL) NOPASSWD:ALL' \
      > $out/etc/sudoers.d/msks

    # Host keys come from the image's own sshd-keygen.service (wanted
    # by ssh.service, ConditionFirstBoot): ssh-keygen -A writes them
    # into /etc/ssh on the root overlay, so they survive stop/start
    # with the overlay (#14) and each workspace owns its own keys —
    # nothing here to stage.

    # sshd waits for the interface to have its address (#110): the
    # forward path dials the guest's tap address, and the ordering
    # puts listening behind DHCP instead of ahead of it. A scoped
    # oneshot, NOT systemd's wait-online: this guest boots with no
    # NIC at all in the no-egress posture, and a link-less networkd
    # never reaches "online" — wait-online would stall those boots
    # at network-online.target. The NIC check lives in ExecCondition
    # (systemd path conditions do not glob — a literal e* path never
    # exists): with no NIC beyond lo the unit is skipped and sshd
    # listens immediately (nothing can reach it anyway); the 15s
    # ceiling never blocks the port beyond a slow DHCP.
    cat > $out/etc/systemd/system/msks-wait-address.service <<'WAITUNIT'
    [Unit]
    Description=msks: sshd listens once the NIC has its address
    After=systemd-networkd.service
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
    # network rendering stays off — the overlay's networkd unit owns
    # whatever NIC appears, taking its address from the daemon's own
    # DHCP (#52), so cloud-init must not fight it with netplan.
    printf '%s\n' \
      '# msks: the cidata seed disk is the only datasource.' \
      'datasource_list: [ NoCloud, None ]' \
      > $out/etc/cloud/cloud.cfg.d/99-msks-datasources.cfg
    printf '%s\n' \
      '# msks: networkd (see 80-msks-egress.network) owns the NIC.' \
      'network: {config: disabled}' \
      > $out/etc/cloud/cloud.cfg.d/99-msks-network.cfg
    # cloud-init creates no accounts (#171): the image ships the
    # msks workspace user (#63), and the identity seed makes its
    # home. The genericcloud image's own default account — the
    # 'debian' user with /home/debian and a passwordless-sudo
    # sudoers entry — never comes into being. The dropin must keep
    # lexicographically sorting at (or after) the tail of
    # cloud.cfg.d: cloud-init merges the directory with the
    # first-defined `users` winning, so a later-sorted dropin
    # defining users would take precedence.
    printf '%s\n' \
      '# msks (#171): the image ships the msks user (#63);' \
      '# cloud-init creates no accounts and the identity seed' \
      '# makes the home.' \
      'users: []' \
      > $out/etc/cloud/cloud.cfg.d/99-msks-users.cfg

    # The image's fstab mounts the root filesystem by the PARTUUID of
    # the cloud image's partition table; direct kernel boot presents
    # a bare ext4, so those device jobs can never start and systemd
    # stalls at boot. The kernel cmdline already names the root
    # device; systemd mounts the pseudo-filesystems itself.
    #
    # /home is the workspace's second persistent disk (#14): the
    # host-side ext4 volume, labeled msks-home at mkfs time and
    # attached as a second virtio-blk disk. Mounting by label (not
    # /dev/vdb) keeps /home on the right device even if the disk
    # order ever shifts. nofail plus a device timeout keeps a boot
    # without the volume (a demo VM, a pre-#14 image) moving instead
    # of stalling the default 90s.
    #
    # The timeout must survive a slow udev coldplug: the device job
    # for /dev/disk/by-label/msks-home is enqueued at sysinit start,
    # before udevd itself runs, and udevd must probe the volume for
    # its label before the clock expires — else home.mount fails for
    # the whole boot (nofail keeps the boot moving; it never retries
    # the mount). A first boot from a fresh overlay is the slow
    # case: every root read is a copy-on-write miss against the
    # backing image, and module loading and journal writes compete
    # with the coldplug for the same I/O. 30s covers it; a boot
    # without the volume pays the same 30s once, in parallel with
    # the rest of boot, and continues.
    printf '%s\n' \
      '# msks: root comes from the kernel cmdline; no swap.' \
      '# /home is the second persistent disk (#14), labeled msks-home.' \
      'LABEL=msks-home /home ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2' \
      > $out/etc/fstab

    printf '%s\n' \
      '# The vsock console transport: the module name on Debian' \
      '# is vmw_vsock_virtio_transport (#21).' \
      'vmw_vsock_virtio_transport' \
      > $out/etc/modules-load.d/msks-vsock.conf

    # Egress networking (#52): a workspace created with egress boots
    # with a virtio-net NIC; everything else presents none. The
    # module loads at boot either way (udev would autoload it on
    # device discovery too), so networkd never waits on a cold probe.
    printf '%s\n' \
      '# The virtio-net driver for egress NICs (#52).' \
      'virtio_net' \
      > $out/etc/modules-load.d/msks-net.conf

    # The L3 recursion stack (#82): a workspace running msksd needs
    # the same kernel modules the appliance loads — tun for the
    # per-inner-workspace taps, the nftables/NAT set the daemon's
    # rulesets name — so the image can be an appliance in miniature.
    # KVM does NOT ride this file: which flavor loads depends on the
    # host CPU, and a modules-load.d entry that fails leaves
    # systemd-modules-load.service failed (a degraded boot) — the
    # oneshot service below picks the flavor and swallows a host
    # without nested virt, exactly as the appliance image does.
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

    # The nested-KVM module for inner workspace VMs (#82), the
    # appliance's own unit verbatim in shape: which flavor loads
    # depends on the host CPU, so a shell picks, and a workspace
    # booted where vmx does not reach (a host without nested virt)
    # still boots — the unit stays active (exited) and /dev/kvm
    # simply never appears. udev makes the node when a module
    # registers; root (the only inner-daemon operator today) opens
    # it regardless of the kvm group's mode bits.
    printf '%s\n' \
      '[Unit]' \
      'Description=msks nested-KVM module (inner workspace VMs, #82)' \
      'After=systemd-modules-load.service' \
      ''' \
      '[Service]' \
      'Type=oneshot' \
      'ExecStart=/bin/sh -c "modprobe kvm-intel || modprobe kvm-amd || true"' \
      'RemainAfterExit=yes' \
      ''' \
      '[Install]' \
      'WantedBy=multi-user.target' \
      > $out/etc/systemd/system/msks-kvm.service
    ln -s ../msks-kvm.service \
      $out/etc/systemd/system/multi-user.target.wants/msks-kvm.service

    # The DHCP client for an egress NIC (#52): networkd takes an
    # address and the daemon's resolver over DHCP on whatever NIC
    # appears. With no NIC (a workspace without egress) nothing
    # matches the unit and networkd stays idle — the same image
    # serves both postures.
    printf '%s\n' \
      '[Match]' \
      'Name=en* eth*' \
      "" \
      '[Network]' \
      'DHCP=yes' \
      > $out/etc/systemd/network/80-msks-egress.network

    # networkd must not race udev's coldplug rename (eth0 to ens3):
    # this boot reaches multi-user immediately after sysinit, and a
    # networkd that enumerates while udevd is still renaming the NIC
    # never manages the renamed link — DHCP never runs, and
    # networkd sits in activating forever. Ordering after the
    # coldplug makes the interface name final before the first
    # enumeration; the .network above matches either name anyway.
    mkdir -p $out/etc/systemd/system/systemd-networkd.service.d
    printf '%s\n' \
      '[Unit]' \
      'After=systemd-udev-trigger.service systemd-udevd.service' \
      > $out/etc/systemd/system/systemd-networkd.service.d/10-after-udev-coldplug.conf

    # networkd + resolved stay enabled for egress workspaces (#52):
    # DHCP configures the NIC and resolved serves the offered
    # resolver at 127.0.0.53. The boot-diet lines that dropped these
    # wants symlinks are gone (they predate NICs); wait-online stays
    # dropped — nothing orders on network-online.target.
    ln -s /lib/systemd/system/systemd-networkd.service \
      $out/etc/systemd/system/multi-user.target.wants/systemd-networkd.service
    ln -s /lib/systemd/system/systemd-networkd.socket \
      $out/etc/systemd/system/sockets.target.wants/systemd-networkd.socket
    ln -s /lib/systemd/system/systemd-resolved.service \
      $out/etc/systemd/system/sysinit.target.wants/systemd-resolved.service
    # resolved's stub: the symlink may exist in the source image, but
    # an image edited to a static resolv.conf would silently ignore
    # the DHCP-offered resolver.
    rm -f $out/etc/resolv.conf
    ln -s /run/systemd/resolve/stub-resolv.conf $out/etc/resolv.conf

    # Escape the default basic.target ordering (#37): the console
    # starts as soon as the vsock module is loaded, not after the
    # whole boot. A too-early start self-heals through Restart=
    # always, and StartLimitIntervalSec=0 keeps systemd's default
    # burst limit from ending those retries.
    #
    # The helper (#63) owns the listener the socat line used to: it
    # accepts host-originated connections only, reads the identity
    # prelude (user, window size), and execs the requested user's
    # login shell on a fresh pty sized to the client's tty (#61's
    # 0x0 fix). A fresh pty slave's default termios — ECHO, ICANON,
    # ISIG, OPOST/ONLCR — is what programs that read stdin directly
    # get, and bash's readline takes over editing while it is active;
    # the helper sends TERM=xterm because systemd hands services
    # TERM=dumb, which turns readline off (#61).
    printf '%s\n' \
      '[Unit]' \
      'Description=msks vsock console (one negotiated shell per connection)' \
      'Documentation=https://github.com/mcdonc/msks' \
      'ConditionPathExists=/dev/vsock' \
      'After=systemd-modules-load.service dev-pts.mount' \
      'DefaultDependencies=no' \
      'StartLimitIntervalSec=0' \
      ''' \
      '[Service]' \
      'ExecStart=/usr/bin/msks-console-helper ${toString vsockShellPort}' \
      'Restart=always' \
      'RestartSec=0.1' \
      'StandardInput=null' \
      ''' \
      '[Install]' \
      'WantedBy=multi-user.target' \
      > $out/etc/systemd/system/msks-console.service
    ln -s ../msks-console.service \
      $out/etc/systemd/system/multi-user.target.wants/msks-console.service

    # grub-common records successful boots into /boot — harmless
    # under the #14 overlay, but a direct-boot VM has no grub to
    # inform anyway.
    ln -s /dev/null $out/etc/systemd/system/grub-common.service

    # The first-boot wizard (locale/timezone prompts) has nothing to
    # ask: the image is already provisioned, and with the root
    # writable (#14) an empty machine-id flips ConditionFirstBoot on
    # and the wizard stalls sysinit.target — no getty ever starts.
    # Masked, systemd generates each workspace's machine-id on its
    # own overlay instead.
    ln -s /dev/null $out/etc/systemd/system/systemd-firstboot.service

    # AppArmor profile loading costs ~0.7s of every boot (#37) and
    # confines nothing in a pristine workspace VM.
    ln -s /dev/null $out/etc/systemd/system/apparmor.service

    # The serial console is the guest's debug channel: autologin root
    # on ttyS0 (the vsock console is the supported interactive path).
    printf '%s\n' \
      '[Service]' \
      'ExecStart=' \
      'ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM' \
      > $out/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf

  '';

  # Parse sfdisk --json: print the byte offset of the Linux root
  # partition. A separate file (not inline) because nix ''-string
  # de-indentation would mangle a python block.
  partitionOffset = pkgs.writeText "msks-root-partition-offset.py" ''
    import json
    import sys

    table = json.load(sys.stdin)["partitiontable"]
    root = None
    for part in table["partitions"]:
        if part["type"] == "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709":
            root = part
            break
    assert root is not None, "no Linux root partition found"
    print(root["start"] * table.get("sectorsize", 512))
  '';

  # The setuid/setgid restoration data (#169): unprivileged rdump
  # drops the bits (its mode_xlate() table carries only the nine rwx
  # bits, so the chmod after extraction never sees them), and the
  # build sandbox is a user namespace where the kernel refuses to
  # set them back for real. `walk` records every special-bit inode
  # of the source image into a manifest; the fakeroot pack stage
  # runs `apply` on it — fakeroot records the chmods without
  # touching the kernel, and mke2fs -d bakes them into the image
  # alongside the faked uid-0 ownership. A separate file (not
  # inline) for the same de-indentation reason as partitionOffset
  # above.
  specialModes = pkgs.writeText "msks-special-modes.py" ''
    import os
    import subprocess
    import sys

    MARKER = "debugfs: ls -p "


    def walk(image, workdir):
        """Return [(path, perm)] for every inode whose mode carries
        setuid, setgid, or sticky, breadth-first through debugfs batch
        listings."""
        pending = ["/"]
        special = []
        while pending:
            cmds = os.path.join(workdir, "ls-cmds")
            with open(cmds, "w") as batch:
                for path in pending:
                    batch.write(f'ls -p "{path}"\n')
            proc = subprocess.run(
                ["debugfs", "-f", cmds, image],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise SystemExit(
                    f"debugfs exited {proc.returncode}: {proc.stderr}"
                )
            entries = {}
            current = None
            for line in proc.stdout.splitlines():
                if line.startswith(MARKER):
                    current = line[len(MARKER) :].strip('"')
                    entries[current] = []
                elif line.startswith("debugfs: "):
                    # A debugfs error (unbalanced quotes, lookup
                    # failure) aborts the walk — exiting silently
                    # would drop the whole subtree from the manifest,
                    # and the set could drift on an image pin update
                    # without a trace.
                    raise SystemExit(f"debugfs: {line}")
                elif line.startswith("/") and current is not None:
                    parts = line.rstrip("/").split("/")
                    if (
                        len(parts) < 6
                        or parts[0] != ""
                        or not all(p.isdigit() for p in parts[1:5])
                    ):
                        raise SystemExit(
                            f"unparsable ls -p line: {line!r}"
                        )
                    mode = int(parts[2], 8)
                    name = parts[5]
                    if name not in (".", ".."):
                        entries[current].append((name, mode))
            next_pending = []
            for path, dir_entries in entries.items():
                prefix = "" if path == "/" else path
                for name, mode in dir_entries:
                    child = prefix + "/" + name
                    if (mode & 0o170000) == 0o040000:
                        next_pending.append(child)
                    if mode & 0o7000:
                        special.append((child, mode & 0o7777))
            pending = sorted(set(next_pending))
        return special


    def do_walk(image, manifest):
        special = walk(image, os.path.dirname(os.path.abspath(image)))
        setuid = {path for path, perm in special if perm & 0o4000}
        # #169 exists because sudo broke: pin the binary itself, not
        # just "some setuid survived" — a parse regression that
        # keeps any other setuid file would otherwise pass.
        if "/usr/bin/sudo" not in setuid:
            raise SystemExit(
                "/usr/bin/sudo is not setuid in the source image; "
                "the debugfs walk parse must have broken"
            )
        with open(manifest, "w") as out:
            for path, perm in special:
                out.write(f"{perm:04o} {path}\n")
        print(
            f"recorded {len(special)} special modes "
            f"({len(setuid)} setuid)"
        )


    def do_apply(manifest, tree):
        applied = 0
        with open(manifest) as entries:
            for line in entries:
                mode_s, path = line.split(" ", 1)
                path = path.rstrip("\n")
                if not os.path.lexists(tree + path):
                    raise SystemExit(
                        f"special-mode path absent from the tree: {path}"
                    )
                os.chmod(tree + path, int(mode_s, 8))
                applied += 1
        if not applied:
            raise SystemExit("empty special-mode manifest")
        print(f"applied {applied} special modes")


    def main():
        usage = f"usage: {sys.argv[0]} walk <image> <manifest> | apply <manifest> <tree>"
        if len(sys.argv) != 4:
            raise SystemExit(usage)
        command, one, two = sys.argv[1:]
        if command == "walk":
            do_walk(one, two)
        elif command == "apply":
            do_apply(one, two)
        else:
            raise SystemExit(usage)


    if __name__ == "__main__":
        main()
  '';

  # The Debian root tree: convert the qcow2 to raw, slice the root
  # partition out (offset from the partition table, not hardcoded),
  # dump the ext4 contents with debugfs (unprivileged — no mount),
  # and lay the overlay on top. A fresh ext4 is built from the tree
  # later, so this stays a plain directory.
  debianRoot =
    pkgs.runCommand "msks-debian-root"
      {
        nativeBuildInputs = [
          pkgs.binutils # readelf: the rsync NEEDED-soname guard below
          pkgs.dpkg # dpkg-deb: unpack the pinned rsync deb
          pkgs.gnutar
          pkgs.qemu
          pkgs.e2fsprogs
          pkgs.kmod
          pkgs.util-linux
          (pkgs.python3.withPackages (ps: [ ]))
        ];
      }
      ''
        set -eu
        # The tree builds in the derivation's scratch cwd (discarded
        # with it); the retained output is a single opaque root.tar.
        root=root-tree
        mkdir -p "$root"

        # qcow2 -> raw
        qemu-img convert -O raw ${debianImage} debian.raw

        # Slice the root partition: the GPT partition labeled/type
        # "Linux filesystem" (the cloud image keeps EFI + BIOS grub
        # partitions around it, which direct kernel boot does not
        # need).
        offset=$(sfdisk --json debian.raw | python3 ${partitionOffset})
        dd if=debian.raw of=root.part bs=512 skip=$((offset / 512)) status=none

        # ext4 -> tree (ownership errors are expected unprivileged: the
        # files land owned by the build user, and the setuid/setgid
        # bits drop — recorded right below for the pack stage to
        # apply).
        debugfs -R "rdump / $root" root.part 2>/dev/null || true
        rm -rf "$root"/lost+found
        # rdump's stderr mixes benign ownership noise with real errors,
        # so the exit code is useless; assert the dump itself landed.
        for top in bin usr etc var lib boot; do
          test -d "$root/$top"
        done

        # Record the setuid/setgid set rdump dropped (#169): the
        # source image's own inode modes are the authority — sudo,
        # su, mount and the rest come back as the distro ships
        # them, and an image pin update cannot drift the set. The
        # bits cannot be set back on this tree (the build sandbox
        # is a user namespace; the kernel refuses), so they ride a
        # manifest to the fakeroot pack stage, which applies them
        # alongside the faked uid-0 ownership.
        mkdir -p "$out"
        python3 ${specialModes} walk root.part "$out"/special-modes

        # The msks overlay.
        cp -a --no-preserve=ownership ${guestOverlay}/. "$root"/

        # The console workspace user (#63): uid/gid 1000, locked
        # password (no sign-in — the console helper is the only way
        # in), home on the persistent /home volume (#14) — the
        # identity seed creates it from /etc/skel on first boot
        # (#171), and the console helper creates a bare one if a
        # session ever precedes the seed — and bash as the shell.
        # cloud-init's own account creation stays off (the
        # 99-msks-users.cfg dropin), so this entry is the whole
        # account list the guest ever gets beyond the image's
        # system users.
        grep -q '^msks:' "$root"/etc/passwd || printf '%s\n' \
          'msks:x:1000:1000:msks workspace user:/home/msks:/bin/bash' \
          >> "$root"/etc/passwd
        grep -q '^msks:' "$root"/etc/shadow || printf '%s\n' \
          'msks:!:19700:0:99999:7:::' \
          >> "$root"/etc/shadow
        grep -q '^msks:' "$root"/etc/group || printf '%s\n' \
          'msks:x:1000:' \
          >> "$root"/etc/group
        grep -q '^msks:' "$root"/etc/gshadow || printf '%s\n' \
          'msks:!::' \
          >> "$root"/etc/gshadow

        # The generic kernel's module tree (#96): the guest's runtime
        # needs are the modprobe closure of the modules it loads —
        # vmw_vsock_virtio_transport (the vsock console, #21),
        # virtio_net (egress NICs, #52), virtio_blk (udev alias
        # probing; the initrd loads it before root anyway), the
        # ACPI power-button pair (button + evdev: logind answers the
        # host-side graceful shutdown with a clean poweroff, #25),
        # isofs (the #41 NoCloud seed disk is iso9660),
        # crc32c-intel (the hardware crc32c ext4's metadata_csum
        # asks the crypto API for; udev autoloads it via its
        # x86cpu modalias), and the L3 recursion set (#82): the
        # nested-KVM trio (kvm-intel/kvm-amd; a workspace running
        # msksd boots inner workspace VMs through /dev/kvm — kvm,
        # irqbypass, and ccp ride in as their dependencies) and the
        # egress stack (tun for per-inner-workspace taps plus the
        # nftables/NAT modules the daemon's rulesets need — the
        # same list the appliance image loads, and what makes the
        # image able to BE an appliance in miniature). button,
        # evdev, and isofs are modules in BOTH Debian flavors — the
        # old cloud image found them only because it shipped
        # Debian's whole tree, and the smoke tests caught button and
        # isofs missing when this closure first shipped without
        # them. The rest of the x86cpu set (aesni_intel and
        # friends) stays out by design: userspace crypto uses its
        # own CPU-feature code, and only kernel-side consumers miss
        # the modules. The closure is resolved mechanically with
        # modprobe --show-depends over depmod metadata generated
        # from the pinned deb and pinned at build time by comparing
        # the full tree's closure against the shipped tree's (the
        # assert below) — the set cannot drift from the kernel's
        # own dependency facts (the #36 bug class). The appliance
        # ships the whole tree; a workspace's ~25MB of cloud
        # modules becomes twenty-nine files (isofs's own cdrom
        # dependency included).
        runtimeModules="
          vmw_vsock_virtio_transport
          virtio_net
          virtio_blk
          button
          evdev
          isofs
          crc32c-intel
          kvm-intel
          kvm-amd
          tun
          nf_tables
          nft_chain_nat
          nft_masq
          nft_ct
          nf_nat
          nf_conntrack
        "
        rm -rf "$root"/lib/modules/*
        rm -rf "$root"/usr/lib/modules/* 2>/dev/null || true
        rm -f "$root"/boot/vmlinuz-* "$root"/boot/initrd.img-* \
          "$root"/boot/System.map-* "$root"/boot/config-*
        modsrc=modprobe-base
        mkdir -p "$modsrc"/usr/lib/modules
        cp -a --no-preserve=ownership \
          "${genericKernel}"/usr/lib/modules/. "$modsrc"/usr/lib/modules/
        # kmod looks under <root>/lib/modules; the deb's own usrmerge
        # layout carries lib -> usr/lib as a symlink.
        ln -s usr/lib "$modsrc"/lib
        kver=$(ls "$modsrc"/usr/lib/modules | head -1)
        # The deb ships no depmod metadata (its postinst generates it
        # on the target); --show-depends needs it.
        find "$modsrc"/usr/lib/modules -type d -exec chmod u+w {} +
        depmod -b "$modsrc" "$kver"
        mkdir -p "$root"/usr/lib/modules/"$kver"
        for mod in $runtimeModules; do
          modprobe -d "$modsrc" -S "$kver" --show-depends "$mod" \
            | awk '/^insmod /{print $2}'
        done | sort -u | while read -r ko; do
          rel=''${ko#*modules/"$kver"/}
          mkdir -p "$root"/usr/lib/modules/"$kver"/"$(dirname "$rel")"
          cp "$modsrc"/usr/lib/modules/"$kver"/"$rel" \
            "$root"/usr/lib/modules/"$kver"/"$rel"
        done
        # modprobe reads modules.builtin to skip built-ins (virtio-pci
        # and virtiofs are built into the generic kernel): without
        # the file, every builtin alias resolves to a missing module.
        cp "$modsrc"/usr/lib/modules/"$kver"/modules.builtin \
          "$modsrc"/usr/lib/modules/"$kver"/modules.builtin.modinfo \
          "$modsrc"/usr/lib/modules/"$kver"/modules.order \
          "$root"/usr/lib/modules/"$kver"/
        cp "${genericKernel}"/boot/config-* "$root"/boot/

        # Runtime depmod over the shipped subset: modprobe — the
        # vsock console's module load, udev alias lookups — must
        # resolve within the tree the image actually carries.
        find "$root"/usr/lib/modules -type d -exec chmod u+w {} +
        depmod -b "$root" "$kver"
        test -s "$root"/usr/lib/modules/"$kver"/modules.dep
        # modprobe --show-depends exits 0 even when a dependency is
        # missing (it prints only what it found), so per-module
        # probes prove little: the guard compares the module-name
        # set the FULL tree resolves against the set the SHIPPED
        # tree resolves — a closure member lost from the image
        # fails the build, not the boot.
        full_closure=$(for mod in $runtimeModules; do
          modprobe -d "$modsrc" -S "$kver" --show-depends "$mod" \
            | awk '/^insmod /{print $2}'
        done | xargs -n1 basename | sort -u)
        tree_closure=$(for mod in $runtimeModules; do
          modprobe -d "$root" -S "$kver" --show-depends "$mod" \
            | awk '/^insmod /{print $2}'
        done | xargs -n1 basename | sort -u)
        if [ "$full_closure" != "$tree_closure" ]; then
          echo "guest module tree closure mismatch:" >&2
          echo "full tree resolves: $full_closure" >&2
          echo "shipped tree resolves: $tree_closure" >&2
          exit 1
        fi

        # Boot diet (#37): drop the wants symlinks of units a
        # workspace never uses. networkd and resolved stay (egress
        # workspaces get a NIC, #52; the overlay enables both);
        # timesyncd has no served clock until a resolver exists,
        # unattended-upgrades no repo to reach, e2scrub_reap no LVM
        # to reap. wait-online stays dropped: a link-less networkd
        # (the no-egress posture) never reaches "online", so sshd's
        # address ordering (#110) rides its own scoped oneshot
        # (msks-wait-address.service) instead of this target.
        wants="$root"/etc/systemd/system
        # The image ships some wants directories read-only; the build
        # owns them now.
        chmod u+w "$wants"/*.target.wants "$wants"/*.target.requires 2>/dev/null || true
        rm -f "$wants"/multi-user.target.wants/unattended-upgrades.service
        rm -f "$wants"/multi-user.target.wants/e2scrub_reap.service
        rm -f "$wants"/sysinit.target.wants/systemd-timesyncd.service
        rm -f "$wants"/network-online.target.wants/systemd-networkd-wait-online.service
        # The netplan renderer config is replaced by the overlay's own
        # .network unit (#52); the generator would only shadow it.
        chmod u+w "$root"/etc
        chmod -R u+w "$root"/etc/netplan
        rm -rf "$root"/etc/netplan

        # rsync (#110): Debian's own binary from the pinned deb —
        # the tree's libraries are its build-time world, and the
        # guards below fail the build the day that stops being true:
        # every NEEDED soname present, the ELF interpreter resolvable,
        # and every version symbol the binary requires (GLIBC_*,
        # OPENSSL_*) defined by the tree's copy of that library — a
        # deb rebuilt against newer symbols than the image ships is
        # the #36 bug class. (u+w: the overlay cp carries the store's
        # read-only dir modes.) Only usr/bin/rsync is staged: nothing
        # in the runtime needs the deb's rrsync/rsync-ssl or its
        # /usr/share scripts — an operator wanting rrsync's scoped
        # syncs installs it in the workspace itself.
        chmod u+w "$root"/usr/bin
        mkdir -p rsync-deb
        dpkg-deb -x ${rsyncDeb} rsync-deb
        cp --no-preserve=ownership rsync-deb/usr/bin/rsync \
          "$root"/usr/bin/rsync
        for so in $(readelf -d "$root"/usr/bin/rsync \
          | awk '/NEEDED/{gsub(/\[\]/,"",$NF); print $NF}'); do
          test -e "$root"/usr/lib/x86_64-linux-gnu/"$so" \
            || { echo "rsync needs $so, absent from the tree" >&2; exit 1; }
        done
        interp=$(readelf -l "$root"/usr/bin/rsync \
          | awk '/interpreter/{gsub(/[\[\]]/,"",$NF); print $NF}')
        test -e "$root""$interp" \
          || { echo "rsync loader $interp absent from the tree" >&2; exit 1; }
        reqs=$(readelf --version-info "$root"/usr/bin/rsync \
          | awk '/File: /{f=$5} /Name: /{print f, $3}')
        while read -r so ver; do
          [ -n "$so" ] || continue
          lib="$root"/usr/lib/x86_64-linux-gnu/"$so"
          readelf --version-info "$lib" | grep -q "Name: $ver" \
            || { echo "rsync needs $ver from $so; the tree's copy is older" \
                 >&2; exit 1; }
        done <<<"$reqs"

        # No baked host keys — ever (#110): each workspace generates
        # its own on first boot; an upstream image that started
        # shipping some would give every workspace the same keys.
        ! ls "$root"/etc/ssh/ssh_host_* >/dev/null 2>&1

        # Sanity: this must be a bootable Debian.
        test -x "$root"/sbin/init
        test -x "$root"/usr/bin/socat
        test -x "$root"/usr/bin/msks-console-helper
        test -x "$root"/usr/bin/rsync
        test -x "$root"/usr/sbin/sshd
        # The dropin's load-bearing line, not just the file's
        # existence: a typo'd printf must fail the build here, not
        # in the opt-in smoke.
        grep -q '^users: \[\]' \
          "$root"/etc/cloud/cloud.cfg.d/99-msks-users.cfg
        # The image's own trees carry no home for the workspace user
        # (the seed makes it on the persistent volume) and none for
        # a cloud-image default account either (#171).
        ! test -e "$root"/home/msks
        ! test -e "$root"/home/debian

        # Size the final image from the tree (content-derived, no
        # magic constant): Debian unpacks to ~600M plus headroom.
        mkdir -p "$out"
        du -s --apparent-size --block-size=4096 "$root" | cut -f1 > "$out"/tree-blocks
        # The intermediate rides the store as ONE opaque blob, never
        # as a tree: the store's auto-optimise hardlinks identical
        # files inside tree-shaped paths (the empty files form one
        # group of tens of thousands), a store path's contract covers
        # readable bytes, not inode identity — and mke2fs -d packs
        # hardlink groups as one inode, which once made the guest's
        # utmp writes surface in cloud-init's empty __init__.py. A
        # tarball cannot be deduped from inside; rdump flattens all
        # hardlinks anyway, so none are recorded. Sorted member order
        # keeps the tarball itself deterministic; the mtimes inside it
        # are NOT (depmod's outputs are build-time), and need not be —
        # byte-stability of the final image comes from
        # E2FSPROGS_FAKE_TIME plus the archive tar's --mtime=@1, not
        # from this hop.
        tar --sort=name --owner=0 --group=0 --numeric-owner \
          -C "$root" -cf "$out/root.tar" .
      '';

  # mke2fs -d packs a directory into an ext4 image without mounting
  # anything — the whole build stays unprivileged and host-independent.
  # One fakeroot session owns the tree and builds the image: the
  # extraction tree is owned by the build user (mke2fs -d bakes the
  # builder's ownership view straight into the image — every inode
  # would be nobody:nogroup). Under fakeroot the chown/chmod are
  # recorded, not performed, and mke2fs -d's stat() reads the faked
  # root ownership. This also restores sane permissions on the
  # password files and the sudoers dropin's 0440 (the tar hop's
  # u+w pass had widened both), and applies debianRoot's
  # special-mode manifest (#169): the faked chmods put sudo and its
  # setuid kin back as uid-0 inodes, the pairing sudo's elevation
  # depends on.
  packScript = pkgs.writeText "msks-rootfs-pack.sh" ''
    set -eu
    tree="''${PACK_TREE:?}"
    img="''${PACK_IMG:?}"
    blocks="''${PACK_BLOCKS:?}"
    fake_epoch="''${PACK_FAKE_EPOCH:?}"
    modes="''${PACK_MODES:?}"
    applier="''${PACK_APPLIER:?}"
    chown -R 0:0 "$tree"
    chmod 0640 "$tree"/etc/shadow "$tree"/etc/gshadow
    chmod 0440 "$tree"/etc/sudoers.d/msks
    python3 "$applier" apply "$modes" "$tree"
    E2FSPROGS_FAKE_TIME="$fake_epoch" mke2fs -q -t ext4 -b 4096 -I 256 \
      -L msks-rootfs \
      -E hash_seed=00000000-0000-0000-0000-000000000000 \
      -d "$tree" "$img" "$blocks"
    E2FSPROGS_FAKE_TIME="$fake_epoch" tune2fs -U clear "$img" >/dev/null
  '';

  rootfs =
    pkgs.runCommand "msks-guest-rootfs"
      {
        inherit debianRoot packScript specialModes;
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
        # The tree arrives as one opaque tarball (see debianRoot):
        # every path untars to its own inode — rdump flattened all
        # hardlinks at extraction, and the store cannot dedup inside
        # a blob — so mke2fs -d can never pack a fabricated shared
        # inode for the guest's runtime writes to collide in.
        mkdir work
        tar -C work -xf "$debianRoot/root.tar"
        # Extraction restores the recorded modes but cannot chown
        # (unprivileged: files land build-user owned, which is why
        # u+w works); fakeroot's faked chown/chmod below need the
        # modes writable first.
        chmod -R u+w work
        # Content plus 1G of slack: the base keeps room for image
        # updates, and the per-workspace overlay (#14) carries whatever
        # the guest writes beyond it.
        PACK_TREE=work \
          PACK_IMG="$out/rootfs.ext4" \
          PACK_BLOCKS=$(( $(cat "$debianRoot"/tree-blocks) + 262144 )) \
          PACK_FAKE_EPOCH="$fakeEpoch" \
          PACK_MODES="$debianRoot/special-modes" \
          PACK_APPLIER="${specialModes}" \
          fakeroot -- /bin/sh -e "$packScript"
      '';

  # The canonical image artifact (#40): a container-image tar
  # (`podman load` compatible) in the containerDisk convention — one
  # layer carrying boot/ (kernel, initrd) and disk/ (rootfs.ext4,
  # image.json schema 2). Importable with podman/skopeo/plain tar,
  # and consumable as a containerDisk by the k8s backend later
  # (#15).
  bootTree =
    pkgs.runCommand "msks-image-boot-tree"
      {
        inherit
          debianRoot
          rootfs
          genericKernel
          minimalInitrd
          ;
        inherit
          imageName
          imageVersion
          kernelCmdline
          vsockShellPort
          ;
      }
      ''
        set -eu
        vmlinuz=$(ls "$genericKernel"/boot/vmlinuz-*)
        initrd="${minimalInitrd}/initrd"
        mkdir -p "$out"/boot "$out"/disk
        cp "$vmlinuz" "$out"/boot/vmlinuz
        cp "$initrd" "$out"/boot/initrd.img
        cp "${rootfs}/rootfs.ext4" "$out"/disk/rootfs.ext4
        kernel_version=$(basename "$vmlinuz" | sed 's/^vmlinuz-//')
        # Guard against version drift: the catalog label must match the
        # Debian tree this image actually wraps.
        shipped=$(tar -xOf "$debianRoot/root.tar" ./etc/debian_version)
        if [ "$shipped" != "${imageVersion}" ]; then
          echo "imageVersion ${imageVersion} != /etc/debian_version $shipped" >&2
          exit 1
        fi
        # Self-describing (#40 review): the archive alone builds a boot
        # spec — no sidecar metadata for foreign imports to miss.
        cat > "$out"/disk/image.json <<EOF
        {
          "schema": 2,
          "name": "${imageName}",
          "version": "${imageVersion}",
          "cmdline": "${kernelCmdline}",
          "vsock_shell_port": ${toString vsockShellPort},
          "console_protocol": "prelude-v1",
          "console_users": ["root", "msks"],
          "kernel_version": "$kernel_version",
          "kernel_format": "bzImage",
          "capabilities": {"provisioner": "${imageProvisioner}"}
        }
        EOF
      '';

  # The image archive: a container-image tar built with plain tar
  # instead of dockerTools (#40 review). The layout is the one
  # `podman save` writes (manifest.json +
  # <id>/{layer.tar,json,VERSION} + repositories; the format
  # originates with `docker save`, which is the last time docker is
  # mentioned here). The layer is UNCOMPRESSED (members readable in
  # place with `tar tf`, no decompression at import) and byte-stable
  # (--sort=name --mtime=@1 --owner=0 --group=0 --numeric-owner), so
  # identical rebuilds hash identically and the per-hash cache
  # dedupes across hosts and CI.
  imageArchive =
    pkgs.runCommand "msks-image-archive"
      {
        inherit bootTree imageName imageVersion;
        nativeBuildInputs = [ pkgs.gnutar ];
        imageId =
          "msks" + builtins.hashString "sha256" (imageName + ":" + imageVersion);
      }
      ''
        set -eu
        mkdir work
        # The layer: the containerDisk tree, uncompressed, sorted,
        # zeroed timestamps and ownership.
        tar --sort=name --mtime='@1' --owner=0 --group=0 --numeric-owner \
          -C "${bootTree}" -cf work/layer.tar .
        # Container-image bookkeeping.
        mkdir "work/$imageId"
        mv work/layer.tar "work/$imageId/layer.tar"
        printf '1.0' > "work/$imageId/VERSION"
        # A minimally valid image config: podman requires the rootfs
        # diff_ids (the uncompressed layer's digest).
        layer_digest=$(sha256sum "work/$imageId/layer.tar" | cut -d' ' -f1)
        printf '%s' \
          '{"architecture":"amd64","os":"linux","config":{},' \
          '"rootfs":{"type":"layers","diff_ids":["sha256:'"$layer_digest"'"]}}' \
          > "work/$imageId/json"
        # Unquoted heredocs: the env-provided name/version/imageId
        # expand in the shell.
        cat > work/manifest.json <<EOF
        [{"Config":"$imageId/json","RepoTags":["workspace-''${imageName}:''${imageVersion}"],"Layers":["$imageId/layer.tar"]}]
        EOF
        cat > work/repositories <<EOF
        {"workspace-''${imageName}":{"''${imageVersion}":"$imageId"}}
        EOF
        tar --sort=name --mtime='@1' --owner=0 --group=0 --numeric-owner \
          -C work -cf "$out" manifest.json repositories "$imageId"
      '';

in
pkgs.runCommand "msks-guest"
  {
    inherit
      debianRoot
      rootfs
      imageArchive
      imageName
      imageVersion
      ;
    passthru = {
      inherit
        debianImage
        genericKernel
        imageArchive
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
    # The generic kernel (#96 — the appliance's pin) plus the
    # minimal initramfs; the version string names the flavor the
    # guest actually boots.
    vmlinuz=$(ls "${genericKernel}"/boot/vmlinuz-*)
    version=$(basename "$vmlinuz" | sed 's/^vmlinuz-//')
    cp "$vmlinuz" "$out/vmlinux"
    cp "${minimalInitrd}/initrd" "$out/initrd"
    cp "${rootfs}/rootfs.ext4" "$out/rootfs.ext4"
    # The canonical artifact: named by name-version, OCI layout inside.
    cp "${imageArchive}" "$out/workspace-''${imageName}-''${imageVersion}.tar"
    printf '%s' "workspace-''${imageName}-''${imageVersion}.tar" > "$out"/image-archive-name
    printf '%s' "$version" > "$out"/kernel-version
    # An unquoted heredoc: $version expands in the shell; the
    # cmdline and port were interpolated by nix at eval time.
    cat > "$out"/guest-manifest.json <<EOF
    {
      "schema": 1,
      "kernel_version": "$version",
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
