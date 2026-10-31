"""The workspace ssh identity (#111, #486, #497): shape and seed.

The operator's own key is the identity (#486): the client sends
its public half with the create, the daemon validates its shape,
re-annotates it, and stores it — the public half alone — with the
workspace's state; it reaches the guest through the #41 user_data
channel, the cidata seed, so a fresh workspace accepts ssh with no
manual key steps anywhere. msks never mints a key and never holds
a private half anywhere (#497 dropped the pre-#486 escrow — rows a
daemon- or client-side mint wrote against), so nothing crosses
the API but the public line.

Any key type is accepted, at whatever type it carries: the guest's
sshd, the platform's own (#115 posture), stays the authority on
which keys it will authenticate.
"""

import base64
import binascii
import re
import secrets

#: The label charset of an algorithm name: OpenSSH's key types are
#: lowercase token shapes (`ssh-rsa`, `ecdsa-sha2-nistp256`,
#: `sk-ssh-ed25519@openssh.com`, `*-cert-v01@openssh.com`) — letters,
#: digits, dash, dot, at. The charset is not an algorithm policy
#: (any *shape-valid* type passes, #132); it is the guard that keeps
#: the interpolated label inside `seed_script`'s single-quoted
#: assignment — a quote or metacharacter in the label would close
#: the string and run as shell code in the guest's first boot.
LABEL_PATTERN = re.compile(r"[a-z0-9@.\-]+")

#: The workspace's login-user charset (#248): the wire shape the
#: create body's `user` field enforces (ssh names the account in
#: its config alias; the console is a root failsafe, #481). It is
#: also what keeps the
#: interpolated login name inside `seed_script`'s quoted
#: assignments, the same job LABEL_PATTERN does for algorithms.
LOGIN_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def normalize_public_key(line: str) -> tuple[str, str]:
    """Validate one supplied public key line: ``(algo, body)``.

    A supplied line is a key the operator already owns (#132, #486)
    — any key type is accepted, at whatever type it carries: the
    guest's sshd, the platform's own (#115 posture), stays the
    authority on which keys it will authenticate. The daemon checks
    shape only — the label matches the algorithm-name charset, the
    body is base64, and the blob's embedded algorithm name agrees
    with its label. The caller's comment is dropped: the daemon
    annotates provenance its own way.
    """
    fields = line.split()
    if len(fields) < 2:
        raise ValueError("public key line needs an algorithm and a key body")
    algo, encoded = fields[0], fields[1]
    if LABEL_PATTERN.fullmatch(algo) is None:
        raise ValueError(f"public key algorithm {algo!r} is not a valid name")
    check_key_body(algo, encoded)
    return algo, encoded


def check_key_body(algo: str, encoded: str) -> None:
    """The body half of a supplied line: decodes as base64, carries a
    length-prefixed algorithm name that fits the blob and matches
    the label the line gave."""
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"public key body is not valid base64: {exc}"
        ) from None
    if len(blob) < 4:
        raise ValueError("public key body is truncated")
    length = int.from_bytes(blob[:4], "big")
    if length + 4 > len(blob):
        raise ValueError("public key body is truncated")
    if blob[4 : 4 + length] != algo.encode():
        raise ValueError("public key algorithm does not match its key body")


