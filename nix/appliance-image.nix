# The msksd appliance image (#10), on a Debian 13 trixie base (#92).
#
# Built the same way as the workspace guest (nix/guest-assets.nix):
# Debian's official genericcloud cloud image (the SAME dated pin the
# guest fetches), Debian's generic kernel, direct kernel boot, an ext4
# rootfs. What replaced the hand-rolled busybox init is systemd: real
# service supervision, journald persisting to the state disk, networkd
# — and the appliance's own units (msksd.service and friends) instead
# of a growing shell script.
#
#   $out/vmlinux            - Debian's generic kernel (bzImage; the
#                             kernel carries CONFIG_PVH=y)
#   $out/initrd             - busybox + the six modules the generic
#                             kernel needs to mount the ext4 root
#                             read-only; switch_root to systemd
#   $out/rootfs.ext4        - the appliance OS: Debian trixie + the
#                             msks systemd units, read-only at runtime
#   $out/state.ext4         - a blank persistent-state disk template
#                             (the up-task copies it once per install)
#   $out/appliance-manifest.json - store paths the host must realize
#                             (msksd closure, cloud-hypervisor, the
#                             default workspace image) + the network
#                             plan networkd follows
#
# The heavy runtime (msksd's python closure, cloud-hypervisor for
# workspace VMs) is NOT copied into the image: at boot a systemd
# mount unit mounts the host's /nix/store read-only over virtiofs
# (tag=store), so the appliance runs the same store paths the host
# built — and workspace guest assets built by msks-build-guest flow
# in with zero copying. The manifest keeps those paths alive on the
# host via GC roots. The kernel is Debian's generic flavor: virtiofs
# and virtio-pci are built in, and the KVM (nested) and nftables
# modules live in its tree, so the distro owns module wrangling
# (#36's class of bugs).
#
# The base is genericcloud (cloud-init included) with cloud-init
# DISABLED: the appliance's config channel is the kernel-cmdline
# msksd.<name>=<value> bridge, which survives state-disk recreation
# and unclean shutdowns — a NoCloud seed could not beat that, so the
# units the seed would provision ship in the image directly.
{
  lib,
  pkgs,
}:

