# The production msksd appliance as a NixOS system (#212, the
# production half of #205). ONE configuration, TWO store shapes:
#
#   dev       the host's /nix/store over a read-only virtiofs share
#             (tag "store") — the same contract the Debian appliance
#             shipped: the appliance runs the store paths the host
#             built, workspace guest assets flow in with zero
#             copying, and a rebuild restarts into a fresh system.
#
#   deployed  the store ON DISK: an erofs base image carrying the
#             system closure AND its nix database (spike 2's recipe),
#             an ext4 store volume for the overlayfs upper layer and
#             the profile, /nix/store the overlay. Updates arrive
#             over ssh from the host (spike 3's --target-host path):
#             only missing paths copy, and the appliance-side system
#             profile is the generation pointer.
#
# The mode rides the msks.mode option. Its default comes from the
# environment: a plain evaluation (nix-build of nix/appliance-nixos.nix,
# or nixos-rebuild over ssh from the host) sees no environment and
# gets "deployed" — the appliance's own shape; the dev-mode artifact
# build exports MSKS_APPLIANCE_MODE=dev. Everything else in this file
# is mode-independent or branches on this one value.
#
# Hazard, stated plainly: the evaluating shell's environment picks
# the shape. An operator who leaves MSKS_APPLIANCE_MODE=dev exported
# (the natural state after a dev build) and later runs the deployed
# update path — evaluated on the host — silently builds a dev-shaped
# system that cannot boot on the appliance. Unset it (or export
# MSKS_APPLIANCE_MODE=deployed) before any update-path evaluation.
#
# The unit contract is carried verbatim from the Debian appliance
# (nix/appliance-image.nix): the msksd service user with its two
# ambient capabilities, the kernel-cmdline settings bridge, the
# generated settings file, the state-disk converge unit, the debug
# shell, the dev-tree daemon, and the static networkd plan on the
# bridge address. Where that appliance masked Debian units, this one
# simply does not enable the NixOS equivalents.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  mode =
    let
      env = builtins.getEnv "MSKS_APPLIANCE_MODE";
    in
    if env != "" then env else "deployed";

  # The daemon closure, the same build the Debian appliance ships
  # (nix/msks-pkg.nix): evaluated against this configuration's pinned
  # nixpkgs, so the appliance's daemon and the host's build are the
  # same store paths in dev mode.
  msks = pkgs.python314.pkgs.callPackage ./msks-pkg.nix {
    textual = pkgs.python314.pkgs.callPackage ./textual-pkg.nix { };
    netfilterqueue = pkgs.python314.pkgs.callPackage ./netfilterqueue-pkg.nix { };
  };

  # Workspace-artifact tools (the same set the Debian appliance names
  # in the generated settings file): the workspace VMM, the overlay
  # builder, the volume formatter, the seed-disk builder, and the
  # egress plumbing.
  vmm = pkgs.cloud-hypervisor;
  qemuImg = pkgs.qemu-utils;
  e2fsprogs = pkgs.e2fsprogs;
  mkisofs = pkgs.cdrtools;
  iproute2 = pkgs.iproute2;
  nftables = pkgs.nftables;

  # Network plan (the host bridge mirrors it; the manifest ships it):
  # networkd applies it inside the appliance, on eth0 (predictable
  # names stay off — the egress nftables rules name eth0).
  net = {
    address = "192.168.77.2";
    prefixLength = 24;
    gateway = "192.168.77.1";
  };

  # The update-path store URI (deployed mode): reads consult the
  # erofs base's database AND the upper layer's, writes land only in
  # the upper layer (spike 2's recipe, spike 3's update proof).
  # check-mount=false: /nix/store is a MOUNT POINT here, which the
  # plain local store treats as an error unless told otherwise.
  overlayStoreUri =
    "local-overlay://?real=/nix/store"
    + "&lower-store=local%3Froot%3D%2Fnix%2F.ro-store%26read-only%3Dtrue"
    + "&upper-layer=/nix/.upper-volume/store"
    + "&state=/nix/.upper-volume/nix-var"
    + "&check-mount=false";

  # The daemon bring-up script — the Debian appliance's msks-boot
  # carried over: the kernel-cmdline settings bridge, the generated
  # settings file, the identity evidence on the console, the
  # debug-shell marker block, the dev-tree daemon, and the exec of
  # the store daemon. See appliance-image.nix's msksBoot for the
  # original comments; the behavior is identical.
  msksBoot = pkgs.writeTextFile {
    name = "msks-boot";
    executable = true;
    destination = "/usr/local/sbin/msks-boot";
    text = ''
      #!/bin/sh
      export PATH="${iproute2}/sbin:${nftables}/sbin:${qemuImg}/bin:${e2fsprogs}/sbin:${vmm}/bin:${msks}/bin:$PATH"

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

      echo "msks appliance: daemon identity: $(id)"
      grep -E '^Cap(Prm|Eff|Amb):' /proc/self/status \
        | sed 's/^/msks appliance: daemon /'

      if [ -e /state/debug-shell ] && [ ! -e /tmp/msks-debug-diag ]; then
        touch /tmp/msks-debug-diag
        echo "msks appliance: DEBUG: root shell on the console (msks-debug-shell.service)"
        echo "=== DIAG ==="
        id
        ls -l /dev/kvm 2>&1 || echo "NO /dev/kvm node"
        systemctl is-active msks-kvm.service 2>&1
        grep -cE "vmx|svm" /proc/cpuinfo
        "${vmm}/bin/cloud-hypervisor" --version 2>&1 || echo "CH EXEC FAIL rc=$?"
        ls /nix/store | head -3
        if [ -f /state/diag.sh ]; then
          echo "--- diag.sh ---"
          sh /state/diag.sh 2>&1
          echo "--- diag.sh end ---"
        fi
        echo "=== DIAG-END ==="
      fi
      if [ -n "''${MSKSD_DEV_TREE:-}" ]; then
        dev_py="$MSKSD_DEV_TREE/.devenv/state/venv/bin/python"
        if [ "$(cat /run/dev-tree.state 2>/dev/null)" = mounted ] \
          && [ -x "$dev_py" ] \
          && [ -d "$MSKSD_DEV_TREE/src/msks/msks" ]; then
          echo "msks appliance: DEV TREE daemon: $MSKSD_DEV_TREE (reload on edit)"
          export PYTHONPATH="$MSKSD_DEV_TREE/src/msks"
          export PYTHONDONTWRITEBYTECODE=1
          exec "$dev_py" -m msks.server.main \
            --config /run/msksd/msksd.yaml --reload
        fi
        echo "msks appliance: MSKSD_DEV_TREE set but not usable; store daemon"
      fi
      exec "${msks}/bin/msksd" --config /run/msksd/msksd.yaml
    '';
  };

  # The state-disk converge script — the Debian appliance's
  # msks-state-prepare carried over (blank, foreign, and existing
  # disks all converge), with one NixOS-era change: the raw device
  # letter, which the boot payload pins per mode (dev: vda, the only
  # disk; deployed: vdc — erofs vda, store volume vdb). The
  # appliance's machine identity is NOT seeded here: it is
  # build-stable through systemd.machine_id= on the kernel cmdline
  # (see boot.kernelParams).
  stateDiskDevice = if mode == "dev" then "/dev/vda" else "/dev/vdc";

  msksStatePrepare = pkgs.writeTextFile {
    name = "msks-state-prepare";
    executable = true;
    destination = "/usr/local/sbin/msks-state-prepare";
    text = ''
      #!/bin/sh
      set -eu

      # The disk must appear before anything can converge on it: the
      # label path once the disk carries the label, the raw letter as
      # the label-less fallback.
      i=0
      while [ $i -lt 50 ] && [ ! -e /dev/disk/by-label/msks-state ] \
        && [ ! -b ${stateDiskDevice} ]; do
        sleep 0.2
        i=$((i + 1))
      done
      dev=${stateDiskDevice}
      [ -b "$dev" ] || dev=/dev/disk/by-label/msks-state

      mkdir -p /run/msks-blank/var/log/journal
      ln -sfn /run /run/msks-blank/var/run
      ln -sfn /run/lock /run/msks-blank/var/lock

      blkid -t LABEL=msks-state -o device "$dev" >/dev/null 2>&1 \
        || e2label "$dev" msks-state 2>/dev/null \
        || mkfs.ext4 -q -L msks-state -d /run/msks-blank "$dev"

      mkdir -p /run/msks-mnt
      mount "$dev" /run/msks-mnt
      mkdir -p /run/msks-mnt/var/log/journal
      ln -sfn /run /run/msks-mnt/var/run
      ln -sfn /run/lock /run/msks-mnt/var/lock

      # sshd's persistent host-key directory (deployed mode): the
      # root is tmpfs, so the keys live on state.
      mkdir -p /run/msks-mnt/ssh
      chmod 0700 /run/msks-mnt/ssh

      # The daemon's /state/msksd home: the state dir, database, and
      # TLS keys are service-user-owned; the daemon is not root and
      # cannot mkdir under /state. A disk from the Debian appliance
      # (or a pre-#101 root-daemon disk) carries the same paths, so
      # the first NixOS boot runs the same migration the Debian image
      # ran: top-level entries move into the home, keeping workspaces
      # and the pinned TLS CA.
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
      chown msksd:msksd /run/msks-mnt/msksd
      if [ "$moved" -eq 1 ]; then
        chown -R msksd:msksd /run/msks-mnt/msksd
      fi

      umount /run/msks-mnt
      super=$(dumpe2fs -h "$dev" 2>/dev/null || true)
      fs_blocks=$(printf '%s\n' "$super" | sed -n 's/^Block count:[[:space:]]*//p')
      fs_block_size=$(printf '%s\n' "$super" | sed -n 's/^Block size:[[:space:]]*//p')
      base=$(basename "$(readlink -f "$dev")")
      dev_bytes=$(( $(cat "/sys/class/block/$base/size") * 512 ))
      if [ -z "$fs_blocks" ] || [ -z "$fs_block_size" ]; then
        echo "msks-state-prepare: could not read the filesystem size; skipping the grow"
      elif [ $((fs_blocks * fs_block_size)) -lt "$dev_bytes" ]; then
        e2fsck -fy "$dev" || true
        resize2fs "$dev" \
          || echo "msks-state-prepare: resize2fs failed; retrying next boot"
      fi
      rm -rf /run/msks-mnt /run/msks-blank 2>/dev/null || true
    '';
  };

  # Generation retention (#212 item 6): keep the newest three system
  # generation links — the booted generation plus the two prior,
  # #205's floor, so the host run script's fallback pointer always
  # has a previous generation to reach — by passing the older ones to
  # nix-env by number (explicit numbers work on every nix; the
  # "+N" keep-form postdates the daemon's nix). Then collect what
  # the trim released, against the overlay store only (upper-scoped:
  # the erofs base is read-only and untouchable by construction).
  storeTrim = pkgs.writeShellScript "msks-store-trim" ''
    set -eu
    profile=/nix/var/nix/profiles/system
    dir=$(dirname "$profile")
    keep=$(ls -1v "$dir" | grep -E '^system-[0-9]+-link$' | tail -3)
    old=""
    for link in $(ls -1v "$dir" | grep -E '^system-[0-9]+-link$'); do
      case "$keep" in
      *"$link"*) ;;
      *)
        gen=''${link#system-}
        gen=''${gen%-link}
        old="$old $gen"
        ;;
      esac
    done
    if [ -n "$old" ]; then
      # shellcheck disable=SC2086
      nix-env -p "$profile" --delete-generations $old
      echo "msks-store-trim: deleted generations:$old"
    fi
    nix-store --gc
  '';
in
{
  imports = [
    # The store machinery (both shapes): the ro-store share mount in
    # dev mode, the erofs store disk + overlay + volume mounts in
    # deployed mode, and the init=<toplevel>/init kernel param.
    (builtins.getFlake "github:microvm-nix/microvm.nix/187b0a390ee054028106a674e7b01b1cb940cbba")
    .nixosModules.microvm
  ];

  options.msks.mode = lib.mkOption {
    type = lib.types.enum [
      "dev"
      "deployed"
    ];
    description = "The appliance's store shape (see the file header).";
  };

  options.msks.daemon = lib.mkOption {
    type = lib.types.package;
    readOnly = true;
    description = "The msksd daemon closure this system ships (the build's manifest references it).";
  };

  config = {
    msks.mode = mode;
    msks.daemon = msks;

    microvm = {
      hypervisor = "cloud-hypervisor";
      # The closure is never registered into a fresh nix db at boot:
      # dev mode shares the HOST's store (its db already knows the
      # paths), deployed mode ships the db inside the erofs base
      # (spike 2). Either way a boot-time load-db pass would only
      # cost time (spike 1's landmine).
      registerClosure = false;
      shares = lib.optionals (mode == "dev") [
        {
          proto = "virtiofs";
          tag = "store";
          source = "/nix/store";
          mountPoint = "/nix/store";
        }
      ];
      volumes = lib.optionals (mode == "deployed") [
        {
          image = "store-volume.img";
          label = "MSKSSTORE";
          mountPoint = "/nix/.upper-volume";
          autoCreate = false;
        }
      ];
      storeOnDisk = mode == "deployed";
      storeDiskType = "erofs";
      writableStoreOverlay =
        if mode == "deployed" then "/nix/.upper-volume" else null;
    };

    # The erofs base's store directory is the overlay's lowerdir
    # (spike 2's pinned wiring — the module's default lowerdir would
    # point at a path this layout does not create).
    fileSystems."/nix/store".overlay.lowerdir = lib.mkIf (mode == "deployed") (
      lib.mkForce [ "/nix/.ro-store/nix/store" ]
    );

    boot.kernelParams = [
      "console=ttyS0"
      # NixOS's predictable-name default off: the egress nftables
      # rules name eth0 (the module adds this too; keep one source).
      "net.ifnames=0"
      # The appliance's machine identity, build-stable (the same
      # stable id the Debian appliance baked into /etc/machine-id —
      # the NixOS root is tmpfs, so the identity rides the cmdline;
      # a stable id keeps the journal's boot history coherent).
      "systemd.machine_id=4d536b734170706c69616e6365496432"
    ];
    boot.initrd.availableKernelModules = [
      "virtio_blk"
      "virtio_pci"
      "virtio_net"
      "ext4"
    ]
    ++ lib.optionals (mode == "deployed") [
      "overlay"
      "erofs"
    ];

    # Kernel modules the appliance loads at boot: the egress plumbing
    # (the tap devices and the nftables/NAT machinery the daemon's
    # rulesets need) — deterministic, not on demand. KVM loads via
    # msks-kvm.service (its flavor depends on the host CPU).
    boot.kernelModules = [
      "tun"
      "nf_tables"
      "nft_chain_nat"
      "nft_masq"
      "nft_ct"
      "nf_nat"
      "nf_conntrack"
    ];

    boot.kernel.sysctl."net.ipv4.ip_forward" = 1;

    # The persistent state disk (#10, unchanged from the Debian
    # appliance): labeled ext4 at /state, /var a bind from it (the
    # journal and logind state ride the disk; the root is a tmpfs),
    # and /etc/machine-id bound from it — the NixOS root is tmpfs, so
    # the appliance's identity lives on state.
    fileSystems."/state" = {
      device = "/dev/disk/by-label/msks-state";
      fsType = "ext4";
      options = [ "x-systemd.device-timeout=10s" ];
      # The preparation unit owns the disk's repair path explicitly
      # (e2fsck before resize2fs on the grow), and a boot-time fsck
      # races that unit's own mount — the two excl-exclusively open
      # the blockdev and one of them loses.
      noCheck = true;
    };
    fileSystems."/var" = {
      device = "/state/var";
      fsType = "none";
      options = [ "bind" ];
      # NOT an initrd filesystem (mkForce over NixOS's journald-driven
      # inference): in stage 1 the bind's source cannot exist yet
      # (state converges in stage 2), and the initrd's tmpfs at
      # /sysroot/var satisfies var.mount forever — the journal would
      # stay volatile. The bind really lands only because nothing is
      # mounted at /var by the time stage 2 wants it; the journal
      # choreography below handles what journald opened before it.
      neededForBoot = lib.mkForce false;
    };
    # The initrd leaves a tmpfs at /var (stage 1 mounts /sysroot/var
    # writable for its own logging and the mount carries across
    # switch_root). That tmpfs SATISFIES var.mount — systemd sees /var
    # already mounted and never runs the bind — so it must go first.
    # Lazy: journald holds files open on it (volatile so far); its
    # records ride the detached mount and the journal-persist unit
    # below re-homes journald onto the real /var.
    systemd.services.msks-var-unshadow = {
      description = "msks detach the initrd's /var tmpfs so the state bind lands";
      unitConfig.DefaultDependencies = false;
      after = [
        "msks-state-format.service"
        "state.mount"
      ];
      before = [
        "var.mount"
        "local-fs.target"
      ];
      wantedBy = [ "local-fs.target" ];
      path = [
        pkgs.gawk
        pkgs.util-linux
      ];
      serviceConfig = {
        Type = "oneshot";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
      script = ''
        vartype=$(awk '$2 == "/var" { print $3 }' /proc/mounts)
        if [ "$vartype" = tmpfs ] && [ -d /state/var ]; then
          umount -l /var
          echo "msks-var-unshadow: detached the initrd's /var tmpfs; the state bind mounts next"
        fi
        exit 0
      '';
    };

    # The /var bind lands late (state converges in stage 2), so two
    # things need a nudge after it: tmpfiles (rules applied before
    # the bind — sshd's /var/empty is the one that bites — landed on
    # the shadowed mount and vanished), and journald itself (it
    # opened its output against the earlier mount). This unit re-runs
    # tmpfiles idempotently and restarts journald onto the state
    # disk, ordered BEFORE msksd so the daemon's whole lifetime
    # attaches to the restarted journald and reaches the disk.
    #
    # The cost, stated plainly: records from BEFORE the restart ride
    # the detached mount and are gone, and PID 1's unit-lifecycle
    # records ("Started <unit>.service") never re-attach afterwards —
    # PID 1's /dev/log datagram socket does not follow a journald
    # restart. Daemon and service stdout/stderr streams (managed by
    # PID 1) reconnect and persist. The no-restart alternative
    # (volatile-until-flush) was built and measured three ways against
    # this tmpfs-root shape; the flush never landed on the disk — the
    # restart is the design that demonstrably persists.
    systemd.services.msks-journal-persist = {
      description = "msks re-run tmpfiles against the mounted /var and restart journald onto it";
      after = [
        "msks-var-unshadow.service"
        "var.mount"
        "local-fs.target"
      ];
      before = [ "msksd.service" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "oneshot";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
      path = [ pkgs.systemd ];
      script = ''
        systemd-tmpfiles --create
        systemctl restart systemd-journald.service
      '';
    };

    # The journal rides /var on the state disk, bounded: journald
    # rotates its files at SystemMaxUse — the journal cannot eat the
    # state disk.
    services.journald.extraConfig = ''
      SystemMaxUse=512M
      MaxRetentionSec=1month
    '';

    networking.hostName = "msksd-appliance";
    networking.extraHosts = "127.0.1.1 msksd-appliance";
    networking.usePredictableInterfaceNames = false;
    networking.firewall.enable = false;
    networking.useDHCP = false;
    # resolved stays off (the Debian appliance's boot diet masked it):
    # the daemon's forwarder serves the workspaces, and the
    # appliance's own resolver is the static file below.
    services.resolved.enable = lib.mkForce false;
    networking.resolvconf.enable = lib.mkForce false;
    systemd.network = {
      enable = true;
      networks."10-eth0" = {
        matchConfig.Name = "eth0";
        address = [ "${net.address}/${toString net.prefixLength}" ];
        gateway = [ net.gateway ];
        DHCP = "no";
      };
    };
    # The resolver the egress forwarder relays to (#52): the same
    # static file the Debian appliance ships. The forwarder dials its
    # own upstream setting; this file serves the appliance's own
    # lookups.
    environment.etc."resolv.conf".text = "nameserver 9.9.9.9\n";

    # The service identity (#101, verbatim): a dedicated system user;
    # /dev/kvm arrives through the kvm supplementary group (NixOS's
    # udev rule assigns the node to the group — the same rule the
    # Debian build asserted at pin-drift time).
    users.groups.msksd = { };
    users.users.msksd = {
      isSystemUser = true;
      group = "msksd";
      home = "/state/msksd";
      shell = "/run/current-system/sw/bin/nologin";
    };

    # The nested-KVM module for workspace VMs: which flavor loads
    # depends on the host CPU, so a shell picks; udev makes the
    # /dev/kvm node when the module registers.
    systemd.services.msks-kvm = {
      description = "msks nested-KVM module (workspace VMs)";
      after = [ "systemd-modules-load.service" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      path = [ pkgs.kmod ];
      script = ''
        modprobe kvm-intel || modprobe kvm-amd || true
      '';
    };

    # The state disk converges before it mounts (the Debian
    # appliance's msks-state-format.service, NixOS-declared): the
    # prepare script above waits for the disk, labels or formats it,
    # converges var/ staging and the daemon home, seeds the
    # machine-id, and grows an undersized filesystem. Ordered before
    # the fstab-generated state.mount by name, with
    # DefaultDependencies=no — a unit with default dependencies is
    # After=basic.target and cannot run this early without an
    # ordering cycle.
    systemd.services.msks-state-format = {
      description = "msks state disk preparation (blank or foreign disks become ext4; var staging and the service user home converge)";
      unitConfig.DefaultDependencies = false;
      wants = [ "systemd-udevd.service" ];
      after = [ "systemd-udevd.service" ];
      before = [
        "local-fs.target"
        "state.mount"
        "var.mount"
        "shutdown.target"
      ];
      conflicts = [ "shutdown.target" ];
      wantedBy = [ "local-fs.target" ];
      # The convergence tools: the unit runs before the system
      # profile is even assembled into /run/current-system on the
      # earliest path, and a bare service PATH has none of them.
      path = [
        pkgs.e2fsprogs
        pkgs.util-linux
      ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${msksStatePrepare}/usr/local/sbin/msks-state-prepare";
        TimeoutStartSec = "15min";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };

    # The live dev-tree share (#144, verbatim): mounted only when the
    # host attached the share; the unit succeeds either way.
    systemd.services.msks-dev-tree = {
      description = "msks dev-tree share mount (present only when the host opted in)";
      unitConfig.DefaultDependencies = false;
      after = [ "systemd-modules-load.service" ];
      before = [ "msksd.service" ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "/bin/sh -c \"mkdir -p /run/msks-dev-tree && mount -t virtiofs devtree /run/msks-dev-tree 2>/dev/null && echo mounted > /run/dev-tree.state || echo absent > /run/dev-tree.state\"";
        StandardOutput = "journal+console";
      };
    };

    # The debug root shell (#189, verbatim): the state-disk marker
    # puts an interactive root shell on the serial console, which the
    # host's msks-appliance-shell drives through a pty-mode boot.
    # Dev mode only: deployed mode runs sshd (bridge-only, key-only)
    # — a strictly better interactive channel — and keeps the serial
    # log as the read-only evidence surface for boots sshd cannot
    # survive. Gated on the mode so a deployed appliance carries one
    # interactive surface, not two.
    systemd.services.msks-debug-shell = lib.mkIf (mode == "dev") {
      description = "msks debug root shell on the serial console (state-disk marker)";
      wantedBy = [ "multi-user.target" ];
      unitConfig = {
        ConditionPathExists = "/state/debug-shell";
      };
      conflicts = [
        "serial-getty@ttyS0.service"
        "getty@ttyS0.service"
      ];
      after = [
        "state.mount"
        "serial-getty@ttyS0.service"
        "getty@ttyS0.service"
      ];
      serviceConfig = {
        ExecStart = "/bin/sh -c \"exec /bin/sh -i </dev/console >/dev/console 2>&1\"";
        TimeoutStopSec = 1;
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };

    # The daemon (#101's privilege contract, verbatim): ordered after
    # the store mount it lives on, the state disk it serves from, the
    # state-disk preparation (a half-migrated disk must not get a
    # daemon crash-looping against still-root-owned files), and the
    # KVM module its workspaces need. Two ambient capabilities —
    # CAP_NET_ADMIN and CAP_NET_BIND_SERVICE — ambient so the tools
    # and the VMM the daemon execs keep them.
    systemd.services.msksd = {
      description = "msksd appliance daemon";
      documentation = [ "https://github.com/mcdonc/msks" ];
      requires = [
        "nix-store.mount"
        "state.mount"
        "msks-state-format.service"
      ];
      after = [
        "nix-store.mount"
        "state.mount"
        "msks-state-format.service"
        "msks-kvm.service"
        "systemd-networkd.service"
        # Before the daemon: its records must attach to the restarted
        # journald (#218 review — unordered, the attach was a coin
        # flip per boot).
        "msks-journal-persist.service"
      ];
      wants = [
        "msks-dev-tree.service"
        "msks-journal-persist.service"
      ];
      wantedBy = [ "multi-user.target" ];
      unitConfig.StartLimitIntervalSec = 0;
      serviceConfig = {
        ExecStart = "${msksBoot}/usr/local/sbin/msks-boot";
        User = "msksd";
        Group = "msksd";
        SupplementaryGroups = "kvm";
        AmbientCapabilities = "CAP_NET_ADMIN CAP_NET_BIND_SERVICE";
        CapabilityBoundingSet = "CAP_NET_ADMIN CAP_NET_BIND_SERVICE";
        RuntimeDirectory = "msksd";
        Restart = "always";
        RestartSec = 1;
        StandardInput = "null";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };

    # The boot diet, NixOS-edition: the Debian appliance masked units
    # a headless appliance never runs; NixOS ships none of them in
    # the first place. What IS enabled by default and costs boot time
    # or pokes the network for nothing:
    systemd.services.systemd-networkd-wait-online.enable = false;
    services.logrotate.enable = false;
    # The Debian appliance's diet masked both: timesyncd phones a
    # public NTP pool from an appliance with no clock need and adds
    # a mid-boot clock jump; fstrim timers nothing on this layout.
    services.timesyncd.enable = false;
    systemd.timers.fstrim.enable = false;
    documentation.enable = false;

    # Dev mode runs nothing nix-shaped inside: the store is the
    # host's, and the appliance only executes store paths. Deployed
    # mode needs the tools for the update path (nix-copy-closure's
    # remote side, switch-to-configuration) — the daemon SOCKET stays
    # off below.
    nix.enable = mode == "deployed";

    # sshd is the deployed update channel (spike 3's shape): bridge
    # address only, root over keys only, and every session pointed at
    # the overlay store by default (NIX_REMOTE via SetEnv — an
    # explicit assignment in a remote command overrides it, root-only
    # surface). The authorized key arrives through NIX_PATH (the
    # update script seeds it); an absent entry leaves root locked out
    # over ssh, the safe default. Dev mode keeps sshd off entirely —
    # the store is the host's, there is nothing to update over ssh.
    services.openssh = lib.mkIf (mode == "deployed") {
      enable = true;
      # Host keys persist on the state disk: the /etc default is
      # tmpfs, and keys that rotate per reboot break the ssh update
      # channel's host verification (or train operators to accept
      # every rotation — the posture the key-only rule exists for).
      hostKeys = [
        {
          path = "/state/ssh/ssh_host_ed25519_key";
          type = "ed25519";
        }
      ];
      listenAddresses = [
        {
          addr = net.address;
          port = 22;
        }
      ];
      settings = {
        PermitRootLogin = "prohibit-password";
        PasswordAuthentication = false;
      };
      extraConfig = ''
        SetEnv NIX_REMOTE=${overlayStoreUri}
      '';
    };
    users.users.root.openssh.authorizedKeys.keys =
      let
        keyEntry = lib.findFirst (
          p: p.prefix == "appliance-update-key"
        ) null builtins.nixPath;
      in
      lib.optionals (keyEntry != null) [ (builtins.readFile keyEntry.path) ];

    # The deployed store machinery: the overlay store needs its
    # experimental features, and the nix-daemon socket stays off —
    # nix.enable wires nix-daemon.socket into sockets.target, the
    # socket listens from boot, and any local connect would start the
    # root daemon serving a plain-local-store view (the re-send-
    # whole-closure footgun; spike 3's live finding). Nothing on the
    # appliance needs the daemon: store access happens in-session
    # under NIX_REMOTE.
    nix.settings.experimental-features = lib.mkIf (mode == "deployed") [
      "nix-command"
      "flakes"
      "local-overlay-store"
      "read-only-local-store"
    ];
    systemd.sockets.nix-daemon.wantedBy = lib.mkIf (mode == "deployed") (
      lib.mkForce [ ]
    );

    # /nix/var/nix on the tmpfs root would lose the profile — the
    # appliance-side generation pointer — on every reboot; the store
    # volume owns it (spike 3).
    systemd.tmpfiles.rules = [
      # sshd's privilege-separation directory: the state disk's
      # var/ staging predates sshd, and tmpfiles (post-mount) is
      # the deterministic creator.
      "d /var/empty 0755 root root -"
    ]
    ++ lib.optionals (mode == "deployed") [
      "L+ /nix/var/nix - - - - /nix/.upper-volume/nix-var"
    ];

    # switch-to-configuration must exist for nixos-rebuild
    # --target-host boot; microvm.nix disables it when the store is
    # not a share — the deployed store is its own overlay, and
    # updates arrive over ssh, so switching stays enabled.
    system.switch.enable = mode == "deployed";

    # Generation retention as a pinned GC policy (#212 item 6): every
    # boot switch appends a system-N-link on the store volume and
    # nothing deletes them until told to. The trim keeps the last
    # three generations, collects what it released, and runs on a
    # weekly schedule far from any boot or update.
    systemd.services.msks-store-trim = lib.mkIf (mode == "deployed") {
      description = "msks store-volume trim (keep the last three system generations; collect unreferenced upper-layer paths)";
      environment.NIX_REMOTE = overlayStoreUri;
      path = [ config.nix.package ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${storeTrim}";
      };
    };
    systemd.timers.msks-store-trim = lib.mkIf (mode == "deployed") {
      description = "msks weekly store-volume trim";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "Sun 04:00";
        Persistent = true;
      };
    };
    system.stateVersion = lib.trivial.release;
  };
}