def seed_script(
    public_key: str | None,
    login_user: str | None = None,
    llm_port: int = 0,
    ca_pem: str | None = None,
    mounts_block: str = "",
    timezone: str | None = None,
) -> str:
    """The seeding payload's script half: authorized_keys for root
    and the msks workspace user (#63), written idempotently — and
    the workspace's login user (#248) provisioned the same way when
    it names an account the image does not ship.

    The script opens by waiting for the persistent /home volume's
    mount (#662): everything it writes under /home must land on
    the volume, not on the root filesystem's empty /home directory
    that a still-running first fsck leaves in place (a mount
    arriving later buries the seed under it — on a host slow
    enough to lose the race, the login user loses ssh a few
    minutes into first boot).

    The same mkdir/chmod/append shape #110's smoke planted by hand,
    now the daemon's own first-boot step. The msks user's home rides
    the persistent /home volume (#14): the seed makes it — owned by
    the user, populated from /etc/skel — before any login needs it
    (#171; a bare ``install -d`` of the .ssh path
    would leave the home itself root-owned, because install -d
    applies -o/-g to the final component only). A home that already
    carries dotfiles keeps them (a factory reset preserves the
    user's files), and a key already present is left alone, so a
    re-provision cannot duplicate lines.

    A login user the image does not ship (anything but root and the
    msks account) is created here (#248): ``useradd -m`` makes the
    account and its home, the key lands in its authorized_keys, and
    a sudoers entry carries the same passwordless-root grant the
    msks user holds (#169) — the operator's own account gets the
    workspace-user posture, not a second-class one.

    The daemon's LLM proxy environment rides the same script
    (#259, #483): the profile.d exports that name the proxy on this
    workspace's tap for MSKSWS_*-aware clients — a placeholder key,
    because the proxy authenticates by tap. A port with no
    identity (a pre-#111 row whose seed is healing) seeds the
    proxy block alone.

    The agent toolchain itself is the guest image's, not the
    seed's (#266): the image bakes pinned Node and pi under
    /usr/local and ships the pi model-discovery extension in
    /etc/skel, so the accounts this script provisions copy it
    into ~/.pi/agent/extensions/ with the rest of the skeleton.
    The seed carries only the per-workspace facts the image
    cannot know — the token, the port, the gateway.

    The workspace's interceptor CA rides the same channel (#424,
    the create-time half of #200): every guest trusts the CA its
    own interception path serves, so HTTPS toward allowlisted
    destinations — and the probe service — validates with zero
    manual steps. The PEM's charset (base64, dashes, newlines)
    carries no quote or metacharacter, so the single-quoted
    assignment is safe.

    The workspace's host-share mounts ride the same script
    (#620, part #623): the block writes, prunes, and enables the
    systemd mount units the current mount set names. It rides
    every seed the daemon builds, the mountless ones included —
    the prune half is what removes the last mount's units.
    """
    if public_key is None:
        return keyless_seed_script(llm_port, ca_pem, mounts_block, timezone)
    script = (
        "#!/bin/sh\n"
        "# msks (#111): the workspace identity — authorized_keys\n"
        "# for root and the msks user, planted first boot.\n"
        "set -eu\n"
        f"key='{public_key}'\n"
        + home_mount_wait_block()
        + home_seed_block()
        + "install -d -m 0700 -o root -g root /root/.ssh\n"
        "touch /root/.ssh/authorized_keys\n"
        'grep -qxF "$key" /root/.ssh/authorized_keys '
        "|| printf '%s\\n' \"$key\" >> /root/.ssh/authorized_keys\n"
        "chown root:root /root/.ssh/authorized_keys\n"
        "chmod 0600 /root/.ssh/authorized_keys\n"
        # The workspace home's authorized_keys: the home itself is
        # made by the block every seed carries (below).
        "install -d -m 0700 -o msks -g msks /home/msks/.ssh\n"
        "touch /home/msks/.ssh/authorized_keys\n"
        'grep -qxF "$key" /home/msks/.ssh/authorized_keys '
        "|| printf '%s\\n' \"$key\" >> /home/msks/.ssh/authorized_keys\n"
        "chown msks:msks /home/msks/.ssh/authorized_keys\n"
        "chmod 0600 /home/msks/.ssh/authorized_keys\n"
    )
    return script + riding_blocks(
        login_user, llm_port, ca_pem, mounts_block, timezone
    )


def riding_blocks(
    login_user: str | None,
    llm_port: int,
    ca_pem: str | None,
    mounts_block: str,
    timezone: str | None,
) -> str:
    """The blocks that ride after the identity's own lines: the
    login user's provisioning (#248), the proxy environment
    (#259), the interceptor CA (#424), the mounts (#620, part
    #623), and the captured timezone (#633) — each present
    exactly when its create-time fact is."""
    blocks = ""
    if login_user not in (None, "root", "msks"):
        blocks += named_user_block(login_user)
    if llm_port:
        blocks += llm_seed_block(llm_port)
    if ca_pem is not None:
        blocks += ca_seed_block(ca_pem)
    blocks += mounts_block
    if timezone is not None:
        blocks += timezone_block(timezone)
    return blocks