let
  # The daemon closure, built from this repo by nixpkgs' python
  # machinery (nix/msks-pkg.nix). Interpolated into the boot script
  # and the manifest below: the path resolves inside the appliance
  # through the virtiofs store share, and the manifest reference
  # keeps it realized on the host.
  msks = pkgs.python314.pkgs.callPackage ./msks-pkg.nix { };

  # The workspace guest build: the source of the Debian image pin (the
  # same base — one fetch, one hash, deduped by the store) and of the
  # default workspace image.
  guest = pkgs.callPackage ./guest-assets.nix { };

  # Debian's official trixie genericcloud image, pinned by dated URL +
  # hash in guest-assets.nix (#30, #41).
  inherit (guest) debianImage;

  # The inode-metadata walker (#169, #179): the appliance extracts
  # the same base image the same unprivileged way, so its tree
  # records and restores the same manifest — the appliance's root
  # is read-only and dpkg never runs there, but the image stays
  # faithful to the base instead of silently flattening ownership
  # (a future writable-root debugging session gets a stock Debian).
  inherit (guest) inodeMeta;

  # Debian's GENERIC kernel flavor, the same pin the workspace guest
  # boots (#96 — the pin and its comments live in guest-assets.nix,
  # one deb fetch serves both images). The generic flavor carries
  # what the appliance probes — kvm-intel/kvm-amd for nested KVM,
  # the nftables/egress stack, the NIC driver — with virtio-pci,
  # virtiofs, and fuse BUILT IN and ext4 as a module, so the
  # initramfs below carries six modules, and the kernel is pinned
  # by its own pool URL and sha256.
  inherit (guest) genericKernel;

  # The default workspace image (#40): the containerDisk archive from
  # the guest build, GC-rooted by this manifest and imported into the
  # appliance's catalog on first boot through the cmdline bridge
  # (msksd.default_image=...). The appliance is self-contained: a
  # bare workspace create works with nothing else built.
  defaultImage = "${guest.imageArchive}";

  # The VMM for workspace VMs booted INSIDE the appliance: same
  # cloud-hypervisor the devenv shell pins.
  vmm = pkgs.cloud-hypervisor;

  # Workspace-artifact tools (#14): msksd builds each workspace's
  # qcow2 overlay (qemu-img) and formats its ext4 home volume
  # (mkfs.ext4) inside the appliance.
  qemuImg = pkgs.qemu-utils;
  e2fsprogs = pkgs.e2fsprogs;
  # The #41 seed disk builder (iso9660, unprivileged). cdrtools'
  # mkisofs is genisoimage.
  mkisofs = pkgs.cdrtools;

  # Egress plumbing (#52): the full `ip` and nftables for the
  # per-VM chains and NAT.
  iproute2 = pkgs.iproute2;
  nftables = pkgs.nftables;

  # Network plan (the up-task mirrors it on the host bridge):
  # networkd applies it inside the appliance.
  net = {
    address = "192.168.77.2";
    prefixLength = 24;
    gateway = "192.168.77.1";
  };

  # net.ifnames=0 keeps the NIC on its kernel name (eth0): the
  # daemon's default uplink (MSKSD_EGRESS_UPLINK) and the networkd
  # match below both speak eth0, and full udev in the trixie base
  # would otherwise rename it to an ens3-style name the nftables
  # rules never see — silently breaking forwarded egress (the DHCP
  # and DNS markers still pass without the forward chain; the guest
  # hits exactly this rename — #36).
  kernelCmdline = "console=ttyS0 root=/dev/vda rootfstype=ext4 ro net.ifnames=0";

  # The minimal initramfs, the guest's recipe (#37, #96: one recipe,
  # one pin) with the root mounted READ-ONLY: virtio-pci
  # is built in, but ext4 and virtio_blk are modules here, ext4 pulls
  # in crc16, crc32c (a mount-time crypto request, not a modules.dep
  # edge), jbd2 and mbcache — and busybox insmod resolves no
  # dependencies, so the init loads all six in dependency order.
  minimalInitrd =
    pkgs.runCommand "msks-appliance-initrd"
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
        # .ko.xz: busybox insmod reads plain modules only. The list is
        # dependency order: ext4's direct deps (crc16, mbcache, jbd2),
        # then ext4 itself, then virtio_blk (independent). crc32c is
        # not a modules.dep edge but a mount-time crypto request —
        # the generic kernel ships it as a module (the cloud flavor
        # builds it in) and a metadata_csum rootfs cannot mount
        # without it; the guest's initrd has loaded the same set
        # since the flavor unification (#96).
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
        # Mount the read-only root and hand off to systemd (#92). On
        # any failure, a shell beats a silent hang (the serial
        # console file is reachable).
        /bin/busybox mount -t proc proc /proc \
          && /bin/busybox mount -t devtmpfs devtmpfs /dev \
          && /bin/busybox insmod /modules/crc16.ko \
          && /bin/busybox insmod /modules/crc32c_generic.ko \
          && /bin/busybox insmod /modules/mbcache.ko \
          && /bin/busybox insmod /modules/jbd2.ko \
          && /bin/busybox insmod /modules/ext4.ko \
          && /bin/busybox insmod /modules/virtio_blk.ko \
          && /bin/busybox mount -t ext4 -o ro /dev/vda /newroot \
          || exec /bin/busybox sh
        /bin/busybox mount --move /dev /newroot/dev
        exec /bin/busybox switch_root /newroot /sbin/init
        INIT
        chmod +x "$out"/tree/init
        (cd "$out"/tree && find . | cpio -o -H newc --quiet | gzip -9) \
          > "$out"/initrd
        rm -rf "$out"/tree
      '';

  # The daemon bring-up script — what the busybox init's tail did,
  # as a file msksd.service ExecStarts. Three daemon modes, in
  # order of precedence: the DEBUG escape hatch, the dev-tree daemon
  # (#144: msksd.dev_tree from the kernel cmdline names a
  # live-shared checkout; the run script sends that pair only when
  # the host set MSKS_DEV_TREE), and the default nix-built store
  # daemon. Every msksd.<name>=<value>
  # pair on the kernel cmdline becomes an MSKSD_<NAME> environment
  # variable (upper-cased; dots map to underscores); the daemon's
  # settings file is generated on the tmpfs at every boot (#46
  # precedence: env > file > defaults). Variable NAMES are echoed to
  # the serial log, never values. The tool paths the file names are
  # this build's store paths, so a file that survived a rebuild
  # would point at dead paths — and a file on tmpfs is never
  # mistaken for operator config.
  msksBoot = pkgs.writeTextFile {
    name = "msks-boot";
    executable = true;
    destination = "/usr/local/sbin/msks-boot";
    text = ''
      #!/bin/sh
      export PATH="${iproute2}/sbin:${nftables}/sbin:${qemuImg}/bin:${e2fsprogs}/sbin:${vmm}/bin:${msks}/bin:$PATH"

      # The kernel-cmdline settings bridge: the host controls daemon
      # overrides (the bootstrap token; the console bring-up wait on
      # slow nested-virt hosts) without an image rebuild.
      cmdline_names=""
      for pair in $(cat /proc/cmdline); do
        case "$pair" in
          msksd.*=*)
            key="''${pair#msksd.}"
            key="''${key%%=*}"
            var="$(printf '%s' "$key" | tr 'a-z.' 'A-Z_')"
            export MSKSD_"$var"="''${pair#*=}"
            cmdline_names="$cmdline_names $var"
            ;;
        esac
      done
      echo "msks appliance: cmdline env:$cmdline_names"

      # The daemon's settings file (#46), generated at every boot:
      # keys are the MSKSD_* variables lowercased; operator overrides
      # ride the cmdline bridge above as variables, which outrank the
      # file. The state dir is the service user's /state/msksd home
      # (created and chowned by msks-state-format.service — the
      # daemon is not root and cannot mkdir under /state; #101). The
      # heredoc body sits at column zero: an unquoted
      # delimiter expands nothing (the store paths are already
      # literal text) and the closing EOF must start a line.
      # The daemon binds (and names in its minted cert — #146) the
      # bridge address: it is the only address the guest has, and
      # the cert's SAN must match the URL clients dial or verified
      # TLS fails on hostname. Two consequences: a boot can race
      # networkd for the address (msksd is only After=networkd, not
      # waiting for it) — a lost race fails the bind and the unit's
      # 1s Restart=always recovers within a retry or two; and a
      # state disk minted before #146 (host 0.0.0.0) remints its
      # LEAF automatically on the first boot (tls.py records the
      # host beside the leaf and regenerates on a mismatch) — no
      # manual removal, and the CA fingerprint is untouched.
      cat >/run/msksd/msksd.yaml <<EOF
      state_dir: /state/msksd
      host: ${net.address}
      port: 8660
      cloud_hypervisor: ${vmm}/bin/cloud-hypervisor
      qemu_img: ${qemuImg}/bin/qemu-img
      mkfs_ext4: ${e2fsprogs}/sbin/mkfs.ext4
      mkisofs: ${mkisofs}/bin/mkisofs
      egress_enabled: true
      ip_tool: ${iproute2}/sbin/ip
      nft_tool: ${nftables}/sbin/nft
      EOF

      echo "msks appliance: kernel $(uname -r) up; execing msksd"
      echo "msks appliance: serving https://${net.address}:8660 (TOFU fingerprint on the serial log)"

      # The identity evidence (#101): the serial log and the journal
      # record which uid — and which capability sets — the daemon,
      # and through ambient inheritance every tool and workspace VMM
      # it execs, runs with. 0x1400 is exactly CAP_NET_BIND_SERVICE
      # (10) + CAP_NET_ADMIN (12).
      echo "msks appliance: daemon identity: $(id)"
      grep -E '^Cap(Prm|Eff|Amb):' /proc/self/status \
        | sed 's/^/msks appliance: daemon /'

      # Debug escape hatch: a /state/debug-shell marker (seeded onto
      # the state disk from the host) backgrounds the daemon, puts the
      # diagnostics on the serial log, and HOLDS the unit open — the
      # console is file-mode serial (an interactive shell needs the
      # serial switched to Pty mode in scripts/appliance-run.sh), and
      # an exiting msks-boot would just restart-loop under
      # Restart=always.
      if [ -e /state/debug-shell ]; then
        ( sleep 2; exec "${msks}/bin/msksd" --config /run/msksd/msksd.yaml ) &
        echo "msks appliance: DEBUG SHELL on console"
        echo "=== DIAG ==="
        id
        ls -l /dev/kvm 2>&1 || echo "NO /dev/kvm node"
        # The module load is msks-kvm.service's job now (the service
        # user cannot modprobe); the hatch reports the unit's state
        # and the evidence that KVM is usable.
        systemctl is-active msks-kvm.service 2>&1
        grep -cE "vmx|svm" /proc/cpuinfo
        "${vmm}/bin/cloud-hypervisor" --version 2>&1 || echo "CH EXEC FAIL rc=$?"
        ls /nix/store | head -3
        # Host-editable diagnostics: seed /state/diag.sh from outside.
        if [ -f /state/diag.sh ]; then
          echo "--- diag.sh ---"
          sh /state/diag.sh 2>&1
          echo "--- diag.sh end ---"
        fi
        echo "=== DIAG-END ==="
        exec sleep infinity
      fi
      # The dev-tree daemon (#144): the host shared its live
      # checkout (tag devtree -> /run/msks-dev-tree, mounted by
      # msks-dev-tree.service) and named it on the cmdline. The
      # checkout's venv python is a nix-store interpreter (the store
      # share resolves it); OUR package is imported from the shared
      # tree via PYTHONPATH — path-independent, so the venv's
      # host-absolute editable-install pointers never matter. The
      # repo nests the package one level deep (src/msks/msks), so
      # PYTHONPATH is $TREE/src/msks — that dir's msks/ is the
      # package. --reload
      # restarts the process when the shared tree changes (polling:
      # virtiofs carries no inotify events across the boundary).
      # Falls back to the store daemon — loudly — when the tree or
      # its venv is missing, so a stale cmdline pair cannot boot
      # half a daemon.
      if [ -n "''${MSKSD_DEV_TREE:-}" ]; then
        dev_py="$MSKSD_DEV_TREE/.devenv/state/venv/bin/python"
        if [ "$(cat /run/dev-tree.state 2>/dev/null)" = mounted ] \
          && [ -x "$dev_py" ] \
          && [ -d "$MSKSD_DEV_TREE/src/msks/msks" ]; then
          echo "msks appliance: DEV TREE daemon: $MSKSD_DEV_TREE (reload on edit)"
          export PYTHONPATH="$MSKSD_DEV_TREE/src/msks"
          # The share is read-only: every .pyc write attempt fails and
          # costs a syscall per module per restart. Skipping them keeps
          # the restart path quiet; nothing is lost (the share is ro).
          export PYTHONDONTWRITEBYTECODE=1
          exec "$dev_py" -m msks.server.main \
            --config /run/msksd/msksd.yaml --reload
        fi
        echo "msks appliance: MSKSD_DEV_TREE set but not usable; store daemon"
      fi
      exec "${msks}/bin/msksd" --config /run/msksd/msksd.yaml
    '';
  };

  # The state disk preparation script — what msks-state-format.service
  # ExecStarts (#101 pulled the shell out of the unit's ExecStart:
  # one readable script instead of a quoted one-liner). Runs as root,
  # before the fstab-generated state.mount (see the unit below for
  # the ordering). Every boot converges the disk, whatever it holds.
  msksStatePrepare = pkgs.writeTextFile {
    name = "msks-state-prepare";
    executable = true;
    destination = "/usr/local/sbin/msks-state-prepare";
    text = ''
      #!/bin/sh
      # Blank or foreign disks become labeled ext4 carrying the var/
      # staging tree (the bind source for /var); existing disks
      # converge to the same tree. #101 adds the daemon's
      # /state/msksd home to the converge set.
      set -eu

      # The disk must appear before anything can converge on it.
      i=0
      while [ $i -lt 50 ] && [ ! -b /dev/vdb ]; do
        sleep 0.2
        i=$((i + 1))
      done

      # Staging tree for a BLANK disk: the run/lock symlinks must
      # predate the /var bind mount, and the journal directory must
      # exist before journald's flush step looks for it (Debian
      # creates it at package install time; a blank disk has no
      # install). tmpfiles and journald create the rest on the
      # mounted disk.
      mkdir -p /run/msks-blank/var/log/journal
      ln -sfn /run /run/msks-blank/var/run
      ln -sfn /run/lock /run/msks-blank/var/lock

      # A disk with the label mounts as-is; an ext4 one WITHOUT it
      # (the old busybox init formatted fallback disks unlabeled)
      # gets e2label'd; anything else becomes a labeled ext4
      # carrying the staging tree.
      blkid -t LABEL=msks-state -o device /dev/vdb >/dev/null 2>&1 \
        || e2label /dev/vdb msks-state 2>/dev/null \
        || mkfs.ext4 -q -L msks-state -d /run/msks-blank /dev/vdb

      # Converge the mounted disk. The var/ staging merge (the same
      # steps as the blank staging above, applied in place).
      mkdir -p /run/msks-mnt
      mount /dev/vdb /run/msks-mnt
      mkdir -p /run/msks-mnt/var/log/journal
      ln -sfn /run /run/msks-mnt/var/run
      ln -sfn /run/lock /run/msks-mnt/var/lock

      # The daemon's /state/msksd home (#101): the state dir,
      # database, and TLS keys are service-user-owned; the daemon is
      # not root and cannot mkdir under /state. The top-level entries
      # a pre-#101 (root-daemon) disk carries move into the new home,
      # so an upgrade keeps its workspaces AND its pinned TLS CA —
      # the certificate material lives at the state-dir top level
      # (msks-ca*.pem, msks-cert*, msks-key*; see tls.py), and a
      # missed move would silently mint a fresh CA on first boot.
      # /state/debug-shell and /state/diag.sh are host-seeded markers
      # and stay at the top level.
      mkdir -p /run/msks-mnt/msksd
      moved=0
      for name in msks.db msks.db-wal msks.db-shm \
        msks-ca.pem msks-ca-key.pem msks-cert.pem msks-key.pem msks-cert.host \
        vms volumes images; do
        if [ -e /run/msks-mnt/$name ]; then
          mv /run/msks-mnt/$name /run/msks-mnt/msksd/
          moved=1
        fi
      done
      # The directory itself converges every boot; the recursive
      # chown runs only when something moved (a converged boot pays
      # one chown, not a walk over every workspace artifact).
      chown msksd:msksd /run/msks-mnt/msksd
      if [ "$moved" -eq 1 ]; then
        chown -R msksd:msksd /run/msks-mnt/msksd
      fi

      umount /run/msks-mnt
      rm -rf /run/msks-mnt /run/msks-blank 2>/dev/null || true
    '';
  };

  # The msks layer over the Debian tree: systemd units, the networkd
  # plan, and masks for what a headless read-only appliance never
  # runs. /var is a bind mount from the state disk (see fstab): a
  # writable /var is the appliance's mutable-root story — journald
  # persists to it, logind's state directory lives on it — so the
  # root itself can stay read-only forever.
  applianceOverlay =
    pkgs.runCommand "msks-appliance-overlay"
      {
        inherit msksBoot msksStatePrepare;
      }
      ''
        set -eu
        mkdir -p \
          $out/etc/systemd/system/multi-user.target.wants \
          $out/etc/systemd/system/local-fs.target.wants \
          $out/etc/systemd/system/sockets.target.wants \
          $out/etc/systemd/system/sysinit.target.wants \
          $out/etc/systemd/journald.conf.d \
          $out/etc/systemd/network \
          $out/etc/sysctl.d \
          $out/etc/modules-load.d \
          $out/etc/cloud \
          $out/nix/store \
          $out/state \
          $out/usr/local/sbin

        # The mountpoints systemd's generated units need: the store
        # share and the state disk mount over them (the busybox init
        # created these at boot; fstab mounts cannot).
        chmod 0755 $out/nix $out/nix/store $out/state

        # The boot script (see msksBoot) and the state-disk converge
        # script (see msksStatePrepare).
        cp "${msksBoot}/usr/local/sbin/msks-boot" $out/usr/local/sbin/msks-boot
        chmod 0755 $out/usr/local/sbin/msks-boot
        cp "${msksStatePrepare}/usr/local/sbin/msks-state-prepare" \
          $out/usr/local/sbin/msks-state-prepare
        chmod 0755 $out/usr/local/sbin/msks-state-prepare

        # Identity: hostname, hosts, and a stable machine-id (the
        # root is read-only, so systemd cannot write one at boot —
        # and a stable id keeps the journal's boot history coherent
        # across restarts). The value is hex for "MsksApplianceId2".
        printf 'msksd-appliance\n' > $out/etc/hostname
        printf '%s\n' \
          '127.0.0.1 localhost' \
          '127.0.1.1 msksd-appliance' \
          > $out/etc/hosts
        printf '4d536b734170706c69616e6365496432\n' > $out/etc/machine-id

        # The journal persists THROUGH /var: the fstab below binds
        # the state disk's var/ over /var. The image itself carries
        # no /var/log/journal (the root build removes it), so journald
        # starts volatile against the read-only root and the flush
        # step — pulled by the wants symlink, because Debian images
        # keep a real /var/log/journal from install time and never
        # need it — creates the directory on the mounted disk (the
        # state template stages it) and moves the boot's records off
        # the tmpfs.
        ln -s /lib/systemd/system/systemd-journald-flush.service \
          $out/etc/systemd/system/sysinit.target.wants/systemd-journald-flush.service
        printf '%s\n' \
          '# msks: the journal rides /var on the state disk; bounded' \
          '# so an unbounded journal cannot eat it.' \
          '[Journal]' \
          'Storage=auto' \
          'SystemMaxUse=512M' \
          > $out/etc/systemd/journald.conf.d/msks.conf

        # Mounts: the read-only root comes from the kernel cmdline
        # (no fstab root entry); the store share, the state disk, and
        # the state disk's var/ over /var are the appliance's own.
        # /tmp is tmpfs via the image's own enablement (Debian ships
        # tmp.mount wanted by local-fs.target in trixie) — the root
        # is read-only, scratch must not be.
        printf '%s\n' \
          '# msks: root comes from the kernel cmdline; no swap.' \
          '# The host /nix/store, read-only over virtiofs (tag store):' \
          '# msksd, the workspace VMM, and the tools resolve through it.' \
          'store /nix/store virtiofs ro 0 0' \
          '# The persistent state disk (#10), labeled at mkfs time.' \
          'LABEL=msks-state /state ext4 defaults,x-systemd.device-timeout=10s 0 2' \
          '# A writable /var from the same disk: the journal, the logind' \
          '# state directory, and /var/tmp live on state, not root.' \
          '/state/var /var none bind 0 0' \
          > $out/etc/fstab

        # The NIC: the static plan the manifest records (the host
        # bridge mirrors it; see scripts/appliance-setup.sh).
        printf '%s\n' \
          '[Match]' \
          'Name=en* eth*' \
          ''' \
          '[Network]' \
          'Address=${net.address}/${toString net.prefixLength}' \
          'Gateway=${net.gateway}' \
          > $out/etc/systemd/network/80-msks-appliance.network

        # Routing is machine identity (#101): the appliance forwards
        # between its uplink and the workspace taps, so the setting
        # ships as a boot-time sysctl — systemd-sysctl applies it in
        # sysinit, before the daemon starts — and msksd (a service
        # user with no write access to /proc/sys) verifies it and
        # refuses egress naming the key when it reads 0.
        printf '%s\n' \
          '# msks: the appliance routes for its egress workspaces (#52, #101).' \
          'net.ipv4.ip_forward = 1' \
          > $out/etc/sysctl.d/90-msks-ip-forward.conf

        # Kernel modules the appliance loads at boot: the egress
        # plumbing (#52 — the tap device and the nftables/NAT
        # machinery the daemon's rulesets need, including nft_ct of
        # #36) and the NIC driver. virtiofs (the store share) is
        # BUILT INTO the generic kernel — no module to load; KVM
        # loads via msks-kvm.service (its Intel/AMD variant depends
        # on the host CPU).
        printf '%s\n' \
          '# msks: the NIC and the egress stack (#52); virtiofs is built in.' \
          'virtio_net' \
          'tun' \
          'nf_tables' \
          'nft_chain_nat' \
          'nft_masq' \
          'nft_ct' \
          'nf_nat' \
          'nf_conntrack' \
          > $out/etc/modules-load.d/msks-modules.conf

        # networkd owns the NIC; udev's coldplug rename (eth0 to
        # ens3-like names) must settle first or the first
        # enumeration manages a name that stops existing (the same
        # race the guest's image pins).
        mkdir -p $out/etc/systemd/system/systemd-networkd.service.d
        printf '%s\n' \
          '[Unit]' \
          'After=systemd-udev-trigger.service systemd-udevd.service' \
          > $out/etc/systemd/system/systemd-networkd.service.d/10-after-udev-coldplug.conf
        ln -s /lib/systemd/system/systemd-networkd.service \
          $out/etc/systemd/system/multi-user.target.wants/systemd-networkd.service
        ln -s /lib/systemd/system/systemd-networkd.socket \
          $out/etc/systemd/system/sockets.target.wants/systemd-networkd.socket

        # The nested-KVM module for workspace VMs: which flavor loads
        # depends on the host CPU, so a shell picks. udev makes the
        # /dev/kvm node when the module registers.
        printf '%s\n' \
          '[Unit]' \
          'Description=msks nested-KVM module (workspace VMs)' \
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

        # The live dev-tree share (#144): when the host boots the
        # appliance with MSKS_DEV_TREE set, the run script attaches a
        # second read-only virtiofs tag (devtree) carrying the host
        # checkout. This root oneshot mounts it and records the
        # outcome OUTSIDE the mountpoint (/run/dev-tree.state) so
        # the marker can never sit on the read-only share; the unit
        # succeeds either way — an absent share is the normal boot,
        # not an error. Pulled in by msksd.service's Wants= below.
        printf '%s\n' \
          '[Unit]' \
          'Description=msks dev-tree share mount (present only when the host opted in)' \
          'DefaultDependencies=no' \
          'After=systemd-modules-load.service' \
          'Before=msksd.service' \
          ''' \
          '[Service]' \
          'Type=oneshot' \
          'ExecStart=/bin/sh -c "mkdir -p /run/msks-dev-tree && mount -t virtiofs devtree /run/msks-dev-tree 2>/dev/null && echo mounted > /run/dev-tree.state || echo absent > /run/dev-tree.state"' \
          'StandardOutput=journal+console' \
          > $out/etc/systemd/system/msks-dev-tree.service

        # The state disk converges before it mounts: the preparation
        # script (see msksStatePrepare) waits for the disk, labels or
        # formats it, and on every boot merges the var/ staging tree
        # and prepares the daemon's /state/msksd home into the mounted
        # disk — so blank, foreign, and existing (including pre-#101
        # root-daemon) disks all converge. Ordered before the mount
        # units BY NAME — fstab's generated state.mount — with
        # DefaultDependencies=no, because a unit with default
        # dependencies is After=basic.target and cannot run this
        # early without an ordering cycle. set -e inside the script:
        # a failed step fails the unit — swallowing it would leave
        # state.mount timing out with this unit looking innocent.
        printf '%s\n' \
          '[Unit]' \
          'Description=msks state disk preparation (blank or foreign disks become ext4; var staging and the service user home converge)' \
          'DefaultDependencies=no' \
          'After=dev-vdb.device' \
          'Before=local-fs.target state.mount var.mount shutdown.target' \
          'Conflicts=shutdown.target' \
          ''' \
          '[Service]' \
          'Type=oneshot' \
          'ExecStart=/usr/local/sbin/msks-state-prepare' \
          'StandardOutput=journal+console' \
          'StandardError=journal+console' \
          ''' \
          '[Install]' \
          'WantedBy=local-fs.target' \
          > $out/etc/systemd/system/msks-state-format.service
        ln -s ../msks-state-format.service \
          $out/etc/systemd/system/local-fs.target.wants/msks-state-format.service

        # The daemon: ordered after the store share and state disk it
        # lives on (Requires: without them it cannot run at all), the
        # state-disk preparation (Requires: a half-migrated disk —
        # the format script died mid-move — must not get a daemon
        # crash-looping against still-root-owned files; the next
        # boot's converge finishes the migration), and the KVM module
        # its workspaces need. The format unit's RemainAfterExit
        # keeps that Requires from re-running it on every daemon
        # restart — a crash loop would otherwise remount the live
        # state disk once per second. Output goes to the journal AND
        # the console (the serial log carries the TOFU fingerprint
        # and the boot markers, as before). A crash restarts the
        # daemon in place — the supervisor used to need a whole-VM
        # restart for that.
        #
        # The privilege contract (#101): a dedicated service user
        # holds exactly two ambient capabilities — CAP_NET_ADMIN
        # (taps and addresses, the nftables tables, and through exec
        # inheritance the workspace VMM opening its tap) and
        # CAP_NET_BIND_SERVICE (the DHCP 67 and DNS 53 listeners) —
        # and nothing in the unit's tree runs as uid 0. Ambient, not
        # merely bounding, so the tools and the VMM the daemon
        # execs keep them; /dev/kvm arrives through the kvm
        # supplementary group (udev's default rule: mode 0660,
        # group kvm). The accounts are baked into the image (see
        # applianceRoot) because the read-only root cannot take
        # sysusers writes. RuntimeDirectory hands the boot script a
        # service-user-owned /run/msksd for the generated settings
        # file.
        printf '%s\n' \
          '[Unit]' \
          'Description=msksd appliance daemon' \
          'Documentation=https://github.com/mcdonc/msks' \
          'Requires=nix-store.mount state.mount msks-state-format.service' \
          'After=nix-store.mount state.mount msks-state-format.service msks-kvm.service systemd-networkd.service' \
          'Wants=msks-dev-tree.service' \
          'StartLimitIntervalSec=0' \
          ''' \
          '[Service]' \
          'ExecStart=/usr/local/sbin/msks-boot' \
          'User=msksd' \
          'Group=msksd' \
          'SupplementaryGroups=kvm' \
          'AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE' \
          'CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE' \
          'RuntimeDirectory=msksd' \
          'Restart=always' \
          'RestartSec=1' \
          'StandardInput=null' \
          'StandardOutput=journal+console' \
          'StandardError=journal+console' \
          ''' \
          '[Install]' \
          'WantedBy=multi-user.target' \
          > $out/etc/systemd/system/msksd.service
        ln -s ../msksd.service \
          $out/etc/systemd/system/multi-user.target.wants/msksd.service

        # cloud-init stays OFF (see the header): the cmdline bridge
        # is the config channel. The marker file stops the generator
        # from enabling anything; the masks hold even if something
        # else tries.
        touch $out/etc/cloud/cloud-init.disabled
        for unit in cloud-init.service cloud-init-local.service \
          cloud-config.service cloud-final.service; do
          ln -s /dev/null $out/etc/systemd/system/$unit
        done

        # The boot diet: units a headless appliance never runs.
        # Masks win over the image's wants symlinks; each name costs
        # boot time (AppArmor ~0.7s), pokes the network with nothing
        # to reach (motd-news, unattended upgrades), or serves
        # nothing here (ssh: the API and the console debug hatch are
        # the access paths, and host keys cannot regenerate on a
        # read-only root; resolved: the daemon's forwarder serves
        # guests, and the appliance's own resolver is a static
        # file). Units that write /var stay — /var is the state-disk
        # bind, and logind (the ACPI power-button handler the host's
        # graceful teardown presses) runs unmasked.
        for unit in apparmor.service systemd-firstboot.service \
          grub-common.service unattended-upgrades.service \
          e2scrub_reap.service e2scrub_all.timer \
          systemd-timesyncd.service \
          systemd-resolved.service systemd-networkd-wait-online.service \
          apt-daily.timer apt-daily.service apt-daily-upgrade.timer \
          apt-daily-upgrade.service apt-listchanges.timer \
          dpkg-db-backup.timer man-db.timer \
          motd-news.timer motd-news.service fstrim.timer \
          ssh.service ssh.socket sshd-unix-local.socket; do
          ln -s /dev/null $out/etc/systemd/system/$unit
        done
      '';

  # Parse sfdisk --json: print the byte offset of the Linux root
  # partition (the same helper guest-assets.nix uses).
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

  # The appliance root tree: the Debian genericcloud tree extracted
  # unprivileged (qcow2 -> raw -> partition slice -> debugfs rdump),
  # the msks overlay laid on top, the generic kernel's module tree
  # swapped in, and the result asserted bootable. Kept as ONE opaque
  # tarball for the same reason as the guest's: the store's
  # hardlink-optimise would otherwise dedup identical files inside a
  # tree-shaped path, and mke2fs -d packs hardlink groups as one
  # inode.
  applianceRoot =
    pkgs.runCommand "msks-appliance-root"
      {
        inherit
          applianceOverlay
          debianImage
          genericKernel
          partitionOffset
          ;
        nativeBuildInputs = [
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
        root=root-tree
        mkdir -p "$root"

        # qcow2 -> raw, then slice the Linux root partition out (the
        # GPT offset, not a hardcoded constant).
        qemu-img convert -O raw ${debianImage} debian.raw
        offset=$(sfdisk --json debian.raw | python3 ${partitionOffset})
        dd if=debian.raw of=root.part bs=512 skip=$((offset / 512)) status=none

        # ext4 -> tree (ownership noise from rdump is expected
        # unprivileged; the metadata walk right below hands the
        # pack stage what it restores).
        debugfs -R "rdump / $root" root.part 2>/dev/null || true
        rm -rf "$root"/lost+found
        for top in bin usr etc var lib boot; do
          test -d "$root/$top"
        done

        # Every source inode's mode/uid/gid before the tree diverges
        # (#169, #179 — same walker, same pins, same base image as
        # the workspace guest). The fakeroot pack stage applies it.
        mkdir -p "$out"
        python3 ${inodeMeta} walk root.part "$out"/inode-metadata

        # The msks overlay (units, masks, identity, the boot script).
        # The image's read-only regular files the overlay replaces
        # (machine-id is 0444 in the base) must go first: cp cannot
        # open them for writing, but rm needs only the directory.
        # /var/log/journal leaves too: against the read-only root it
        # would only trick journald into a persistent start it cannot
        # write — the state disk's staged directory is the real one.
        # It is a setgid directory (gid 999, systemd-journal), so
        # the deletion is declared to the pack stage as an expected
        # special-mode absence.
        rm -rf "$root"/var/log/journal
        rm -f "$root"/etc/machine-id
        cp -a --no-preserve=ownership ${applianceOverlay}/. "$root"/
        # Store outputs are read-only, and cp -a copies those modes onto
        # the tree's existing directories (etc, usr, var): every later
        # mutation (resolv.conf, netplan, the kernel swap) needs the
        # write bits back.
        chmod -R u+w "$root"

        # The service identity (#101): msksd runs as its own system
        # user, and /dev/kvm reaches it through the kvm group. The
        # root is READ-ONLY, so systemd-sysusers cannot write /etc at
        # boot — the accounts bake here, at build time, into the
        # base's account files. Ids are the first free slot in
        # [200,249] of the system range: deterministic for the fixed
        # pin, and a future base that fills a slot shifts to the
        # next one instead of colliding. The unit's
        # SupplementaryGroups=kvm names the group, so no /etc/group
        # membership entry is needed.
        free_id() {
          for id in $(seq 200 249); do
            cut -d: -f3 "$1" | grep -qx "$id" || { echo "$id"; return 0; }
          done
          return 1
        }
        msksd_uid=$(free_id "$root/etc/passwd") \
          || { echo "no free system uid in 200-249"; exit 1; }
        msksd_gid=$(free_id "$root/etc/group") \
          || { echo "no free system gid in 200-249"; exit 1; }
        grep -q '^msksd:' "$root/etc/passwd" || {
          printf 'msksd:x:%s:%s:msksd appliance daemon:/state/msksd:/usr/sbin/nologin\n' \
            "$msksd_uid" "$msksd_gid" >> "$root/etc/passwd"
          printf 'msksd:!:19000:0:99999:7:::\n' >> "$root/etc/shadow"
          printf 'msksd:x:%s:\n' "$msksd_gid" >> "$root/etc/group"
          printf 'msksd:!::\n' >> "$root/etc/gshadow"
        }
        grep -q '^kvm:' "$root/etc/group" || {
          kvm_gid=$(free_id "$root/etc/group") \
            || { echo "no free system gid in 200-249"; exit 1; }
          printf 'kvm:x:%s:\n' "$kvm_gid" >> "$root/etc/group"
          printf 'kvm:!::\n' >> "$root/etc/gshadow"
        }

        # The resolver the egress forwarder relays to (#52): a public
        # resolver by default — the host bridge gateway runs no
        # listener. Point the kernel cmdline's
        # msksd.egress_dns_upstream= at another resolver to override
        # (the env bridge turns it into MSKSD_EGRESS_DNS_UPSTREAM).
        # The image's resolv.conf is a symlink into resolved's runtime
        # (resolved is masked here); a real file replaces it.
        rm -f "$root"/etc/resolv.conf
        printf 'nameserver 9.9.9.9\n' > "$root"/etc/resolv.conf

        # The generic kernel's module tree replaces the image's own
        # (the image ships its own tree for the same upstream version —
        # this republishes the pinned deb's so the build owns the
        # pairing); a stale vermagic tree would make every module
        # probe miss. The image's /boot payload leaves with it — the
        # appliance direct-boots artifacts kept outside the image.
        rm -rf "$root"/lib/modules/*
        rm -rf "$root"/usr/lib/modules/* 2>/dev/null || true
        rm -f "$root"/boot/vmlinuz-* "$root"/boot/initrd.img-* \
          "$root"/boot/System.map-* "$root"/boot/config-*
        mkdir -p "$root"/usr/lib/modules
        cp -a --no-preserve=ownership \
          "${genericKernel}"/usr/lib/modules/. "$root"/usr/lib/modules/
        cp "${genericKernel}"/boot/config-* "$root"/boot/

        # The deb ships no depmod metadata (its postinst generates it
        # on the target); generate it here so modprobe — the
        # modules-load set, udev alias lookups — resolves anything.
        find "$root"/usr/lib/modules -type d -exec chmod u+w {} +
        kver=$(ls "$root"/usr/lib/modules | head -1)
        depmod -b "$root" "$kver"
        test -s "$root"/usr/lib/modules/"$kver"/modules.dep

        # The module set the appliance actually probes: Debian's
        # generic kernel owns them (#92 answers the open question —
        # nothing needs a hand-maintained module tree; the cloud
        # flavor the guest boots would NOT have carried virtiofs or
        # kvm-intel/kvm-amd). Fail the build at pin-drift time, not
        # at boot.
        modtree="$root"/usr/lib/modules/"$kver"/kernel
        for mod in virtio_net tun nf_tables nft_chain_nat \
          nft_masq nft_ct nf_nat nf_conntrack kvm kvm-intel kvm-amd; do
          find "$modtree" -name "$mod.ko.xz" -o -name "$mod.ko" | grep -q . \
            || { echo "generic module tree missing $mod"; exit 1; }
        done
        grep -q virtiofs "$root"/usr/lib/modules/"$kver"/modules.builtin \
          || { echo "generic kernel does not build virtiofs in"; exit 1; }

        # The /dev/kvm contract (#101): udev's default rule hands the
        # node to the kvm group (mode 0660), which the service user
        # opens it through. Fail the build at pin-drift time, not at
        # boot, if the base stops shipping the rule.
        udev_rules="$root/usr/lib/udev/rules.d"
        [ -d "$udev_rules" ] || udev_rules="$root/lib/udev/rules.d"
        grep -h 'KERNEL=="kvm"' "$udev_rules"/*.rules 2>/dev/null \
          | grep -q 'GROUP="kvm"' \
          || { echo "udev does not assign /dev/kvm to the kvm group"; exit 1; }

        # Sanity: this must be a bootable Debian with the msks layer.
        test -x "$root"/sbin/init
        test -x "$root"/sbin/mkfs.ext4
        test -x "$root"/sbin/e2label
        test -x "$root"/usr/local/sbin/msks-boot
        test -x "$root"/usr/local/sbin/msks-state-prepare
        test -d "$root"/var/lib/systemd
        test -f "$root"/etc/systemd/system/msksd.service
        test -f "$root"/usr/lib/modules/"$kver"/modules.dep
        grep -q '^msksd:.*:.*:/state/msksd:' "$root"/etc/passwd
        grep -q '^kvm:' "$root"/etc/group
        grep -q '^net.ipv4.ip_forward = 1$' "$root"/etc/sysctl.d/90-msks-ip-forward.conf

        # Size the final image from the tree (content-derived).
        mkdir -p "$out"
        du -s --apparent-size --block-size=4096 "$root" | cut -f1 > "$out"/tree-blocks
        tar --sort=name --owner=0 --group=0 --numeric-owner \
          -C "$root" -cf "$out/root.tar" .
      '';

  # mke2fs -d packs the tree into an ext4 image without mounting
  # anything — unprivileged and host-independent. One fakeroot
  # session owns the tree and builds the image (rdump landed
  # everything build-user owned; the faked chown/chmod are what
  # mke2fs -d bakes in). The pack applies the inode-metadata
  # manifest on top of the root:root baseline (#169, #179): the
  # appliance's tree diverges from the base by overlay, mask, and
  # kernel swap, and only those paths keep the baseline — every
  # path the base shipped comes back with its own mode and owner.
  packScript = pkgs.writeText "msks-appliance-pack.sh" ''
    set -eu
    tree="''${PACK_TREE:?}"
    img="''${PACK_IMG:?}"
    blocks="''${PACK_BLOCKS:?}"
    fake_epoch="''${PACK_FAKE_EPOCH:?}"
    meta="''${PACK_META:?}"
    applier="''${PACK_APPLIER:?}"
    expected_absent="''${PACK_EXPECTED_ABSENT:-}"
    chown -R 0:0 "$tree"
    chmod 0640 "$tree"/etc/shadow "$tree"/etc/gshadow
    chmod 0600 "$tree"/etc/ssh/ssh_host_*_key 2>/dev/null || true
    python3 "$applier" apply "$meta" "$tree" $expected_absent
    E2FSPROGS_FAKE_TIME="$fake_epoch" mke2fs -q -t ext4 -b 4096 -I 256 \
      -L msks-rootfs \
      -E hash_seed=00000000-0000-0000-0000-000000000001 \
      -d "$tree" "$img" "$blocks"
    E2FSPROGS_FAKE_TIME="$fake_epoch" tune2fs -U 00000000-0000-0000-0000-000000000002 "$img" >/dev/null
  '';

  rootfs =
    pkgs.runCommand "msks-appliance-rootfs"
      {
        inherit applianceRoot packScript inodeMeta;
        nativeBuildInputs = [
          pkgs.e2fsprogs
          pkgs.fakeroot
          pkgs.gnutar
          (pkgs.python3.withPackages (ps: [ ]))
        ];
        fakeEpoch = 1262304000;
      }
      ''
        set -eu
        mkdir -p "$out"
        mkdir work
        tar -C work -xf "$applianceRoot/root.tar"
        chmod -R u+w work
        # Content plus 256M of slack: the root is read-only at
        # runtime, so the slack only covers image growth across
        # Debian point updates.
        PACK_TREE=work \
          PACK_IMG="$out/rootfs.ext4" \
          PACK_BLOCKS=$(( $(cat "$applianceRoot"/tree-blocks) + 65536 )) \
          PACK_FAKE_EPOCH="$fakeEpoch" \
          PACK_META="$applianceRoot/inode-metadata" \
          PACK_APPLIER="${inodeMeta}" \
          PACK_EXPECTED_ABSENT="/var/log/journal" \
          fakeroot -- /bin/sh -e "$packScript"
      '';

  # Persistent-state template: blank ext4 carrying the var/ staging
  # tree (the bind source for the appliance's writable /var — see
  # the overlay's fstab). The up-task copies it once per install; the
  # appliance's preparation unit converges every other path (blank
  # or foreign disks, and existing disks missing the staging tree).
  stateDisk =
    pkgs.runCommand "msks-appliance-state"
      {
        nativeBuildInputs = [
          pkgs.e2fsprogs
          pkgs.fakeroot
        ];
        fakeEpoch = 1262304000;
      }
      ''
        set -eu
        mkdir -p "$out"
        # Room for two images (the cloud-init-bearing genericcloud
        # base lands at ~1.5G rootfs plus ~1.5G retained archive
        # each, #41) with the database, tokens, workspace
        # overlays/volumes, and the journal under it.
        truncate -s 8G "$out/state.ext4"
        # The staging var/: the run/lock symlinks must predate the
        # /var bind mount, the journal directory must exist before
        # journald's flush step looks for it (Debian creates it at
        # package install time; a blank disk has no install), and
        # everything else tmpfiles creates on the mounted disk.
        # fakeroot bakes root ownership in (the build user would
        # otherwise own the tree /var binds over).
        mkdir -p stage/var/log/journal
        ln -s /run stage/var/run
        ln -s /run/lock stage/var/lock
        fakeroot -- sh -c '
          chown -R 0:0 stage
          E2FSPROGS_FAKE_TIME="$fakeEpoch" mke2fs -q -F -t ext4 -b 4096 -I 256 \
            -L msks-state \
            -E hash_seed=00000000-0000-0000-0000-000000000003 \
            -d stage \
            "$out/state.ext4"
        '
        E2FSPROGS_FAKE_TIME="$fakeEpoch" tune2fs -U 00000000-0000-0000-0000-000000000004 "$out/state.ext4" >/dev/null
      '';

  manifest = pkgs.writeText "appliance-manifest.json" (
    builtins.toJSON {
      kernel = "${genericKernel}";
      initrd = "${minimalInitrd}";
      rootfs = "${rootfs}/rootfs.ext4";
      stateDisk = "${stateDisk}/state.ext4";
      cmdline = kernelCmdline;
      network = net;
      msksd = "${msks}";
      vmm = "${vmm}";
      defaultImage = defaultImage;
    }
  );
in
pkgs.runCommand "msks-appliance"
  {
    inherit
      rootfs
      stateDisk
      manifest
      genericKernel
      minimalInitrd
      ;
    # The outputs referenced only by the manifest text: making them
    # build-input-style deps of this derivation keeps them realized on
    # the host (the appliance sees them through the virtiofs share).
    deps = [
      msks
      vmm
      guest
    ];
  }
  ''
    set -eu
    mkdir -p "$out"
    vmlinuz=$(ls "${genericKernel}"/boot/vmlinuz-*)
    cp -L "$vmlinuz" "$out/vmlinux"
    cp -L "$minimalInitrd/initrd" "$out/initrd"
    cp -L "$rootfs/rootfs.ext4" "$out/rootfs.ext4"
    cp -L "$stateDisk/state.ext4" "$out/state.ext4"
    cp -L "$manifest" "$out/appliance-manifest.json"
  ''
