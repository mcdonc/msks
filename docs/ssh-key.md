# Your ssh key

Every workspace carries one ssh public key as its identity: the key
the client sent at create. The guest's first boot writes that line into
`authorized_keys` for `root` and for the workspace's login user. The
daemon keeps the same line on the workspace's row and serves it across
restarts and stop/start cycles. `msks key <workspace>` prints the
stored line.

[First run](first-run.md#the-key-files) shows how to supply the key.
This chapter shows what to do when the key changes: a routine
rotation, a lost or revoked key, a replaced hardware token, or a new
client machine.

## The stored line stays

A workspace's key is fixed at create. The API refuses a change to a
workspace: `PUT` and `PATCH /api/v1/workspaces/{id}` answer 405 with
`workspaces cannot be modified after create` (#41). The stored line
stays for the life of the workspace, and `msks key` only reads it.

Two things follow from that, and the rest of the chapter builds on
them.

- **Login.** The guest's `authorized_keys` files hold the stored key.
  A different key logs in only after you add its line to those files
  by hand. `msks ssh` and `msks rsync` sign with the private half of
  the stored line.
- **Secrets.** Every mint wraps the value to the stored line. A
  hand-added key receives no new mint. [The secrets
  chapter](secrets.md) covers the mint and the unlock.

## Step 1: point the client at the new key

Edit the two keys in the client config (`~/.config/msks/msks.yaml`),
or export their environment forms:

```yaml
identity_file: ~/.ssh/id_ed25519_2026 # the private half
pubkey_file: ~/.ssh/id_ed25519_2026.pub # the public half
```

Change both, or unset `pubkey_file`. `msks create` reads `pubkey_file`
first. It reads `identity_file` only when `pubkey_file` is unset. A
`pubkey_file` still on the old key plants the old key in every new
workspace.

A hardware token's key reaches a workspace through `pubkey_file`
or `--pubkey`, with the token in your agent. msks reads no private
key file a token holds from `identity_file`.

Load the new key into your ssh agent as well. `msks create` reads
the files above. `msks ssh` and `msks rsync` sign through the agent
when it holds the workspace's key, and read `identity_file` otherwise:

```bash
ssh-add ~/.ssh/id_ed25519_2026
```

From then on every `msks create` plants the new key. The TUI's `c`
form and the GUI's Add dialog use the same configured key. The
confirmation prints the file it used:

```text
$ msks create my-workspace --image debian:13 --start
created my-workspace (id 9f2c41ab77)
identity: /home/you/.ssh/id_ed25519_2026.pub (public half)
attach with: msks console my-workspace
```

A create with `pubkey_file` unset prints the `identity_file` path
instead. A new client machine needs the same two settings, and the
same `ssh-add` for the agent. The environment forms are
`MSKSC_IDENTITY_FILE` and
`MSKSC_PUBKEY_FILE` ([the CLI chapter's environment
table](cli.md#client-environment)).

## Step 2: the workspaces created with the old key

Those workspaces keep the old key as their identity. Nothing on the
daemon changes when you edit the client config. Three routes keep you
in, and you can mix them.

### Keep the old key for those workspaces

`msks ssh` and `msks rsync` look for the private half of the stored
line in two places. Your agent comes first. The match is on key
material, so the agent may hold the old key under any file name. The
file `identity_file` points at comes second, and that rung reads
unencrypted files only. An encrypted file there exits with a line
that says `msks never types a passphrase` and lists the ways back
in. With the old key in your agent, both commands keep working on
the old workspaces while `identity_file` already points at the new
key. An old key you keep encrypted on disk stays in the agent for
them.

### Add the new line by hand

`msks console <workspace>` needs no key. The console is the guest's
root autologin on its virtio-console port, reached through the daemon's
authenticated websocket. Inside the session, append the new public
line to the two `authorized_keys` files that matter. `alice` below is
the workspace's login user, the name `msks create --user` recorded.
`msks ls --json` shows it as `login_user`.

```text
$ msks console old-workspace
(old-workspace prompt, as root)
# printf '%s\n' 'ssh-ed25519 AAAAC3Nza… you@client' >> /root/.ssh/authorized_keys
# printf '%s\n' 'ssh-ed25519 AAAAC3Nza… you@client' >> /home/alice/.ssh/authorized_keys
```

Paste the line from the new `.pub` file on the client
(`cat ~/.ssh/id_ed25519_2026.pub`). The image's own `msks` account
holds the stored line at `/home/msks/.ssh/authorized_keys`. Add the
new line there too if you log in as `msks`.

Plain `ssh` through a forward then logs in with the new key:

```bash
msks forward old-workspace 22 --local 2201 &
ssh -i ~/.ssh/id_ed25519_2026 -p 2201 alice@127.0.0.1
```

The `Host msks-*` block in [the networking
chapter](networking.md#the-workspace-ssh-identity) works too. Add a
second `IdentityFile` line to it for the transition.

`msks ssh` and `msks rsync` keep signing with the stored line, the old
key. Both commands read that line from the daemon at session time and
never read the guest's `authorized_keys`. When neither your agent nor
`identity_file` holds the old key, both commands exit with
`this client holds no private half for <workspace>`. Use plain `ssh`
through the forward for that workspace.

### What a re-provision does to a hand-added line

The daemon's seed script runs on every re-provision of a workspace. It
appends the stored line when the file lacks it. It removes no line.
Two events re-provision a workspace:

- **A factory reset** (`POST /api/v1/workspaces/{id}/reset`, see [the
  storage chapter](storage.md#factory-reset)), and a boot that finds
  the root overlay lost (#697). Both rebuild the root overlay from the
  image. `/root/.ssh/authorized_keys` is on the overlay, and it comes
  back with the stored line alone. The login user's file is on the
  persistent `/home` volume. It keeps the hand-added line, and the
  stored line stays beside it.
- **A seed renewal.** A changed mount set or a rotated interception
  CA bakes a new seed with a new instance-id. The overlay stays, and
  both files keep their hand-added lines. cloud-init's own ssh module
  runs on the new instance-id. It keeps the lines a file already
  holds.

A factory reset and an overlay heal rebuild the root filesystem, and
the guest generates new ssh host keys on every image. A seed renewal
makes the guest generate new host keys on the Debian and Fedora
images. The NixOS image runs no cloud-init ssh module and keeps its
host keys. The client rotates its host-key cache with each event's
counter, and the next `msks ssh` re-pins with a notice that says
what caused it. That notice is about the guest's host key ([what
does not change](#what-does-not-change)).

### Or recreate the workspace

`msks rm` and a fresh `msks create` give a workspace whose stored
line is the new key. Copy what you need out of the old workspace
first (`msks rsync`, with the old key in your agent). `msks clone`
does not help here. A clone copies the source's stored line. A
clone of an old-key workspace is an old-key workspace ([the cloning
chapter](cloning.md)).

## Step 3: secrets after the key change

Every mint wraps the value once per covering workspace, to the public
line on that workspace's row. The unlock reads the private half from
`identity_file` and opens the blobs that key opens. A hand-added
`authorized_keys` line changes neither side. New mints for an old
workspace wrap to the old key, and only the old private half opens
them.

The store wraps to two key types only: `ssh-ed25519` and `ssh-rsa`.
A workspace whose stored line is another type receives no blob. An
ecdsa key and a hardware token's `sk-ssh-ed25519@openssh.com` key
are outside the set. An owner-wide mint skips such a workspace, and
a scoped mint refuses with the workspace named ([the mint
flow](secrets.md#the-mint-flow)). A key change to one of those types
keeps every login step above. The new workspaces get no blob: they
cannot unlock a row, and a scoped mint for one refuses. An
owner-wide row still swaps in them while another covered workspace
keeps it warm. An owner-wide mint whose every acceptor is such a
workspace refuses too.

The options depend on whether you still hold the old private half.

- **You keep the old private half.** The old workspaces' placeholders
  stay readable. When `msks secret ls` shows those rows `locked`, run
  the unlock from a client whose `identity_file` is the old key:

  ```bash
  MSKSC_IDENTITY_FILE=~/.ssh/id_ed25519 msks secret unlock
  ```

  The TUI and the GUI run the same unlock with the configured
  `identity_file` alone. Start either one with `MSKSC_IDENTITY_FILE`
  set to the old key for that run. With the config on the new key,
  both leave the old rows locked. The status line reads
  `stay ciphertext (no stanza matches this key)` beside a row that
  opened, and `stay locked — the key opens none of them` otherwise.

  The unlock POST has a second effect. The daemon re-encrypts each
  unlocked row to the row's current recipient set. Owner-wide rows
  cover every workspace you own that accepts owner-wide coverage,
  the new-key ones included. `msks create --secret-coverage scoped`
  opts a workspace out. One unlock with the old key gives those rows
  a blob for each new workspace in the set, when its key type is a
  capable one. A client configured with the new key opens them from
  then on. Scoped rows (`--workspace`) keep the coverage they
  were minted with. A recreated workspace has a new id, and no
  scoped row covers it. Mint its scoped placeholders again.

- **You lost the old private half.** The blobs wrapped to the old
  key cannot be opened. An owner-wide row still has a way forward:
  revoke it and mint it again under the same name. The fresh mint
  wraps to every workspace's stored line of a capable type, and a
  client configured with the new key unlocks it from then on. The
  swap then serves every covered workspace, the old ones included.
  The daemon swaps from the row's cached plaintext, not from a
  per-workspace blob. A scoped placeholder for an old workspace has
  no way forward. A re-mint wraps to the same lost key. Each
  re-mint warms the row for one unlock window. After that nobody
  can unlock it again, because its only blob is wrapped to the lost
  key. Recreate the workspace under the new key
  when it needs that credential.

## Two keys during the transition

- **Both keys in the agent.** `ssh-add` both. `msks ssh` and
  `msks rsync` pick the key that matches the workspace's stored
  line, by key material. The agent is consulted first, and
  `identity_file` holds one key. Point it at the new key, and keep
  the old key in the agent for the old workspaces.
- **The unlock reads one file.** `identity_file` is one private key.
  The old workspaces' blobs open with the old key, and the new
  workspaces' blobs with the new key. Run `msks secret unlock` with
  `MSKSC_IDENTITY_FILE` set to the old key for the old rows, and
  leave the config on the new key for everything else.
- **`--pubkey` for one create.** This form plants that one line,
  whatever the config says:

  ```bash
  msks create ws --pubkey ~/.ssh/id_ed25519.pub
  ```

  During the transition it creates a workspace around either key
  from one client. The confirmation prints no identity line for a
  `--pubkey` create. `msks key ws` shows the stored line.

- **When the old key can be retired.** List the workspaces that
  still hold the old line. Compare the first two fields of each
  stored line with the old `.pub`, because the daemon appends its
  own comment to the stored line:

  ```bash
  old=$(cut -d' ' -f1,2 ~/.ssh/id_ed25519.pub)
  msks ls --json | jq -r --arg old "$old" \
    '.[] | select(((.ssh_pubkey // "") | split(" ")[0:2] | join(" ")) == $old) | .name'
  ```

  An empty list means no workspace holds the old key. Drop it from
  the agent (`ssh-add -d ~/.ssh/id_ed25519`) and archive or delete
  the file. Nothing changes for any workspace.

  A list with names means those workspaces still need the old key
  for `msks ssh` and for their scoped placeholders. Keep the key
  until you recreate or delete them. If you drop it anyway, `msks ssh`
  fails on them, plain `ssh` works only through a hand-added
  line, and their scoped placeholders cannot be unlocked again.
  Their owner-wide placeholders keep swapping while another
  workspace on the row unlocks it.

## What does not change

- **The per-workspace `known_hosts` pin.** That entry is the guest's
  ssh host key, which the guest generates at its first boot. Your key
  is the client's login credential. The two are unrelated, and a key
  change causes no pin notice. A factory reset or a healed overlay
  loss regenerates the host keys. The next `msks ssh` then re-pins
  with a notice that says what caused it. The CLI chapter's
  [`msks ssh` section](cli.md#msks-ssh) has the details.
- **`msks console`.** The console never needed the key. It works
  through every step above.

## A workspace created with `--pubkey`

Its stored line is the supplied key, whatever the client config said
at the time. Everything above applies to it, with the supplied key as
the workspace's key. `msks ssh` signs with it when the agent holds it
or `identity_file` points at it. Secrets wrap to it when its type is
`ssh-ed25519` or `ssh-rsa`. A different key needs the by-hand line
or a recreate.