#: The zone-name grammar the block interpolates: the second wall
#: against a row edited by hand (boot's capture validates the
#: only writer; identity is a leaf the client ships too, so the
#: grammar lives here again rather than behind a server import).
#: Segments of letters, digits, underscore, dash, and plus,
#: joined by single slashes — no dot, quote, backslash,
#: whitespace, or shell metacharacter can pass.
ZONE_RE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*\Z")


def timezone_block(timezone: str) -> str:
    """The #633 block: the host's timezone, captured at create, set
    in the guest on first boot. The zone name's grammar is
    validated here as well as at capture (boot.zone_name owns the
    writer; ZONE_RE is the second wall against a row edited by
    hand), so it interpolates safely into single quotes and the
    zoneinfo path. Every step is best-effort under ``set -eu`` —
    a guest whose /etc is not writable keeps booting and keeps
    the image's default clock: timedatectl applies the zone live
    when systemd's timedated is up (and refreshes Debian's
    /etc/timezone companion), and the plain symlink plus the
    companion write pin the persistent state either way, on
    guests with or without timedated."""
    if ZONE_RE.fullmatch(timezone) is None:
        raise ValueError(f"not a timezone zone name: {timezone!r}")
    return (
        f"if command -v timedatectl >/dev/null 2>&1; then\n"
        f"  timedatectl set-timezone '{timezone}' 2>/dev/null "
        "|| true\n"
        "fi\n"
        f"ln -nsf /usr/share/zoneinfo/{timezone} /etc/localtime "
        "2>/dev/null || true\n"
        f"printf '%s\\n' '{timezone}' > /etc/timezone "
        "2>/dev/null || true\n"
    )


