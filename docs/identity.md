# The identity file

Every workspace msks creates plants **your** public ssh key
as the workspace's operator identity. Every `msks ssh` and
`msks rsync` session signs with your private half. msks
generates no key of its own: the key you configure is the one
your workspaces answer to. The same key locks workspace secrets
on the daemon: values a workspace needs at runtime are
sealed under the identity, and the client unlocks them with the
private half again.

## Why an ssh key

The workspace's door is sshd: the guest runs it, the forward
carries it. The operator identity is therefore the credential
that door already speaks, an ssh key. That choice carries four
things with it. The trust root is a key you already guard, in your agent or
on a hardware token. msks mints no account, no password, and
no credential of its own. Nothing new exists to leak, and
nothing new must be rotated: your key's lifecycle is the whole
lifecycle. The public half is all msks ever
stores: planting it on a workspace and sealing secrets under
it keeps every private half where it already lives, on your
machine or in your agent; the daemon's catalog holds public
material only. Attribution comes free: each operator's key
marks which human a workspace answers to, so several operators
share one daemon with separate keys and no shared secret
between them. And the same key seals the workspace secrets
— one root of trust for both the door and the sealed
values, instead of a separate master secret the daemon would
have to keep.

The client names the key in its YAML file
(`~/.config/msks/msks.yaml`) or its environment, with two keys:

```yaml
identity_file: ~/.ssh/id_ed25519 # the private half, read in place
pubkey_file: ~/.ssh/id_ed25519.pub # the public half alone
```

A leading `~` expands; an empty value counts as unset; both are
global-only, and a `daemons:` entry that carries either is
refused, because an ssh identity belongs to you, not to one
daemon connection. Each key has its `MSKSC_*` variable
(`MSKSC_IDENTITY_FILE`, `MSKSC_PUBKEY_FILE`), and the
environment overrides the file. When both keys are set,
`pubkey_file` wins and `identity_file` is never consulted; a
`--pubkey` flag overrides both for one create.

## `identity_file`: the private half

`identity_file` names your private key file, read in place;
msks derives the public half from it at create, and the key
file is never copied into msks's state. Session resolution consults your own ssh-agent first. When
`SSH_AUTH_SOCK` names a live agent holding that key, every
session signs through the agent and the file's encryption stops
mattering. The match is on key material, so the file it came
from and its name do not matter (`ssh-add` it). A key no agent holds is
staged in memory as an unencrypted OpenSSH-format key
(`ed25519`, `ecdsa` P-256/P-384/P-521, or `rsa`); msks never
types a passphrase. An encrypted key no agent holds is refused
with a line naming `ssh-add` as the first fix, then decrypting
a copy, then `--pubkey` at create. An agent that asks for
confirmation per signature (a hardware token's touch) asks at
its own cadence: the first-boot probe's retries and the
session each count as a signing request.

A passphrase-encrypted `identity_file` raises the session's
one passphrase prompt when the daemon's locked secret rows
need it. The TUI asks once when the tree opens, and the answer
unlocks every locked row for the session. Escape or Cancel
leaves the rows locked; they stay named on the secrets page
and re-askable there.

## `pubkey_file`: the public half alone

`pubkey_file` names a `.pub` file, and a create with it set
plants
that one line as the workspace's identity and reads no private
material at all. This is the form for a key you keep
encrypted on disk: the public half sits in the `.pub` file
while the private half answers from your agent.

**The requirement**: your ssh-agent holds the actual key.
`msks ssh` and `msks rsync` on such a workspace authenticate
through `SSH_AUTH_SOCK`'s agent, so `ssh-add` the key in the
shell you run msks from. An agent holding the key makes the sessions work with the
private half never read from disk. A shell whose agent does
not hold it leaves the workspace's key auth without an answer,
and `msks ssh` exits with a line saying the identity resolves
nowhere. The console route still works.

## Where the private half lives at runtime

Three paths cover every session. An agent-held key stays in
the agent: msks asks it to sign, and the file on disk is
never opened. A key msks stages itself runs in a transient in-process
agent that dies with the session. And a session you forward
your agent into (`ssh_options` with `-A`) carries
the agent you named into the workspace. The workspace identity
msks mints stays separate, and your keys arrive only where you
forwarded them.

## The devenv shell's identity

A checkout's devenv shell presets `MSKSC_IDENTITY_FILE` at the
worktree's own dev identity
(`.devenv/state/msksc/identity`), minted passphrase-less the
first time the shell opens, so the smokes and `msks-dev` have
a key without touching yours. The preset fills an unset
variable: a non-empty `MSKSC_IDENTITY_FILE` you exported
before entering the shell survives, and so does the file's
`identity_file` (the environment preset lands only when you
named nothing). Pointing the dev identity at your own key (a
symlink over the minted file) works, with this chapter's
rules: the passphrase prompt appears when the daemon's rows
are locked under it and no agent holds it.

With neither key naming a usable source, `msks create` refuses
with a line saying so; it is the one command that needs the
identity at its start.