def home_mount_wait_block() -> str:
    """The #662 block: the wait for the persistent /home volume's
    mount, ahead of everything the seed writes under /home.

    The image's fstab mounts the volume ``nofail`` with a 30s
    device timeout — a boot without the volume keeps moving, and a
    boot with one runs the volume's first fsck (fstab pass 2) at
    its own pace. cloud-init runs the seed on its own schedule,
    with no ordering against ``home.mount``: on a host fast enough
    the mount wins the race and the wait is the single /proc/mounts
    read of an already-mounted /home; on a host slow enough (the
    #649 rig's nested-VM first boot runs ~40× slower than native)
    the seed ran first, its writes landed on the root filesystem's
    empty /home directory, and the volume's mount then buried them
    — the login user held a working ssh for the first minutes of
    first boot and lost it the moment the mount completed.

    The wait ends three ways. The volume mounts — the common
    case, one /proc/mounts read and proceed. The guest carries no
    writable virtio disk beyond the root, past a 60s grace — no
    volume was attached at all (the demo-VM boot without one),
    home.mount has failed for this boot, and seeding under the
    root filesystem's /home is the right move: the seed says so
    on stderr and proceeds (the pre-#662 posture, correct
    whenever the volume is not coming). Or 1200 monotonic
    seconds pass (20 minutes) — and a boot whose mount fails
    permanently while the disk is present pays that whole wait
    before seeding (the outcome was decided at the 30s device
    timeout; the wait cannot see it, so it errs toward holding —
    the alternative gave up early and re-created #662 on the
    rig). The bound counts seconds, not polls: a first-boot host
    can be slow enough that each fork in the poll loop itself
    costs seconds, and an iteration-counted cap would stretch
    the same count into tens of minutes of blocked first boot.
    The #662 rig's slowest observed mount landed 29 seconds past
    the 10-minute cap of the day — the cap was measured from the
    seed's start, about 90 seconds into that boot, so the mount
    fell at minute 12 — which is why the bound is 20 minutes; a
    mount slower than that has not been observed on any host.
    The volumeless grace stays a minute: a boot with no writable
    volume disk never mounts /home, and there is nothing to wait
    for.

    The wait is paced by /proc/uptime, not the wall clock: first
    boot is exactly when NTP steps the clock (a forward step
    shortens the wait and can re-create #662; a backward step
    stretches it past any bound), and uptime is monotonic and
    fork-free — a shell-builtin read where an earlier revision
    paid a date fork per poll.

    The volume-present signal is the raw disk, not the
    by-label node the fstab mounts: on a first boot slow enough
    to need this wait, udev's own label probe can trail the boot
    by many minutes (the #662 rig watched the by-label node
    arrive eleven minutes in, the mount a minute behind it) — a
    wait that gives up on the label's absence stops exactly when
    it must keep holding. The raw virtio disk exists from the
    kernel's device scan, seconds into boot: while a writable
    disk beyond the root is present, the volume is attached and
    its mount is merely late, so the wait holds it to the cap.
    The read-only flag in /sys/block tells the volume from the
    seed disk (the only other disk a workspace boots with); the
    scan needs no lsblk, udevadm, D-Bus, or systemd — the image
    side could instead order cloud-final After=home.mount the
    way its own console gettys do, but that ships only in a new
    image: this block rides the seed the daemon builds, so it
    covers every existing image and every renewal with no
    rebuild, and holds even a mount systemd has already failed
    for the boot (an After= ordering is satisfied the moment the
    unit fails). The mount test reads /proc/mounts directly for
    the same reason.

    #686 grows the retry: a boot slow enough that udev's by-label
    symlink lands after the fstab unit's 30s device timeout fails
    ``home.mount`` as a dependency — the disk arrives moments
    later, but systemd never retries a failed fstab mount, and
    the wait would pay its whole cap for a mount one ``systemctl
    start`` away. So the wait, finding the volume disk present
    and the mount still down, asks systemd for the start itself —
    paced at 30s (a fork per poll on a host slow enough to need
    this block is its own tax), best-effort (a guest without
    systemd proceeds as before), and always the start verb: the
    earlier falsified cut queried unit state (a dependency
    failure leaves it inactive, invisible to the query), which
    stays out. The macOS nested stack reproduced the dependency
    failure twice (both image generations); the retry mount took
    under a second both times.
    """
    return (
        "# msks (#662): everything this seed writes under /home\n"
        "# must land on the persistent volume, not on the root\n"
        "# filesystem's empty /home directory that the volume's\n"
        "# still-running first fsck+mount leaves in place — wait\n"
        "# for the mount before writing.\n"
        "# The volume scan: a writable virtio disk beyond the root\n"
        "# means the volume is attached (the seed disk reads RO).\n"
        "vol_present() {\n"
        "for d in /dev/vd[b-z]; do\n"
        '[ -e "$d" ] || continue\n'
        '[ "$(cat "/sys/block/${d##*/}/ro" 2>/dev/null)" = 0 ] '
        "&& return 0\n"
        "done\n"
        "return 1\n"
        "}\n"
        "read -r up _ < /proc/uptime\n"
        "now=${up%%.*}\n"
        "cap=$((now + 1200))\n"
        "devcap=$((now + 60))\n"
        "try=$now\n"
        "while ! grep -qs ' /home ' /proc/mounts; do\n"
        "read -r up _ < /proc/uptime\n"
        "now=${up%%.*}\n"
        'if [ "$now" -ge "$cap" ]; then\n'
        "printf 'msks: the /home volume did not mount within 1200s; "
        "seeding under the root filesystem /home\\n' >&2\n"
        "break\n"
        "fi\n"
        'if [ "$now" -ge "$devcap" ]; then\n'
        "if ! vol_present; then\n"
        "printf 'msks: the guest carries no writable volume disk, "
        "so /home will not mount this boot; "
        "seeding under the root filesystem /home\\n' >&2\n"
        "break\n"
        "fi\n"
        "fi\n"
        "# #686: a boot whose by-label device unit expired before\n"
        "# udev's symlink left home.mount failed as a dependency —\n"
        "# systemd never retries it, the disk is here, so ask for\n"
        "# the start, paced at 30s and best-effort (a guest without\n"
        "# systemd, or a start that cannot succeed, keeps waiting).\n"
        'if [ "$now" -ge "$try" ] && vol_present; then\n'
        "command -v systemctl >/dev/null 2>&1 && "
        "systemctl start home.mount >/dev/null 2>&1 || :\n"
        "try=$((now + 30))\n"
        "fi\n"
        "sleep 1\n"
        "done\n"
    )


def home_seed_block() -> str:
    """The workspace home (#171): install -d both creates the home
    and repairs one a pre-#171 seed left root-owned (it applies
    -o/-g to an existing directory too); the skel copy is skipped
    when the home already has dotfiles, and cp -a preserves the
    skel's root ownership, so the chown -R that follows is what
    hands the copy to the user — the same useradd -m shape. The
    copy is best-effort (set -eu would otherwise abort the whole
    seed on an image without a skeleton): a bare home still starts
    the shell, and the chown runs on whatever is there.

    Every seed carries the block, the keyless ones included: the
    home rides a separate volume that mounts empty over whatever
    the image baked, so a keyless guest needs it exactly as much
    as a keyed one — the msks account's own scaffolding (the pi
    extension, the nix channels on NixOS) arrives only through
    the skeleton copy.
    """
    return (
        "install -d -m 0755 -o msks -g msks /home/msks\n"
        "if [ ! -e /home/msks/.profile ]; then\n"
        "cp -a /etc/skel/. /home/msks/ || true\n"
        "chown -R msks:msks /home/msks\n"
        "fi\n"
    )


def keyless_seed_script(
    llm_port: int,
    ca_pem: str | None,
    mounts_block: str = "",
    timezone: str | None = None,
) -> str:
    """The identity-less seed: the workspace home (#171), the
    proxy-environment block when a listener will serve this
    workspace's tap (a port of zero names a daemon with no model
    list), the interceptor CA block when one rides (#424), the
    mounts block every built seed carries (#620, part #623), and
    the timezone block when the create captured one (#633)."""
    script = "#!/bin/sh\n# msks (#424): the workspace's seed.\nset -eu\n"
    script += home_mount_wait_block()
    script += home_seed_block()
    if llm_port:
        script += llm_seed_block(llm_port)
    if ca_pem is not None:
        script += ca_seed_block(ca_pem)
    script += mounts_block
    if timezone is not None:
        script += timezone_block(timezone)
    return script


def ca_seed_block(ca_pem: str) -> str:
    """The #424/#200 block: the workspace's interceptor CA, staged
    and named so every client family trusts it.

    Every guest stages the certificate under /etc/msks and links it
    into the system trust store when the distro's linker exists
    (Debian's ``update-ca-certificates`` — every client, login
    shell or not). The exports name the CA beside the **platform
    roots**: ``SSL_CERT_FILE`` replaces the OpenSSL/Go default
    lookup, so it points at a bundle built from the platform's own
    bundle — whichever name the distro ships (``ca-bundle.crt``
    NixOS-style, ``ca-certificates.crt`` Debian-style) — with the
    CA appended, and it exports only when that bundle was built; a
    CA-only bundle would break TLS to every real service. Git's
    own ``GIT_SSL_CAINFO`` names the same bundle (it replaces
    git's default CA file, so it must carry the platform roots
    too), and node adds the root itself through
    ``NODE_EXTRA_CA_CERTS``. NixOS's
    ``/etc/profile`` reads no ``profile.d``: the same exports ride
    its sanctioned ``/etc/profile.local`` hook (whose one rule also
    brings the LLM block's exports alive there). Every step is
    best-effort — a guest with no writable target keeps booting and
    says so on stderr."""
    return (
        f"ca_cert='{ca_pem}'\n"
        "install -d -m 0755 -o root -g root /etc/msks\n"
        "printf '%s\\n' \"$ca_cert\" > /etc/msks/interceptor-ca.crt\n"
        # The system-trust link, when the distro has a linker:
        # checked FIRST so a NixOS guest stages nothing under
        # /usr/local that nothing there consumes.
        "if command -v update-ca-certificates >/dev/null 2>&1 \\\n"
        "   && install -d -m 0755 /usr/local/share/ca-certificates \\\n"
        "   && printf '%s\\n' \"$ca_cert\" > \\\n"
        "      /usr/local/share/ca-certificates/msks-interceptor.crt\n"
        "then\n"
        "  update-ca-certificates >/dev/null\n"
        "fi\n"
        # The export bundle: the platform roots under whichever name
        # the distro ships them, with the CA appended — never the
        # CA alone (SSL_CERT_FILE replaces the default lookup).
        "ca_base=''\n"
        "for bundle in /etc/ssl/certs/ca-bundle.crt \\\n"
        "             /etc/ssl/certs/ca-certificates.crt\n"
        "do\n"
        '  [ -r "$bundle" ] && ca_base=$bundle && break\n'
        "done\n"
        'if [ -n "$ca_base" ]; then\n'
        '  cat "$ca_base" /etc/msks/interceptor-ca.crt \\\n'
        "      > /etc/msks/ca-bundle.crt 2>/dev/null || true\n"
        "fi\n"
        "install -d -m 0755 /etc/profile.d\n"
        "cat > /etc/profile.d/msks-ca.sh <<'MSEOF'\n"
        "# msks (#424): name this workspace's interception CA beside\n"
        "# the system roots. SSL_CERT_FILE and GIT_SSL_CAINFO (only\n"
        "# when the bundle was built — they REPLACE the default\n"
        "# lookup, so they must carry the platform roots too) for\n"
        "# the OpenSSL and Go clients and for git's own CA file;\n"
        "# NODE_EXTRA_CA_CERTS for node, which adds the root on top\n"
        "# of its own bundled roots.\n"
        "if [ -r /etc/msks/ca-bundle.crt ]; then\n"
        "  export SSL_CERT_FILE=/etc/msks/ca-bundle.crt\n"
        "  export GIT_SSL_CAINFO=/etc/msks/ca-bundle.crt\n"
        "fi\n"
        "if [ -r /etc/msks/interceptor-ca.crt ]; then\n"
        "  export NODE_EXTRA_CA_CERTS=/etc/msks/interceptor-ca.crt\n"
        "fi\n"
        "MSEOF\n"
        # NixOS's hook: /etc/profile.local. Idempotent by marker.
        "if [ ! -e /etc/profile.local ] || \\\n"
        "   ! grep -q 'for i in /etc/profile.d' /etc/profile.local\n"
        "then\n"
        '  echo \'for i in /etc/profile.d/*.sh; do [ -r "$i" ] \\\n'
        '    && . "$i"; done\' >> /etc/profile.local\n'
        "fi\n"
        "if ! command -v update-ca-certificates >/dev/null 2>&1; then\n"
        "  echo 'msks: the interceptor CA is staged and exported;' \\\n"
        "    'this guest links no system trust store' >&2\n"
        "fi\n"
    )


#: The value the seed exports as MSKSWS_API_KEY (#483): a
#: placeholder, not a credential — the proxy serves the workspace
#: whose tap reached it and reads no credential. It exists for
#: OpenAI-shaped clients that refuse to send requests with an
#: empty key, and for the pi extension's presence check. The
#: guest's pi extension registers the same literal
#: (nix/guest-pi-extension.ts) — two sources by necessity (the
#: seed is Python, the extension is baked TypeScript), one value.
LLM_KEY_PLACEHOLDER = "msks-local-proxy"


def llm_seed_block(port: int) -> str:
    """The #259 block, post-#483: the profile.d script that exports
    the MSKSWS_* client environment — the base URL names the DHCP
    lease's gateway (this workspace's tap address) and the port
    the daemon served at create, so login shells name the proxy
    with zero manual steps. The names carry the msks prefix, not
    the generic OpenAI pair: the proxy is this daemon's own
    service, and a vendor-shaped name would claim otherwise. The
    API key is the module placeholder — the proxy authenticates
    by tap (#483), so the value carries no authority; the heredoc
    is quoted, so it plants unexpanded and computes the gateway
    at login."""
    return (
        # The block owns its profile.d target: the NixOS base ships
        # no /etc/profile.d, and the heredoc below aborts a set -eu
        # seed against a missing directory.
        "install -d -m 0755 /etc/profile.d\n"
        "cat > /etc/profile.d/msks-llm.sh <<'MSEOF'\n"
        "# msks (#259): name the daemon's LLM proxy on this\n"
        "# workspace's tap for MSKSWS_*-aware clients. The proxy\n"
        "# serves the workspace whose tap reached it (#483); the\n"
        "# API key is a placeholder the proxy never reads.\n"
        "gw=$(ip route show default 2>/dev/null | awk '{print $3; exit}')\n"
        'if [ -n "$gw" ]; then\n'
        f'  MSKSWS_BASE_URL="http://$gw:{port}/v1"\n'
        f'  MSKSWS_API_KEY="{LLM_KEY_PLACEHOLDER}"\n'
        "  export MSKSWS_BASE_URL MSKSWS_API_KEY\n"
        "fi\n"
        "MSEOF\n"
    )


def named_user_block(login_user: str) -> str:
    """The seed lines that provision a login user the image does
    not ship (#248): the account (created only when missing, so a
    re-provision or an operator-premade account keeps its uid),
    its home in the #171 shape, its authorized_keys, its shell
    (the image's own workspace user names it — Debian's /bin/bash,
    NixOS's store bash), and membership in the image's admin group
    ``wheel``, which is where the #169 passwordless-sudo grant
    lives: the image declares the grant for the group (Debian's
    ``%wheel`` sudoers dropin, NixOS's declarative rule),
    so the seed never writes sudo configuration — a guest that is
    rebuilt keeps exactly the sudo policy its configuration
    declares. Skipped, with a line on stderr, when the name lands
    on a system account the image ships.

    Ownership rides ``chown user:`` (the colon form names the
    account's login group) rather than install's -o/-g, so an
    account whose primary group is not its own name — one the
    operator's user_data made — still takes its files. The name is
    LOGIN_NAME_RE-validated before it ever reaches this string.

    A name that lands on an account the image already ships with a
    system uid (1-999: Debian's base-passwd carries charset-valid
    names like ``sync`` and ``man``) seeds nothing: a system
    account is not a login, and adding one
    to the workspace group would be a privilege write with no
    login behind it. The block says so on stderr (cloud-init's
    output log, the serial console) and the rest of the script
    still runs.

    A ``useradd`` that fails gets the same tolerance, for the same
    reason the skeleton copy does: an abort here would plant
    the keys and never the account — and the console
    gate would refuse the name forever after. The failure (useradd's own
    stderr and the line above) lands in the cloud-init log, and
    the seeding stands down.
    """
    return "\n".join(
        [
            f"luser='{login_user}'",
            "seed_user=yes",
            # /etc/passwd is parsed directly, not through getent:
            # the seed runs in cloud-init's job environment, whose
            # PATH carries no NSS tool on every image (NixOS's
            # busybox has no getent applet) — and workspaces carry
            # only file-based accounts, the same fact the console
            # helper's own passwd parsing rests on. The name is
            # LOGIN_NAME_RE-validated, so it is safe inside the
            # pattern.
            'luid=$(grep "^$luser:" /etc/passwd | cut -d: -f3)',
            # The image's workspace user names the login shell; a
            # login user gets the same one (fallback: the POSIX
            # default every image has).
            'wshell=$(grep "^msks:" /etc/passwd | cut -d: -f7)',
            '[ -n "$wshell" ] || wshell=/bin/bash',
            'if [ -z "$luid" ]; then',
            # -G joins the admin group; an image without the
            # group still gets the account (without the sudo grant
            # — the membership step below says so on stderr).
            'if ! useradd -m -s "$wshell" -G wheel "$luser" 2>/dev/null; then',
            'if ! useradd -m -s "$wshell" "$luser"; then',
            'printf "msks: login user %s could not be created; '
            'msks will not seed it\\n" "$luser" >&2',
            "seed_user=no",
            "fi",
            "fi",
            'elif [ "$luid" -lt 1000 ] && [ "$luid" -ne 0 ]; then',
            'printf "msks: login user %s names a system account, '
            'uid %s; msks will not seed it\\n" "$luser" "$luid" >&2',
            "seed_user=no",
            "fi",
            'if [ "$seed_user" = yes ]; then',
            # An operator-premade account joins the group too — the
            # grant is the group's, not the account's creation.
            'if ! id -nG "$luser" 2>/dev/null | grep -qw wheel; then',
            'usermod -aG wheel "$luser" 2>/dev/null '
            '|| printf "msks: login user %s could not join the '
            'wheel group; it gets no passwordless sudo\\n" '
            '"$luser" >&2',
            "fi",
            'install -d -m 0755 "/home/$luser"',
            'if [ ! -e "/home/$luser/.profile" ]; then',
            'cp -a /etc/skel/. "/home/$luser/" || true',
            "fi",
            'install -d -m 0700 "/home/$luser/.ssh"',
            'touch "/home/$luser/.ssh/authorized_keys"',
            'grep -qxF "$key" "/home/$luser/.ssh/authorized_keys" '
            "|| printf '%s\\n' \"$key\" "
            '>> "/home/$luser/.ssh/authorized_keys"',
            'chown -R "$luser:" "/home/$luser"',
            'chmod 0600 "/home/$luser/.ssh/authorized_keys"',
            "fi",
            "",
        ]
    )


#: The multipart boundary for a seed that carries both the identity
#: script and the operator's own payload: cloud-init's NoCloud
#: datasource splits the user-data document on MIME parts and runs
#: each per its content type.
MIME_BOUNDARY = "============msks-identity=="


def compose_user_data(
    operator_payload: str | None,
    public_key: str | None,
    login_user: str | None = None,
    llm_port: int = 0,
    ca_pem: str | None = None,
    mounts_block: str = "",
    timezone: str | None = None,
) -> str:
    """The seed's user-data document: what cidata actually carries.

    With no minted key, no proxy environment, no mounts, and no
    captured timezone the operator's payload travels verbatim (the
    #41 contract, unchanged); with a key, a proxy port, a CA,
    mounts, or a timezone and no payload the seed is the seeding
    script alone; with a script and a payload, a MIME multipart
    carries the two as sibling parts. The content type of the
    operator part is sniffed from its first line — the two forms
    the #41 contract documents are a ``#!`` script and a
    ``#cloud-config`` document.
    """
    if nothing_else_rides(
        public_key, llm_port, ca_pem, mounts_block, timezone
    ):
        return operator_payload
    script = seed_script(
        public_key,
        login_user,
        llm_port,
        ca_pem,
        mounts_block,
        timezone,
    )
    if operator_payload is None:
        return script
    return multipart_document(operator_payload, script)


def nothing_else_rides(
    public_key: str | None,
    llm_port: int,
    ca_pem: str | None,
    mounts_block: str,
    timezone: str | None = None,
) -> bool:
    """Whether nothing but the operator's payload rides the seed —
    the #41 verbatim contract's condition: no key, no CA, no
    proxy port, no mounts, no captured timezone (#633). The
    payload itself may be None (no seed at all); the caller
    returns it either way."""
    return (
        public_key is None
        and ca_pem is None
        and timezone is None
        and not llm_port
        and not mounts_block
    )


def multipart_document(operator_payload: str, script: str) -> str:
    """The two-part user-data document: the seed script beside the
    operator's payload, split on a boundary neither contains."""
    boundary = unique_boundary(operator_payload, script)
    parts = [
        f'Content-Type: multipart/mixed; boundary="{boundary}"',
        "MIME-Version: 1.0",
        "",
        f"--{boundary}",
        'Content-Type: text/x-shellscript; charset="utf-8"',
        "MIME-Version: 1.0",
        "",
        script,
        f"--{boundary}",
        f"Content-Type: {operator_content_type(operator_payload)}; "
        'charset="utf-8"',
        "MIME-Version: 1.0",
        "",
        trailing_newline(operator_payload),
        f"--{boundary}--",
        "",
    ]
    return "\n".join(parts)


def unique_boundary(operator_payload: str, script: str) -> str:
    """The multipart boundary, made uncollidable: a payload that
    embeds the standard boundary string gets a random one instead
    (an embedded boundary would split the document mid-payload)."""
    if MIME_BOUNDARY in operator_payload or MIME_BOUNDARY in script:
        return f"==msks-{secrets.token_hex(16)}"
    return MIME_BOUNDARY


#: The first line → MIME type table for the operator part: both
#: cloud-init document forms by their exact label (a whole-line match
#: — "#cloud-config-archive" is a different handler, not a prefix
#: of "#cloud-config"), and a shell script for everything else (the
#: #41 forms are exactly these).
OPERATOR_CONTENT_TYPES = {
    "#cloud-config": "text/cloud-config",
    "#cloud-config-archive": "text/cloud-config-archive",
}


def operator_content_type(payload: str) -> str:
    """The operator part's MIME type, by payload form."""
    first_line = payload.split("\n", 1)[0].strip()
    return OPERATOR_CONTENT_TYPES.get(first_line, "text/x-shellscript")


def trailing_newline(payload: str) -> str:
    """A body that ends without a newline gets one: the closing
    boundary must start on its own line."""
    return payload if payload.endswith("\n") else f"{payload}\n"
