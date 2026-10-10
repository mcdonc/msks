# The CLI client

The `msks` command is a thin client for the daemon's `/api/v1` REST
surface. It runs from any host that can reach the daemon — a dev box,
a CI runner — and every command authenticates
with the same bearer token the REST API uses.

The command set covers the operator loop:

```bash
msks                         # the workspace tree TUI (#309)
msks ls                      # what exists, and what state is it in
msks create ws                # make a workspace
msks console ws               # boot it if needed, then work inside it
msks forward ws 22            # bridge a guest TCP port to stdio
msks key ws                   # fetch the workspace's ssh identity
msks rsync ws -- -av ./src/ root@:/src/  # copy files over the forward
msks home export ws           # download its /home volume (backup, seed)
msks start ws                 # boot it without attaching
msks stop ws                  # power it off
msks rm ws                    # delete it (and its data)
```

## Client environment

The client reads environment variables prefixed
`MSKSC_` (client) to stay apart from the daemon's `MSKSD_*` (server)
namespace — a box that runs both can export each side independently.
The list-valued settings (`MSKSC_TERMINAL_OPEN_CMD`,
`MSKSC_SSH_OPTIONS`) carry their string form and are documented with
their file keys below.

| Variable               | Meaning                                                                                                                                                   | Default                          |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| `MSKSC_URL`            | The daemon's base URL                                                                                                                                     | `https://127.0.0.1:8660`         |
| `MSKSC_TOKEN`          | A daemon bearer token (see tokens below)                                                                                                                  | — (required)                     |
| `MSKSC_CAFILE`         | A PEM file to verify the daemon's TLS certificate                                                                                                         | unverified with warning          |
| `MSKSC_EXPECTED_IMAGE` | An image reference the operator sets; `msks ls` compares it with the image the daemon reports in `/health` and names drift (#160)                         | unset (no check)                 |
| `MSKSC_CACHE_DIR`      | The directory per-workspace host-key caches live under (#251); the per-workspace directories are created below it                                         | `~/.cache/msks`                  |
| `MSKSC_DATA_DIR`       | The directory client state lives under (#251) — per-workspace identity files a pre-#486 client wrote live here; same naming rule                          | `~/.local/share/msks`            |
| `MSKSC_IDENTITY_FILE`  | Your own private key file — the ssh identity `msks create` plants into every workspace (#336, #486; required, msks generates none); a leading `~` expands | unset (`msks create` refuses)    |
| `MSKSC_TERMINAL_TITLE` | The title template for the workspace-shell windows the workspace page opens (#445); `{workspace}` resolves to the workspace's id                          | unset (the terminal's own title) |

The two directory variables are separate because their contents
differ in durability: the host-key cache is disposable (a swept
cache costs one trust-on-first-use re-pin), while the identity
files a pre-#486 client wrote beside it have no other copy —
losing one loses ssh to that workspace. Pointing the cache at a
per-project, disposable location and the data at somewhere durable
is the intended use; one variable for both would tie their
lifetimes together.

Each names its directory directly — the per-workspace directories
are created below it — and takes an absolute path (a relative
value is refused with a line naming the fix; a leading `~`
expands; an empty value counts as unset).

`MSKSC_IDENTITY_FILE` names a file, not a directory: your own
private key, read in place. msks derives the public half from it
at create and stages the private half in memory for `msks ssh`
and `msks rsync` — the key file
itself is never copied into msks's state. The key must be an
unencrypted OpenSSH-format key msks can stage (`ed25519`, `ecdsa`
P-256/P-384/P-521, or `rsa`): msks never types a passphrase, and
a key outside that set is refused with a line naming the fix
(keep such keys with ssh-agent, or plant the public half with
`--pubkey`).

A missing `MSKSC_TOKEN` is an error before any network activity: the
client names the variable and exits. Tokens come from the daemon:
`POST /api/v1/tokens` mints one, and the devenv environment presets
all three from the worktree's dev-daemon state dir,
`.devenv/state/msksd/` (token + CA) — once the dev daemon has
served once, a fresh devenv shell needs no exports. The same shell
presets the two directory variables at the worktree's own
`.devenv/state/msksc/` (cache under `cache/`, identities under
`data/`), so each checkout's client state stays its own — one
tree's cache entry is never read by another (#251); a non-empty
value exported before entering the shell survives the preset, and
an empty one counts as unset. Workspaces created before the preset
keep their identity files under the previous root
(`~/.local/share/msks/<id>/`): inside a devenv shell, either move
that workspace's directory under the worktree's
`.devenv/state/msksc/data/` or unset `MSKSC_DATA_DIR` — the ssh
error names the path it looked in.

```bash
msks ls        # presets: https://127.0.0.1:8660, the worktree's
               # token, and .devenv/state/msksd/msks-ca.pem
               # (verified TLS)
```

Targeting a hand-run bare-host daemon instead (#146 — no managed
process; `msks-dev-ready` then `msksd` from a shell) takes the
explicit pair from ITS state, with its own CA:

```bash
export MSKSC_URL=https://127.0.0.1:8660
export MSKSC_TOKEN=$(cat .devenv/state/msksd/bootstrap-token)
export MSKSC_CAFILE=.devenv/state/msksd/msks-ca.pem
```

The daemon serves TLS with a self-signed certificate. Point
`MSKSC_CAFILE` at the daemon's CA (`msks-ca.pem` under its state
directory) and the client verifies the certificate chain. Without
`MSKSC_CAFILE` the client proceeds unverified and prints a warning to
stderr on every invocation — the same trust-on-first-use posture as
`msks console` (#21), fine for a lab network and worth closing before
anything real.

## The client config file (#314)

The environment variables above have a durable home: a YAML file at
`~/.config/msks/msks.yaml` (relocated by `MSKSC_CONFIG_DIR`, or
`XDG_CONFIG_HOME`). The variables keep working beside it, and each
one overrides the same key in the file — a fresh devenv shell with its
presets exported behaves exactly as it does today. Precedence,
highest first:

```text
--daemon flag > MSKSC_* environment > the file's daemon aliases
> the file's global keys > built-in defaults
```

A bare `msks` reads the default path and, on the first run, generates
a commented near-empty template there (the directory 0700, the file
0600). `--config <path>` reads exactly that file — a missing one is an
error, and an explicit path is never generated — and `--config=none`
reads the environment and the built-in defaults only.

Every scalar key is its `MSKSC_*` variable with the prefix stripped
and lowercased (`MSKSC_URL` → `url`,
`MSKSC_EXPECTED_IMAGE` → `expected_image`), with the same semantics
as the variable: a leading `~` expands, and a key set to nothing
(`key:` with no value, or `""`) is the unset form. Hyphens and
underscores spell the same key — `token-file` and `token_file` are
one key (klangk's file spelled kebab; msks's variables spell snake),
and a file that carries both spellings of one key is refused, the
second treated as a duplicate. `token_file` is
the one key whose variable carries a different shape: the file
points at a file holding one daemon token, so the config tree stays
free of inline credentials and the token keeps the permissions of
the file that holds it — `MSKSC_TOKEN` still carries an inline token
when the environment is the more convenient place for one. For the
invocation, the token is read out of its file into the process
environment (the substrate every reader already speaks), where
child processes — console shells, the terminal launcher — inherit
it; the file itself stays the durable, permissioned home. An
unreadable or empty token file is an error before any network
activity, naming both places a token can come from.
`identity_file` (#336) follows the `token_file` pattern for a
different secret: it names your private key file by path (read in
place, never copied — see the create default below), and it is
global-only — a `daemons:` entry that carries it is refused,
because an ssh identity belongs to you, not to one daemon
connection.

Unknown keys and duplicate keys are refused at load, each error
naming the key and the valid ones — the same fail-fast rules msksd's
config file carries (#46).

### Daemon aliases

The file's one structural key is `daemons:` — one entry per daemon
you talk to. `url` is the entry's one required key; `token_file`,
`cafile`, and `expected_image` override their global keys for that
alias:

```yaml
url: https://127.0.0.1:8660
token_file: ~/.config/msks/dev.token
daemons:
  dev:
    url: https://127.0.0.1:8660
    token_file: ~/projects/msks/.devenv/state/msksd/bootstrap-token
    cafile: ~/projects/msks/.devenv/state/msksd/msks-ca.pem
  lab:
    url: https://hv-1.lab.example.com:8660
    token_file: ~/.config/msks/lab.token
    cafile: ~/.config/msks/lab-ca.pem
    expected_image: msks/debian13:1.1

active_daemon: dev
```

`active_daemon` names the alias a bare `msks` addresses; the
`--daemon <alias-or-url>` flag picks one for a single invocation and
also takes a raw URL. The two selections sit at different layers of
the precedence rule: a `--daemon` alias outranks the environment for
that invocation (you named the daemon, so its entry's keys win over
the ambient exports), while an `active_daemon` alias sits below the
environment (the devenv presets keep winning, so a worktree shell
still talks to its own dev daemon beside a user config file that
names another).

### `terminal_open_cmd`

`terminal_open_cmd` names the launcher the workspace page's
**Open a shell (new terminal)** action uses to open a workspace
shell in a new terminal window (#314, #341); the msks invocation
is appended after it, the way most terminals take a command after
`-e` / `--`:

```yaml
terminal_open_cmd: konsole -e          # string form (shell-split)
terminal_open_cmd:                     # list form (no shell quoting)
  - alacritty
  - -T
  - msks ssh
  - -e
```

`MSKSC_TERMINAL_OPEN_CMD` overrides the file value with the string
form. The built-in default is `xterm -e` — the terminal most
Linux distributions carry — so the new-terminal path needs no
configuration; a launcher that fails to execute (a binary that is
missing or not executable) names its reason on the tree's first
flash after the same-terminal shell ends, and the shell runs in
the same terminal. A holding variant
(`konsole --hold -e`, xterm's `-hold`) keeps the window open after
the session ends, for reading final output; without one the window
closes itself when the session disconnects.

### `terminal_title`

`terminal_title` names the title of every terminal window the
workspace page's new-terminal shell action opens (#445), with
`{workspace}` resolved to the workspace's id — the token
`msks ssh` addresses, because that is what the TUI appends to
the launcher — or `shell`, when the appended command names no
workspace:

```yaml
terminal_title: msks — {workspace}
```

Two paths write it. A launcher that runs `msks-term-popup`
writes the title before it attaches tmux, and the launch's own
tmux server pins `set-titles` off, so the title stays for the
window's lifetime — a workspace shell inside the window cannot
rewrite it. Every other launcher gets the title from the
appended `msks ssh` itself: the window msks opens is marked at
spawn, and the command names it before the session starts. A
workspace that sets its own title from inside the session (a
`PROMPT_COMMAND` with a title escape, the way stock ssh sessions
can) rewrites it there — tmux shields the popup path, and nothing
shields the plain path.

`MSKSC_TERMINAL_TITLE` overrides the file value. The default is
unset: the terminal emulator's own title — the command it runs,
or whatever its `-T` / `--title` flag names — stays in place.
A `msks ssh` you type yourself keeps your terminal's title: the
setting applies to the windows msks opens, and a same-terminal
shell (the dead-launcher fallback) changes nothing.

### `ssh_options`

`ssh_options` remembers ssh options for every ssh session msks
runs — `msks ssh`, the workspace page's new-terminal shell action
(which runs `msks ssh` in the new window), and `msks rsync`'s ssh
transport (#385). The value is the ssh options you would type
after `--` on the `msks ssh` command line — the options
themselves; a `--` token in the value is refused, because msks
adds the options to its own and a remembered separator would turn
msks's transport into the remote command:

```yaml
ssh_options: -A -o ServerAliveInterval=30   # string form (shell-split)
ssh_options:                                # list form (tokens stay whole)
  - -A
  - -o
  - ServerAliveInterval=30
```

`MSKSC_SSH_OPTIONS` overrides the file value with the string form.

The remembered options ride behind the command line's own, so an
option you pass for one invocation wins over the remembered one —
and msks's own transport settings (the forward as ProxyCommand,
the per-workspace known_hosts, the identity) stay defaults beneath
both, the same first-obtained precedence stock ssh applies. Agent
forwarding gets the one extra rule the ordering alone cannot give:
when the command line names any forwarding setting (`-A`, `-a`, or
a `ForwardAgent` value), the remembered forwarding options are
dropped for that invocation and the command line decides alone —
otherwise a remembered `-A` would reassign a typed `-a`, because
ssh's flags assign in order. A remembered `-A` forwards your
agent, the one `SSH_AUTH_SOCK` names, exactly as a typed one does
(#174) — the guest receives your keys, not the workspace
identity.

The remembered options carry the command-line passthrough's full
power, including the ability to override msks's transport for
every session (a remembered `ProxyCommand=`, `UserKnownHostsFile=`,
or `IdentityAgent=` re-routes or re-keys every ssh msks runs). The
command-line passthrough has the same power per invocation; the
remembered form persists it, so the key is the one to check first
when a remembered option breaks the seam.

`msks console` and the workspace page's same-terminal shell are
websocket sessions, not ssh — `ssh_options` applies to the ssh
surfaces above and leaves them untouched.

### `msks-term-popup`: the consent-decider terminal (#379, #467)

`msks-term-popup` is a shipped client command that runs the
appended shell inside a local tmux session and answers the
workspace's egress consent prompts right there: the launch also
starts the consent-decider app in a hidden tmux session on the
same socket, where it holds the workspace's decider registration
for the window's whole life. The app lists every hold the
workspace carries — destination and time remaining per row —
with the arrow keys moving the selection: `a` allows until
restart and `d` denies the selected hold, while the uppercase
twins `A` and `D` pick a duration for their verdict (Enter picks,
Escape cancels). `m` switches the workspace's egress mode
(#465): the picker offers `allow`, `static`, and `interactive`
with the current mode highlighted (Enter picks, Escape or `q`
cancels), and picking `static` while nothing is effectively
allowed asks the empty-static question first — the same
confirmation the consent page asks. The switch goes through the
same endpoint `msks egress mode` speaks; the status line names
the current mode beside the connection state, a landed switch
flashes its effect there (`in effect now`, or `takes effect at
next start` for a stopped workspace), and a switch the daemon
refuses names its reason on the same line. A hold leaves the
list the moment the daemon resolves it — a verdict from another
decider window or the hold's own timeout — and the popup viewer
hides itself when the list empties.

A hold arriving with the popup closed raises a popup viewer
over the shell that attaches to the hidden session, and the
bindings map rides the popup's bottom edge: the verdict keys,
the mode switch, and the hide/show entry. `q`, `Esc`, or `C-b`
hide the viewer
— the decider keeps running, its queue keeps living, and `C-b p`
brings the popup back; a popup the app raised returns the same
way on the next hold. The session ends with its window, and the
decider registration ends with the server. It works with any
terminal that runs a command: name your terminal and its command
flag in `terminal_open_cmd`, and `msks-term-popup` right after it
(tmux 3.2 or newer must be on PATH):

```yaml
terminal_open_cmd: konsole -e msks-term-popup
terminal_open_cmd: xterm -e msks-term-popup
terminal_open_cmd:                     # list form (no shell quoting)
  - alacritty
  - -T
  - msks ssh
  - -e
  - msks-term-popup
```

The popup viewer keeps its own attach to the hidden session, so
a hold that arrives while it stands is already in the list, and
verdicts posted from it land through the same REST contract as
the consent page — a post that cannot land (the request resolved
in another decider window while the verdict was in flight) names
its reason on the popup's status line. The window the prefix
opens takes its title from
`terminal_title` (`msks — {workspace}` resolves the workspace's
id into it, #445). The session's own status bar names the
workspace too (#455): the launch resolves the workspace's name
and pins it to the status bar's left side — `[project-x]` — with
the id standing in whenever the lookup cannot land (the daemon
down, the workspace gone, the wait run out) and `shell`
for a plain shell; the window list after the label stays empty
(#458), and so does the bar's right side, which tmux's default
fills with the pane's title, the clock, and the date — the
label is the whole bar.
The hidden consent session runs on the launch's own socket — one
small server per window — and the app retires when the window
has been gone for a stretch, taking the session and the server
with it; the window's own
hold flags (`konsole --hold`, xterm's
`-hold`) keep the window open after the session ends as they do
for a plain shell window.

The pane keeps 10000 lines of scrollback and takes the mouse
wheel (#434): wheel up scrolls into the pane's history, wheel
down scrolls back toward the live output, and reaching the bottom
— or pressing `q` — returns to the plain shell. The keyboard path
covers the same history: `Ctrl-b [` (or `Ctrl-b PageUp`) opens
tmux's copy mode, where the arrow keys and PageUp/PageDown move
through it. `Shift-PageUp` and `Shift-PageDown` page through it
too, one page per press, entering copy mode on the way up and
returning to the shell at the bottom (#444); a terminal that
keeps the shifted pair for its own view (its scrollback has
nothing to show on this pane) still has the wheel — Konsole can
hand the pair to the pane with a keytab rule scoped to the
alternate screen, sending `\E[5;2~` and `\E[6;2~`. The bare
PageUp/PageDown reach the shell untouched. These settings — the
status bar's name among them — ride
the window's own tmux server (the
launch's dedicated socket); an operator's own running tmux server
keeps its own settings. With tmux's mouse mode on, a plain drag
selects text in tmux's buffer, and holding Shift while dragging
selects the terminal's native text (most terminals hand Shift
drags to their own selection). A consent popup raised while the
pane is scrolled still takes its verdict keys: the popup overlays
the scrolled pane, and the next keypress answers it.

## Workspace identity: a name and an id (#246)

A workspace carries two identity fields. The **name** is the label
you choose at create — the positional argument of every command,
unique among the daemon's workspaces. The **id** is minted by the
daemon at create (10 hex digits — 5 random bytes, re-rolled until
it answers to nothing on this daemon), immutable, and never reused
while its workspace lives: every host-side surface — the artifact
directories under the state
dir, the catalog row, and the client-side caches — keys on the id.
Deleting a workspace and creating another under the same name
yields a different id with nothing of the first instance left to
collide: the second workspace's `msks ssh` works on the first try,
with a fresh host-key cache of its own.

Every command that takes a workspace accepts either reference —
`msks console ws` and `msks console 1a2b3c4d5e` reach the same
workspace. The name is the everyday reference; the id is the
reference no other workspace shares.

## `msks` — the workspace tree TUI (#309)

A bare `msks` — no arguments at all — launches the full-screen
workspace tree (`msks tui` names the same command, and `msks tui
my-workspace` opens that workspace's page directly). The tree is
rooted at the **workspaces list**: every workspace one row —
newest first, the CREATED column's arrow naming the sort (#470)
— with creating, starting, stopping, and removing on its keys
(`c` new, `s` start, `x` stop, `D` remove — asked and confirmed —
`e` secrets, `r` refresh, `/` a typeahead that jumps the focus
to the first row whose name starts with what you type, and
Enter opens the workspace's page). The list re-reads the daemon
every five seconds, so a workspace another surface moved — the
CLI, another operator — appears without a keypress, and a power
verb the focused row's own status makes pointless names its
skip client-side without a round-trip. A refused create
opens the failure panel (#426): a centered panel over the list
carrying the daemon's refusal verbatim beside the name the form
submitted, waiting for dismissal — Enter, Escape, `q`, or the
Close button — so the detail stays on screen while the operator
reads it; closing it returns to the list, and a create that
lands keeps today's flash note. `e` opens the
**secrets** page (#431). Each row's status
column carries the state's color behind a filled cue dot (#348
and #470): a running workspace in the theme's success color,
every other state — stopped among them — in the warning color —
the rest of the row keeps the default foreground, the focused
row's name takes a bold cue, and the colors follow the active
theme.

The listing renders inside a rounded frame — the same framing
the create form carries (#349) — so the rows read as one table
between the status bar and the footer. The status bar above it
sets the workspace count in the bold default foreground with the
daemon's URL riding after it in muted text (a long URL clips at
its middle, both ends kept, so the line stays one row); a
flash message owns the whole line while it lives, and a flash
longer than the terminal's width crops at the edge with an
ellipsis marking the cut, so the bar stays one row whatever a
refusal echoes back (#359). Each row's created column reads a
relative label (#350), bucketed by whole calendar days:
`today`, `yesterday`, then `2d ago` under a week, `3w ago` under
a month, `1mo ago` under a year, `1y ago` past it — the absolute
created date reads on the workspace page's header.

A placeholder row is a daemon-wide object (its coverage set
crosses workspaces, and the daemon-wide row belongs to none of
them), so the secrets page lives at the tree's level: `e` on the
workspaces list opens it.

The **secrets page** lists every placeholder row the daemon
holds, in the listing's own five-column shape: coverage (`*` for
the daemon-wide row, the workspace ids scoped), name,
destinations, the remaining lifetime, the created date. The
lifetime cell is a live countdown — `2h`, then `1h`, repainted
every second — and reads `never` on an unbounded row. `x` revokes
the focused row after a confirmation that names what a revoke
does (the row retires everywhere at once); `r` opens the duration
picker over TTL-appropriate choices — an hour to thirty days,
the choice nearest the row's remaining lifetime highlighted —
and extends the row in place: the sentinel and the row's identity
stay as they are. `e` opens the daemon-wide audit view: the
newest hundred recorded mints, revokes, and expiries replayed
from the audit table at open, newest first, then the wire events
streaming in live beside them, with the off-allowlist sighting
highlighted as the exfil signal and `k`/`w` picking the kind and
workspace filters (a daemon-wide row's events cover every
workspace, a scoped row's its members). The audit view holds a
plain subscription — no decider registration — so it claims no
holds.
`c` opens the mint form (#393): name, repeatable destinations
(an exact host or a label-anchored suffix, comma-separated in
one field), coverage — the daemon-wide row, or a multi-select
fed by the tree's own workspace list — the lifetime (`unbounded`
by default, an hour to thirty days beside it), and the value: a
masked field the operator types or pastes into — the form takes
the secret's bytes directly. The submit checks the secret store
first (`msks secret
check`'s endpoint), then mints; a refused mint opens the failure
panel (#426) over the form — the daemon's refusal verbatim beside
the name the form submitted — and a refused store check opens it
with the daemon's refusal alone; either way it closes by hand
(Enter, Escape, `q`, or the Close button), and the fields
stay filled for a retry once it closes. A mint
that lands replaces the form
with the sentinel's panel: the sentinel opens masked — its
prefix names the reach (`mskssec1_` scoped to the chosen
workspaces, `mskssec2_` every accepting workspace), the body
renders as bullets — with an `s` Show action that reveals and
hides it again, and a `c` copy
action that writes the sentinel to the terminal's clipboard over
OSC 52 — the copy path a terminal that honors the sequence
answers, over ssh included, with no reveal needed — and the closing
rule: the display ends with the panel. Enter (or a click) on a
listing row opens the panel again (#440): the page fetches the
row's sentinel from the daemon and shows it with the same show
and copy actions — the sentinel reads back on demand, at mint
and after.
Closing the panel clears its text, and the minted row
stands on the page's list. The placeholder-to-workspace
navigation lands with the cross-references (#394).

The **workspace page** carries the per-workspace loop: a
two-line header — the workspace's name on the first line with
its status beside it in the state's color (the same coloring the
list uses), the id, image hash, host, and created date muted on
the second, its separators carrying the same theme-muted span
style the header's status takes — a status line for egress
consent (the mode, then one granted scope named with its expiry;
two or more grants read as a count with the nearest expiry — `5
grants · next expires 4h` — so the line stays readable at 80
columns, every grant listed on the workspace's egress consent
screen), and the page's actions centered in the space the header
lines and the footer leave — the block capped at 64 columns — in
three groups: a shell in a new terminal
window, then egress consent and an edit dialog for the sizes and
topology (#331), then start and stop. Each action paints its
name with its description muted behind it, and the row Enter
acts on carries a marker beside the list's own highlight and the
name in bold. The start and stop rows dim behind their reason
while the workspace's status makes the verb pointless — a stop
on a stopped workspace, a start on a running one — and Enter on
a dimmed row names that reason on the consent line; the bold
sits on the rows the state invites (the shell in every state —
its window's `msks ssh` child boots a stopped workspace itself,
so the action never dims — and Start while the workspace sits
stopped), and `s`/`x` run start and stop from wherever the
focus stands — the letters the list binds. The page re-reads
the workspace each second, so the dimming follows a start or
stop made anywhere, not only on the page. A workspace
the listing no longer sees — removed from another surface —
closes its page and names the removal on the list's status
line. The
header's first line also counts pending holds (`egress to
decide: N` while any hold waits, refreshed each second). The
header's meta line clips each field to its own budget — the id,
the image hash, the host — so the created date stays whole at 80
columns and the metadata owns its own line: a name truncates
only when it alone no longer fits the line — and a flashed
refusal on the consent line truncates at the edge the same way,
so the page's layout holds steady. A workspace in `interactive`
mode holds new flows while the page is open: the page registers
as the workspace's decider, so holds land on it.

The page's **Egress consent** action and its `e` key
(#358, #454) open the egress consent page: a full-screen visit
holding the held-request queue and the in-effect verdicts
together — the consent decider as a page inside the tree, and
the only place verdicts are made. The page carries the same
two-line header as the workspace page beneath it (#460) — the
workspace's name with its status (and the pending-hold count
while any hold waits), then the id, image hash, host, and created
date muted — so the workspace's identity stays visible while
verdicts are made. A hold's arrival pushes nothing: the
header's `egress to decide: N` counts what waits on both pages'
headers (a hold waits
about two minutes before it times out denied), and the
workspace page's consent line beneath flashes the held
destination with the key in — a burst of
first-seen holds names its count instead, and a hold that lands
while another screen owns the terminal flashes on the first tick
after that screen leaves. Keys on the
page: the arrows walk the held requests and cross into the
verdict rows beneath (and back), `a`/`d` allow or deny the
focused hold for the default duration (`tilrestart`), `A`/`D`
pick a duration first (`once / 5m / 15m / tilrestart /
forever`) — the letters act on the holds alone, and a
mis-zoned press names the zone it acts on instead of deciding
silently — `x` revokes the focused verdict (the
row leaves on the daemon's refreshed frame, never
optimistically), and `m` opens the mode picker (`allow` /
`static` / `interactive`, the current mode highlighted) — the
switch's one path since the workspace page's own mode action
left (#460): picking a mode switches the posture through the same
endpoint `msks egress mode` speaks, without leaving the page.
The status line names the new mode as the switch lands; a pick
of `static` with nothing effectively allowed asks the same
offline-workspace confirmation the CLI asks (confirm to switch,
decline to decide nothing), and a refused switch names its reason
on the status line, where the operator reads it. Enter on a
focused hold opens the duration picker — the key answers, the
pick decides; only an explicit letter or a picked duration ever
decides — and while no hold waits the verdict keys stand inert
and leave the footer until one lands. The page's
keys live on the page itself, so they cannot collide with the
workspace page's. `q` or Escape returns to the workspace page
(holds keep waiting, the header's count keeps counting, `e`
reopens). The placeholder-token audit lives on the secrets page
(#390): the consent page keeps the holds, the verdicts, and the
mode. See `msks egress` below for what verdicts and rules
cover.

The page's **Edit settings** action (#331) opens the edit
dialog: the create form's own layout, seeded with the
workspace's current values. The sizes and topology (root, home,
vcpus, memory) are editable — a submit sends the changed values
through the same endpoint `msks resize` speaks, and the page's
consent line carries the same outcome line the CLI prints,
including what waits for the next boot. A workspace that is
running when Apply lands asks first (#380): confirm and the page
stops the workspace, then applies the resize — decline and the
workspace keeps running with its sizes as they were. Home bytes
move at once, and root growth and the new topology apply at the
next boot; a resize the daemon still refuses — a root shrink,
among them — names its reason on the consent line. The name,
image, and user fields show their current values read-only,
marked `*` as create-time — changing
them is a delete-and-recreate, and the dialog refuses a changed
value with a note naming the field instead of accepting it
silently. A size left blank keeps its current value. Escape
closes the dialog and decides nothing.

The page's **Open a shell (new terminal)** action spawns the
configured terminal launcher with a `msks ssh` invocation
appended (see `terminal_open_cmd` below), and the tree keeps
running beside the window — the spawned shell inherits the tree's
resolved connection, so it reaches the same daemon, and it rides
ssh rather than the console because a fresh window gets resized:
the console keeps the getty's geometry, while ssh carries every
resize to the guest. The action runs on a stopped
workspace too: the spawned `msks ssh` boots the workspace itself
(it carries the workspace's id, and the window reads the pre-flight's
`msks: <id> is stopped; starting it`) and the same-terminal fallback
below boots the same way. A launcher that cannot start — a missing
binary, one without the execute bit — names its
reason on the tree's first flash after the shell hands the
terminal back, and the shell runs in this terminal instead: a
chained `msks console` session.

Every screen walks with the arrow keys alone: lists move with
up/down, the create form's fields move with up/down between them,
and Escape leaves the screen it is on (`q` backs out of a page;
at the workspaces list, Escape is the tree itself quitting).
Ctrl+Q exits the client from any screen (#442) — the tree, a
page, a form field, or a panel stacked over them — the same
clean exit `q` takes at the workspaces list, and it takes
priority over the form field's shortcuts while a form field
holds the focus. A bare Ctrl+C keeps the client running (#442):
a form field holds it as the field's own copy shortcut, and
the tree's own screens answer it with a notification naming the
quit key. A stacked panel away from its fields — a
confirmation, a picker, the sentinel
panel — leaves the key silent: nothing exits, nothing copies,
nothing shows, the stock Textual modal behavior. Ctrl+Shift+C
belongs to the terminal (#437): the TUI starts with the kitty
keyboard protocol off, so a terminal that binds the combo to
its own copy handles the gesture and the TUI never sees the
key. A terminal that passes the key through sends the same byte
as Ctrl+C, and the tree's own screens answer it with the
notification — no path of the gesture exits the client. The TUI speaks the same REST
surface the `msksc` commands speak (`MSKSC_URL`, `MSKSC_TOKEN`,
`MSKSC_CAFILE`) and reads no daemon state directly.

## `msks ls`

Prints one line per workspace the daemon knows, aligned in five
columns: name, id, status, image hash (first 12 hex chars), and
owning host. The columns are measured against the values present
(#271): a long name or image reference widens its column for every
row, so each row's columns start at the same offsets — the header
row included.

```text
$ msks ls
name          id          status   image         host
my-workspace  1a2b3c4d5e  running  9f2c41ab77de  hv-1
scratch       77eedd0199  created  -             hv-1
```

The status column speaks the daemon's lifecycle vocabulary —
`created` (row exists, never booted), `starting`, `running`,
`paused`, `stopped`, `unknown`, `absent`. A `-` in the name column
is a workspace without a label — created nameless through the API,
or before the id/name split — addressed by its id alone. A `-` in
the image column means the workspace boots explicit kernel/rootfs
paths instead of a catalog image.

`--json` replaces the table with one JSON document — the API's
workspace rows verbatim (id, name, kernel, initrd, rootfs, cmdline,
cpus, mem_mib, image_hash, image_ref, host, root_mib, home_mib,
status, created_at):

```bash
msks ls --json | jq -r '.[] | select(.status == "running") | .name'
```

A daemon with zero workspaces prints nothing (an empty table) and an
empty JSON array under `--json`.

## `msks storage`

Reports the state-disk budget and its consumers (#184) — the budget
line, each workspace's cost beside its ceiling, and each catalog
image's cost:

```text
$ msks storage
state disk    used 23.4G of 40G    free 16.6G    pressure ok

┏━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ workspace ┃ root cost/ceiling ┃ home cost/ceiling ┃ cost ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ ws4       │ 3.1G / 10G        │ 812M / 2G         │ 3.9G │
│ scratch   │ 61M / 10G         │ 12M / 2G          │ 73M  │
└───────────┴───────────────────┴───────────────────┴──────┘

┏━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ image     ┃ imported         ┃ cost ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ debian:13 │ 2026-09-21 12:03 │ 3G   │
│ debian:13 │ 2026-08-02 05:11 │ 3G   │
└───────────┴───────────────────┴──────┘
```

- **cost** is the disk blocks the artifact occupies on the state
  disk — an idle artifact costs its content, not its ceiling (the
  files are sparse), and an overlay's cost rides a little above
  what the guest's own `df` shows for its root (qcow2 bookkeeping).
- **imported** is when the image archive came in, local time to
  the minute (#186) — entries that share a reference (a rebuilt
  image imported again) read as distinct through it; the listing
  orders them oldest-first.
- **ceiling** is the size fixed at create; the guest sees it as its
  quota and its user can watch it fill with `df` inside the
  workspace.
- **pressure** names the state-disk condition: `warn` at or past
  `MSKSD_STORAGE_WARN_PCT` (default 90) percent used, `critical`
  below `MSKSD_STORAGE_FLOOR_MIB` (default 512) free. Below the
  floor, workspace creates, image imports, and home-volume
  imports answer a named `507` until space comes back (`msks
storage` names the consumers; `msks rm` and `msks image rm`
  remove them; the message's other escape is _lowering_ the floor —
  raising it refuses more, not fewer). Imports are sized when the
  size is knowable: the image archive is counted twice (retained
  copy plus unpacked cache), and a home upload with a
  `Content-Length` must fit above the floor.

`msks storage <workspace>` narrows the workspace table to one
workspace, by name or id.
`--json` prints the API's `GET /api/v1/storage` document verbatim
(state block, per-workspace rows, catalog rows). The events channel
carries a `storage.pressure` event whenever the pressure changes.

## `msks create`

POSTs the API's create body. The positional argument names the
workspace (#246) and follows the daemon's workspace charset —
lowercase letters, digits, and dashes, starting with a letter or
digit, up to 64 chars. The daemon mints the workspace's immutable
id; the name is the label the other commands address it by. The
create request's field is `name` (an `id` field is accepted as the
same thing — the pre-#246 spelling).

Flags map onto the create request's fields (the identity flags
below generate theirs):

| Flag          | API field               | Meaning                                                                                   |
| ------------- | ----------------------- | ----------------------------------------------------------------------------------------- |
| `--image`     | `image`                 | Catalog ref: `name:version`, bare name, or hash                                           |
| `--kernel`    | `kernel`                | Explicit kernel path (skips the catalog)                                                  |
| `--initrd`    | `initrd`                | Explicit initrd path                                                                      |
| `--rootfs`    | `rootfs`                | Explicit rootfs path (skips the catalog)                                                  |
| `--cmdline`   | `cmdline`               | Explicit kernel cmdline                                                                   |
| `--cpus`      | `cpus`                  | vcpus, 1–64 (daemon default: 2)                                                           |
| `--mem-mib`   | `mem_mib`               | Guest memory MiB, 64–32768 (daemon default: 8192)                                         |
| `--root-mib`  | `root_mib`              | Persistent root overlay size (daemon default)                                             |
| `--home-mib`  | `home_mib`              | Persistent /home volume size (daemon default)                                             |
| `--user-data` | `user_data`             | First-boot provisioning payload file; `-` reads stdin (#41)                               |
| `--pubkey`    | `ssh_pubkey` (verbatim) | Use a public key you already own as this one workspace's identity (#132); `-` reads stdin |
| `--user`      | `user`                  | The workspace's login user (#248); default: your username                                 |

Only the flags you pass are sent — unset flags let the daemon apply
its own defaults. An `--image` reference resolves against the
daemon's image catalog (`docs/images.md`); explicit `--kernel` and
`--rootfs` bypass it. Size ranges are enforced server-side; a value
outside them comes back as a validation error (below).

`--user NAME` records the workspace's login user (#248): the
name rides the row, and the guest's first boot seeds the account
— `useradd -m`, a home on the persistent `/home` volume populated
from `/etc/skel`, the workspace identity in its `authorized_keys`,
and the same passwordless-sudo grant the image's own workspace
user carries. `msks ssh`, `msks rsync`, and `msks console` then use
the recorded name as their default login, so a workspace answers
the same identity for every operator that reaches it. Without
`--user` the create fills the invoking user's name — a bare create
lands your own account. The name must fit the login-name charset
(lowercase letters, digits, dashes, underscores; a lowercase
letter or underscore first; at most 32 characters); a username
that does not (a capitalized one) is refused with a line pointing
at `--user`. Naming `root` or the image's `msks` account keeps the
shipped account and seeds nothing new. A name that lands on a
system account the image already ships (Debian carries
charset-valid names like `sync` and `man`) seeds nothing either —
the first boot says so in its log, and the login stays with the
accounts the guest already serves; pick a name the image uses
for no one. Create-time and immutable,
like `user_data`: a workspace created before #248 keeps the
image's `msks` user as its login.

`--start` boots the workspace right after creating it:

```bash
$ msks create my-workspace --image debian:13 --start
created my-workspace (id 9f2c41ab77)
attach with: msks console my-workspace
```

The confirmation line prints both halves of the workspace's
identity — the label you chose and the id the daemon minted — as
soon as the create succeeds and before the boot is
attempted. A failed boot still leaves the workspace created — the
error message says so and names the recovery command:

```text
created my-workspace (id 9f2c41ab77)
msks: 503: vmm launch failed
msks: 9f2c41ab77 is created; boot it later with: msks start my-workspace
```

Creating without `--start` prints the same line and exits; boot it
whenever with `msks start` — or just `msks console` it: the console
command boots a not-running workspace on its own (below).

`--user-data` is the first-boot provisioning hook (#41): the file's
contents travel to the daemon and run once on the
workspace's first boot (see `docs/images.md` for the seed-disk
mechanism, the payload forms each image provisioner accepts, and
the create-time immutability). It composes with `--start`:

```bash
$ printf '#!/bin/sh\napt-get update\n' | msks create ws --user-data - --start
created ws (id 77eedd0199)
attach with: msks console ws
```

One operator key is the identity (#336, #486): `msks create`
plants it as every workspace's identity — one key across
workspaces, the daemon holding the public half only. The key is
yours and msks generates nothing: `identity_file` (or
`MSKSC_IDENTITY_FILE`) names your private key file — the config
file or the environment — and a create with neither that nor
`--pubkey` refuses with a one-line error naming both. A key an
older msks wrote under the client data root
(`~/.local/share/msks/identity`, honoring `XDG_DATA_HOME` or
`MSKSC_DATA_DIR`) keeps working: point the setting at that file.

msks does not scan `~/.ssh` and never guesses which of your keys
to take. The create sends the key's derived public half only and
writes nothing per-workspace; your private key file stays where it
lives — msks reads it in place and copies it nowhere. The
confirmation names the identity it used:

```bash
$ msks create my-workspace --image debian:13 --start
created my-workspace (id 9f2c41ab77)
identity: /home/you/.ssh/id_ed25519
attach with: msks console my-workspace
```

`msks ssh` and `msks rsync` resolve the same key at session time,
so a workspace re-created under the same name keeps your access —
a new id, the same key. A key msks stages for its own sessions
must be an unencrypted OpenSSH-format key — `ed25519`, `ecdsa`
(P-256/P-384/P-521), or `rsa`; an `identity_file` naming a key
outside that set is refused with a line saying so. `--pubkey`
plants any well-formed public line at any type — the guest's sshd
stays the authority on which key types it authenticates
(#132, #115).

`--pubkey FILE` builds the workspace around a public key you
already own (#132): the file's one line travels to the daemon at
any well-formed key type, the private half stays wherever you keep
it, and nothing is written client-side. `msks ssh` and `msks
rsync` work on such a workspace when the operator identity
resolves to that key — `identity_file` pointing at it; a key that
resolves nowhere keeps the direct route — `ssh -i` through a
forward, or the `Host msks-*` alias with `IdentityFile` pointing
at it — and `msks ssh` exits with a line saying exactly that.

## `msks start`

Boots one created workspace (`POST /api/v1/workspaces/{id-or-name}/start`)
and prints the result:

```bash
$ msks start my-workspace
my-workspace running
```

The start request answers after the VMM finishes booting — a few
seconds on an idle host, longer under load. The client waits up to
two minutes before reporting a timeout, and a timeout message notes
that the daemon may still finish the boot.

## `msks stop`

Powers one running workspace off (`POST /api/v1/workspaces/{id-or-name}/stop`)
and prints the result, mirroring `msks start`:

```bash
$ msks stop my-workspace
my-workspace stopped
```

## `msks resize`

Moves a **stopped** workspace's disk sizes and topology
(#184, #277) — the ceilings its guest sees as quotas, and the cpus
and memory it boots with — through `POST
/api/v1/workspaces/{id-or-name}/resize`:

```text
$ msks resize ws4 --home-mib 4096
resized ws4: root 10240 MiB, home 4096 MiB

$ msks resize ws4 --cpus 4 --mem-mib 4096
resized ws4: root 10240 MiB, home 4096 MiB, cpus 4, mem 4096 MiB
(the new topology applies on its next boot)
```

- `--home-mib` grows or shrinks the `/home` volume. The daemon
  quiets the filesystem, `resize2fs` moves it, and a shrink that
  would cut into used blocks answers a named `409` (free data in
  the workspace or shrink less).
- `--root-mib` grows the root overlay only; the next start's
  cloud-init fills the larger device for free (the command's output
  says so when the root moved — a home resize's bytes are already in
  place). The grow-only rule is measured against the overlay's actual
  virtual size, which sits above the row when create clamped it to
  the base image. Shrinking the root stays unsupported — `msks rm`
  and a fresh create, or a factory reset, reclaim a root instead.
- `--cpus` and `--mem-mib` set the vCPU count and guest memory. The
  daemon records them in the workspace row and boots it with the new
  topology at the next `msks start` — a running workspace is never
  reconfigured live. The bounds are create's (`--cpus` 1–64,
  `--mem-mib` 64–32768 MiB), and the command's output carries the
  boot note when they changed. The flags mix freely with the disk
  flags: one invocation can move the disks and the topology
  together.

The workspace must be in a free lifecycle state (`created`,
`stopped`, `absent`) — a running or paused workspace answers `409`.
The row follows immediately (`msks ls --json`, `msks storage` show
the new ceiling), and a completed resize is announced on the events
channel (`workspace.resized`). At least one flag is required.

The stop asks the guest for a graceful, deadline-bounded power-off —
the daemon presses the ACPI power button and the guest's systemd runs
a full shutdown — so the write-out can take a moment. The deadline is
the daemon's (`MSKSD_SHUTDOWN_TIMEOUT_S`); there is no client-side
timeout flag, and like `msks start` the client waits at most two
minutes on the answer. A stop that misses the daemon's deadline
answers 503 with the endpoint's detail on one line, exit non-zero.
Stopping a workspace that is already stopped is a no-op success: the
daemon reports it stopped either way, and the client asks without
pre-filtering on local state — the same holds for a workspace that
was never booted (the row records `stopped` without a launch ever
happening). The data survives the stop — the root overlay and the
`/home` volume come back on the next `msks start`.

## `msks rm`

Deletes workspaces (`DELETE /api/v1/workspaces/{id-or-name}`) — the
row, the VMM (stopped first, killed if wedged), and the persistent
artifacts: the root overlay and the `/home` volume. The data does
not come back; creating a workspace under the same name mints a
fresh id and starts from the image's pristine root, with nothing
of the deleted instance left to collide with. One workspace or
several:

```bash
$ msks rm my-workspace
my-workspace deleted
$ msks rm scratch-1 scratch-2
scratch-1 deleted
scratch-2 deleted
```

Workspaces are removed one at a time, in the order given; a failure
stops the run there with the API's one-line error, and the ones
already removed stay removed (each success printed its confirmation
line). There is no confirmation prompt — deleting is what `rm` means,
and a workspace is recoverable by recreating it. A workspace recorded
on another host answers 409 with the host mismatch named in the error,
like every lifecycle command.

## `msks image`

Manages the daemon's image catalog — the surface `docs/images.md`
documents over HTTP, as CLI subcommands. Every subcommand uses the
same client environment as the workspace commands.

### `msks image ls`

One line per registered image — reference, hash (first 12 hex
chars), the default designation, the kernel facts, and when the
image entered the catalog — on the measured grid every listing
shares (#271):

```text
$ msks image ls
ref          hash          default  kernel                imported
debian:13    9f2c41ab77de  default  6.12.107+deb13 (raw)  2026-09-22 14:03
alpine:3.20  33aa9db1c4ef  -        6.12.7 (raw)          2026-09-19 09:41
```

The image the daemon designates as default carries the `default`
flag; a bare `msks create` resolves to it. The `imported` column
shows the moment the archive entered the catalog, in local time
to the minute — an entry re-imported under the same reference
moves the time forward with it. A renamed row (#340) adds an
`origin` column beside the ref, showing the archive's own pair
where it differs from the registered one; rows the archive's own
pair names keep the column with a dash. `--json` prints the
listing as the API returns it (`GET /api/v1/images`, each row's
`imported` field in ISO 8601 UTC, each row's `origin_name`/
`origin_version` carrying the manifest pair), stable for
scripting.

### `msks image import`

Registers an archive in the catalog (`POST /api/v1/images`). The
source is a **daemon-side** path — the daemon reads the file from
its own filesystem; the command does not upload anything — or an
`https://` URL the daemon downloads itself (#258):

```text
$ msks image import /srv/images/debian-13.tar
imported debian:13 (9f2c41ab77de)

$ msks image import https://images.example.com/debian-13.tar
imported debian:13 (9f2c41ab77de)

$ msks image import /srv/images/debian-13.tar --name mine --version 1.0
imported mine:1.0 (9f2c41ab77de)
```

A `--name`/`--version` override (#340) registers the archive
under an operator-chosen pair — either key alone, the other from
the archive's own manifest, whose pair stays recorded as the
row's origin (see `docs/images.md`). The archive bytes and the
content hash stay exactly as they were; a pair another row
already holds is refused with a named error.

A URL source is fetched into the catalog's staging area under the
import ceiling and deadline (`MSKSD_IMAGE_IMPORT_MAX_MIB`,
`MSKSD_IMAGE_IMPORT_TIMEOUT_S`), verified against system TLS
roots, and imported from the downloaded copy — the recorded hash
always reflects the fetched bytes, so the same content imports
once regardless of source. The first image imported into an empty
catalog also becomes the daemon's default. An archive the daemon
cannot read, parse, or fetch answers 400 with the reason on one
line.

### `msks image rename`

Changes a cataloged image's registered name/version (#340) — the
bytes, the hash, the manifest origin, and workspaces already
booting the image stay put:

```text
$ msks image rename debian:13 --name mine --version 1.4
renamed debian:13 to mine:1.4 (9f2c41ab77de)

$ msks image rename mine:1.4 --version 1.5
renamed mine:1.4 to mine:1.5 (9f2c41ab77de)
```

Either key alone keeps the other at its registered value; pass
at least one. The reference forms are the same as `image rm`'s
— `name:version`, a bare name (its newest version), `name@hash`,
a full hash, a unique hash prefix — and the rename goes through
the daemon (`PATCH /api/v1/images/<hash>`) after resolving the
reference against the listing. A pair another row already holds
is refused with the daemon's named error (409), and a pair the
reference forms cannot carry (`:` or `@` inside a value, or an
empty one) is refused by name too. After a rename the old
`name:version` stops resolving; `name@hash` (with the new name)
and bare-hash references keep booting the same bytes.

### `msks image check`

Boots an image locally and verifies the guest contract point by
point (#258) — the pre-import gate `docs/images.md` describes. The
command runs on the image author's host: it needs `/dev/kvm` and
nothing else from msks (no daemon, no state):

```text
$ msks image check workspace-mine-1.0.tar
PASS archive        imported mine:1.0 (1d6a5e782fc0), root autologin getty on the console port
PASS boot           guest answered the console in 3.4s (kernel 6.12.107+deb13-amd64)
PASS console        root autologin getty on the console port
PASS user-data      seed payload ran on first boot
PASS acpi-shutdown  clean shutdown within 120s
PASS root-rw        root is writable and the write survived a stop/start (overlay)
PASS home-label     /home mounted by label msks-home and its write survived a stop/start (volume)
```

A broken contract point fails its row and the exit code is 1,
naming the first failure; `--keep` preserves the throwaway state
dir (serial logs) for inspection. `--egress` adds the DHCP point —
the guest must take a global address over the daemon's own net
stack — and needs root plus an egress-capable default route
(`--uplink` names another interface).

### `msks image rm`

Removes an image from the catalog. The reference accepts every form
the daemon resolves for workspace create — `name:version`, a bare
name (its newest version), `name@hash` (the full 64-hex hash), a
full hash — and a unique hash prefix (the 12 chars `image ls`
prints):

```text
$ msks image rm debian:12
debian:12 deleted
```

The removal is keyed by the image's hash after resolving the
reference against the listing. An image a workspace still boots is
refused — the API's 409 names the workspace — and a reference that
matches nothing exits with the catalog spelled out so the next try
can be copy-pasted. An ambiguous hash prefix names the images it
matches; use the full hash or `name@hash`. (Two imports of the same
`name:version` — a rebuilt archive — are the usual ambiguity, and
only the hash forms still identify one of them.)

### `msks image info`

Prints one image's full record — reference, the archive's own
pair (the origin, #340), hash, kernel facts, cmdline, and the
default designation — from the same listing data:

```text
$ msks image info debian:13
ref      mine:1.4
origin   debian:13
hash     9f2c41ab77de0000000000000000000000000000000000000000000000000000
kernel   6.12.107+deb13 (raw)
cmdline  console=ttyS0 root=/dev/vda rw
seed     provisioner - (none declared)
default  yes
```

The reference forms are the same as `image rm`'s.

### `msks image default`

Designates the image a bare `msks create` boots (#270) — the row
`image ls` marks — or clears the designation with `--unset`:

```text
$ msks image default debian:13.6
designated debian:13.6 (98ccf2e1f2db) as the default image

$ msks image default --unset
default designation removed; a bare create falls back to the sole
entry debian:13.6 (98ccf2e1f2db)
```

The reference forms are the same as `image rm`'s — `name:version`,
a bare name (its newest version), `name@hash`, a full hash, a
unique hash prefix. The designation goes through the daemon (`POST
/api/v1/images/default`), survives restarts, and the next `image ls`
marks the row. A fresh `MSKSD_DEFAULT_IMAGE` import at daemon
startup still reclaims the slot — the command is the operator's
designation between restarts. A reference that names nothing exits
with the catalog spelled out, the same miss `image rm` answers.

`--unset` clears the designation (`DELETE /api/v1/images/default`)
and reports the fallback a bare create now takes: the sole catalog
entry stays what it boots (the line above), and on a catalog
holding several images a bare create needs `--image` until one is
designated again — the line says so.

## `msks home`

Moves a workspace's `/home` volume through the daemon (#80) —
backup, migration to another daemon, seeding a fresh workspace with
data — as the two byte-stream endpoints `docs/storage.md` documents:

```bash
msks home export my-workspace              # writes my-workspace.ext4
msks home export my-workspace other.ext4   # names the output file
msks home export my-workspace - | gzip > backup.ext4.gz
msks home import fresh-ws my-workspace.ext4
msks home import fresh-ws - < backup.ext4
```

The workspace must be stopped (a volume under a running VM answers
`409` — stop it first; `msks stop` is enough). A boot that arrives
during a move waits for it and boots the volume the move left
(moves, boots, and deletes serialize per workspace; the waiter
answers a named 409 after `MSKSD_MOVE_WAIT_TIMEOUT_S` instead of
hanging). Export streams the
volume file's bytes verbatim and prints one confirmation line
(`exported my-workspace (2097152 bytes) to my-workspace.ext4`);
`-` writes the bytes to stdout and moves the note to stderr, so a
pipe stays clean for gzip or ssh — and a reader that goes away
mid-stream (`| head`, a compressor on a full disk) prints one line
on stderr and exits non-zero. Import uploads the named ext4
image (or stdin, for `-`), the daemon replaces the volume with it,
and the reply is the byte count: `imported 2097152 bytes into
fresh-ws`.

The daemon refuses — one line, exit 1 — a file that is not an ext4
image (`msks: 400: the request body is not an ext4 image ...`), a
workspace in the wrong state, and everything the API's refusals
name (a foreign host). An import keeps the
workspace's existing volume until the upload completes and passes
the ext4 check; a cut-off upload changes nothing.

The typical pairings: backup (`export`, later `import` back into
the same workspace after a `rm` + `create`), migration (`export` on
one daemon, `create` + `import` on another), and seeding (build an
ext4 image with the data a fleet of workspaces starts from, import
it into each fresh one). Inside a workspace with egress, git and
rsync over the forward (`docs/networking.md`) carry day-to-day
code; the volume moves are for the whole `/home` at once.

## `msks secret`

Placeholder secrets (#198, #339, #423): mint a sentinel — daemon-wide by
default, scoped with `--workspace` — and the daemon keeps your value
in its agefile — the workspace never holds it (the full story is
[docs/secrets.md](secrets.md)):

```bash
# from a password manager, nothing touches disk: daemon-wide,
# one sentinel for every workspace on the daemon
op read 'op://Vault/github/credential' \
  | msks secret mint --name github_api \
      --dest api.github.com --secret-file -

msks secret mint --workspace myws --name pypi \
  --dest .pypi.org --dest pypi.org \
  --ttl 86400 --secret-file ./token    # scoped, suffix + exact

msks secret mint --workspace ci,deploy --name pypi \
  --dest .pypi.org --secret-file ./token   # one row, two workspaces

msks secret ls                           # placeholders + coverage, never sentinels
msks secret renew --workspace myws --name pypi --ttl 86400
msks secret revoke --name github_api     # the daemon-wide row of the label
msks secret revoke --workspace ci,deploy --name pypi
msks secret coverage myws scoped         # exempt myws from daemon-wide rows
msks secret check                        # the store answers writes
```

`--secret-file` takes a path or `-` for a pipe (the value is
whitespace-stripped at both ends); the secret is
never accepted as a command-line argument (argv lands in process
lists and shell history), and an empty file is refused before any
network roundtrip. `--dest` repeats and binds the swap: an exact
host (`api.github.com`) or a suffix that covers every host under a
domain (`.github.com`). `--workspace` takes one ref or a comma
list and repeats; omitted, the mint covers every workspace on the
daemon — one row, one `mskssec2_` sentinel — while a scoped mint
prints a `mskssec1_` sentinel, so the string alone names its
reach. The mint prints the sentinel;
the listing and the audit omit it (`secret ls` never shows one),
and the daemon serves a row's sentinel back on demand over the
token-authenticated API — the `msks tui` row panel (#440) is
that surface. `revoke` and `renew` take the same targeting the mint
took; `revoke` takes effect on the next request and retires the
whole row everywhere at once, and `renew`
extends a `--ttl` lifetime in place with the sentinel unchanged.

## `msks console`

An interactive shell inside a workspace, over the daemon's console
websocket. The command boots the workspace first when the daemon
reports it as not running — the notices print on stderr while the
boot runs, then the session attaches:

```text
$ msks console my-workspace
msks: my-workspace is stopped; starting it
msks: my-workspace running
(workspace prompt)
```

The session is a root shell (#481): the console is the failsafe
path, served by the guest's autologin root getty on the
virtio-console port — a raw byte stream over the daemon's
authenticated websocket, with no user selection and no in-band
protocol. Reach a workspace user's shell with `su - <name>` inside
the session, or over ssh for the full interactive posture:

```text
$ msks console my-workspace
(workspace prompt, as root)
```

The getty keeps its own terminal geometry for the session's life:
the console stream is a raw byte pipe, so a window resized
mid-session does not reach it. Full-screen work (editors, tmux) belongs to an ssh session through
the forward (#108–#112): ssh's window-change channel resizes its
pty live.

A workspace that is already running attaches with no preamble. Two
states get special handling:

- **`starting`** — another client's boot is in flight. The console
  waits for it (polling up to two minutes) and attaches when it
  lands, instead of racing a second boot into the daemon's
  double-launch guard.
- **`paused`** — refused with the honest reason: the daemon has no
  resume, so the message names the recovery (`msks stop` it, then
  `msks start` again).

A start that loses a race — the daemon reports `stopped`, another
client boots it in the gap — re-checks and attaches to the winner.

Ctrl-] detaches and leaves the workspace running; Ctrl-C and Ctrl-D
reach the guest. To type a literal Ctrl-] into the guest, press it
twice quickly (the second press within 50 ms): the pair delivers one
Ctrl-] byte. A single Ctrl-] — or one followed by any other byte —
detaches, and the byte that followed the escape is consumed with it,
so a paste that happens to contain a lone Ctrl-] detaches the session.
Large pastes travel as a few websocket frames (4,096-byte chunks), not
one frame per byte. The session needs a tty on both stdin and stdout.
See the README's workspace-console section (#21) for the transport
story.

A session whose guest stream stops carrying bytes while input keeps
flowing is closed by the daemon after `console_stall_timeout_s`
(`MSKSD_CONSOLE_STALL_TIMEOUT_S`, 60 s default): the guest pty echoes
every input byte, so that silence names a wedged stream, and the
client exits with `console stalled (guest stream wedged; reconnect
for a fresh session)` — reconnecting opens a fresh shell in the same
workspace. An idle session stays open indefinitely: the clock runs
only while client input is waiting for its echo (#103). Two caveats:
a program that reads with echo off (`read -s` password prompts, `su`)
also draws no echo, so a prompt left waiting past the window closes
the session — set the timeout higher or to `0` (off) for such
workflows. The deadline is anchored at the first unanswered input, so
continuous sending into a dead console still gets the named close one
window later. A stream that wedges while the helper still holds
undelivered output is closed by the helper's own teardown after
300 s (without the 4502 name); a stream that wedges fully idle stays
open — nothing is in flight to time — until the next input arms the
daemon's clock.

## `msks forward`

A guest TCP port on this command's stdio — the pipe ssh's
ProxyCommand expects — over the daemon's forward websocket. The
command boots the workspace first when the daemon reports it as not
running (the same notices as `msks console`), then bridges bytes
unexamined in both directions: a tty is not required, and binary
protocols (ssh, rsync) ride it cleanly:

```bash
msks forward my-workspace 22                 # stdio: ProxyCommand shape
msks forward my-workspace 8080 --local 8080   # loopback listener
```

`--local PORT` binds `127.0.0.1:PORT` instead of stdio; every
accepted connection opens its own forward websocket, so parallel
clients (a browser and a curl, two ssh sessions) are independent
sessions. Refusals print one line — the daemon's close codes name the
cause (no NIC, not running, service not listening, still booting) —
while a clean end of stream (the guest service closed, stdin EOF)
leaves exit code 0.

The forward authenticates with the websocket handshake's
`Authorization: Bearer` header (the same scheme as every other
msks surface, #216): the token never lands in a URL, so proxy and
process logs hold no credentials. Only egress workspaces have a
NIC to forward to — a workspace created `--no-egress` is refused
with the reason naming it. `forward.opened` and `forward.closed`
events appear on the daemon's events channel for every session.

## `msks egress`

Egress consent (#69, #195): decide, watch, and inspect a workspace's
outbound-destination verdicts. A workspace in `interactive` mode
holds each new outbound connection's first packet until a decider
allows or denies it — for web flows (TCP 80/443) of an armed
interceptor workspace, the hold lands at the connection's TLS
handshake or its first HTTP request and shares the same rows and
durations; `static` workspaces allow only their
create-time allowlist; `allow` workspaces (the create default)
record off-list destinations and pass them.

```text
msks tui                          # THE decider: the workspace page's
                                  # egress consent page (see `msks` above)
msks egress rules ws-dev          # the mode, allowlist, and in-effect verdicts
msks egress requests ws-dev       # the consent rows (audit trail), newest first
msks egress requests ws-dev --decision pending
msks egress decide ws-dev <request-id> allow --duration 5m
msks egress decide ws-dev <request-id> deny --duration forever
msks egress revoke ws-dev <request-id>
msks egress mode ws-dev interactive          # switch the posture (#280);
msks egress mode ws-dev static --allow .debian.org   # --allow replaces
msks egress mode ws-dev static --offline     # ... the allowlist (--offline
                                              # confirms an empty static)
msks egress watch ws-dev          # stream frames; registers this client as
                                  # a decider (holds wait only while one is
                                  # connected)
msks egress watch ws-dev --decide --duration forever
                                  # the same, prompting y/n per request
```

`decide` and `revoke` name the request by the id `watch` and
`requests` print (the full id, copy-pasteable); both act only on
the named workspace's requests. A portless destination (a
non-TCP/UDP flow) prompts as `host (all ports)` — an allow opens
every port on the host for the duration. Durations: `once` (this connection only — a
reconnect re-prompts), `5m`, `15m`, `tilrestart` (until the
workspace VM stops; the default), `forever` (the workspace's
lifetime — replayed at every boot). `revoke` undoes an in-effect
verdict immediately: the flow rules and the destination's live
connections drop, and new connections gate again.

`mode` (#280) switches a workspace's posture without recreating
it. A running workspace swaps live: the whole firewall table
re-applies in one transaction, established connections survive
(an attached `msks ssh` session stays up), the resolver flips
with the chain, and any held request answers deny when the
switch leaves `interactive`. A stopped workspace builds the new
posture at its next start — the command's output line says which
happened. Verdicts carry across switches: an `allow forever`
granted under `interactive` keeps acting under `static`, so
switching to `static` freezes the workspace at everything
approved so far — name-keyed verdicts in full, and, on a direct
gated→gated switch, the kernel-side pins too. A round-trip
through `allow` rebuilds the pins from the durable rows
(`forever` verdicts re-pin at entry; a timed address-keyed allow
re-prompts or re-learns on the guest's next resolution). `--allow SPEC` (repeatable, create's grammar)
replaces the allowlist; omitted, the workspace keeps its list.
Switching to `static` with nothing effectively allowed — an
empty allowlist and no in-effect allowed verdict — is refused
with a message naming the fix: pass `--allow` entries, or pass
`--offline` to run the switch anyway (that posture answers every
name NXDOMAIN — an offline workspace, reachable only by ssh).

### The decider surface

`msks tui`'s workspace page is the interface a human decides
from: while the page is open it registers this client as the
workspace's decider (holds wait only while one is connected), and
its consent overlay — pushed by the page's **Egress consent**
action, or by the first hold of a burst — shows every held
request with its countdown and sends verdicts through the same
endpoints the subcommands use. Keys: `a`/`d` allow or deny the
focused hold for the default duration, `A`/`D` open the duration
picker, `r` the rules screen (with `x` to revoke), `m` the mode
picker — the `msks` section above owns the overlay's full
lifecycle (the auto-open, the park, the self-close). The
placeholder-token audit is the secrets page's daemon-wide view
(#390). A dropped link reconnects
with backoff and re-registers (the snapshot re-lands); while
disconnected the overlay's status line says so — the daemon
fail-closes new connects, and in-flight holds run their timeout.
An off-allowlist sighting flashes the surface that owns the
terminal: the overlay's status line while it is up, the page's
consent line when it is not (#201's alarm, carried into the
tree).

The protocol state (frame parsing, countdowns) is pure and
unit-tested; the `watch`/`decide`/`revoke` subcommands remain the
scripting surface (`watch` prints frames as lines).

The create-time posture (`msks create --egress-mode`, `--allow`)
sets the first mode — `--egress-mode allow|static|interactive`,
and repeatable `--allow SPEC` entries — a bare host matches the
apex only, `.host` includes subdomains, `*.host` matches subdomains
only, `host:port` scopes a port, and `10.0.0.0/8[:port]` names an
address range. Name entries gate at the daemon's resolver (the one
the DHCP lease hands the guest); address entries accept in the
per-VM kernel chain. `msks egress mode` switches the posture
after create (#280); the daemon-wide default new workspaces take
is `MSKSD_EGRESS_MODE` (`allow` as shipped).

`msks create --secret-coverage scoped` boots the workspace exempt
from daemon-wide placeholders (#339) — only placeholders minted
directly at it arm it; the default (`all`) accepts daemon-wide
coverage, and `msks secret coverage` flips the posture after
create.

## `msks key`

The workspace's ssh identity (#111): every workspace a
local-backend daemon creates carries the public half its create
sent — the operator's key (#486), whose line the guest's first
boot planted into `authorized_keys` for root and the workspace's
login user. The fetch takes the same
`MSKSC_URL`/`MSKSC_TOKEN`/`MSKSC_CAFILE` environment as every other
command:

```bash
msks key my-workspace                    # the public authorized_keys line
msks key my-workspace --private          # the private half, on stdout
msks key my-workspace --out ~/.cache/msks/my-workspace.key
```

`--out FILE` writes the private half with mode 0600 and prints
nothing but the path; the mode is forced on an existing file too.
The path is always the operator's choice: the command writes the
key to the file the operator named and to stdout, and nowhere else.
A shell redirect (`msks key my-workspace --private > f`) keeps the
shell's own umask — that is what `--out` is for. A workspace that
predates #111 answers 404 with "no ssh identity"; a keyless create
(a deliberate API call with no key) answers the same. The public
half persists across daemon restarts and workspace stop/start.

The daemon holds no private half for a workspace created now
(#486): `--private` and `--out` exit with an error naming where
that half lives — the private file `identity_file` (or
`MSKSC_IDENTITY_FILE`) names. A workspace whose row predates #486
keeps serving the half the daemon still holds: a daemon-minted
row (#111) answers both private forms, and a client-minted one
(#121) names the client data root of the client that minted it
(`~/.local/share/msks/<id>/identity`, or that root under
`MSKSC_DATA_DIR`).

## `msks ssh`

Stock ssh into a workspace over the forward, with the identity
staged in memory (#112) — one command, no key steps:

```bash
msks ssh my-workspace              # as the workspace's login user
msks ssh my-workspace -- -l root   # the recovery login
msks ssh my-workspace -- -A        # forward your agent ($SSH_AUTH_SOCK)
msks ssh my-workspace -- -L 8080:localhost:80
```

Options you pass here win for the invocation; the config file's
`ssh_options` key remembers a set for every session instead — see
[its section](#ssh_options) for the precedence between the two.

The command boots the workspace first when the daemon reports it as
not running (the same notices as `msks console`), fetches the
identity over the authenticated API, and runs `ssh` with the
forward websocket as its ProxyCommand (`msks forward <ws> 22`). A
session that booted its workspace waits out the guest's first boot
(#168): the daemon reports `running` while the guest's sshd is
already up but the identity seed has yet to land in
`authorized_keys`, so the command probes the login first (a
throwaway `true` as the remote command, retried for up to 30s with
a one-line notice between attempts) and opens the real session —
interactive or one-shot — once the guest accepts the workspace
key; a remote command runs exactly once either way. The probe
carries only the session's login user and agent-forwarding setting —
msks's own transport, its
quiet flag, and a `true` command — so a session's tunnels and
other ssh options cannot hold the wait open or alter it; the
session itself keeps every option. For
a workspace created now (#486) the API serves the public half and
the private half comes from the operator identity —
`identity_file` (or `MSKSC_IDENTITY_FILE`), the same key the
create planted. A workspace whose row predates #486 resolves
through the legacy paths: a daemon-minted half arrives over the
API; a client-minted one (#121) comes from the local data root
(`~/.local/share/msks/<id>/identity` under the default root,
`MSKSC_DATA_DIR` when it is set; written at create) — a missing,
stale, or corrupt file exits with one line naming the path and the
recovery, which for a key an older msks minted to the data root
(`~/.local/share/msks/identity`) is pointing `identity_file` at
it. Either way the session writes no new copy of the private half
anywhere: a transient in-process ssh-agent holds it in memory for
the session, ssh names the identity by its public half (`-i`,
public material only) and signs through the agent socket — a
daemon-held half arrives over the API and goes away with the
process, and a locally-held half — yours — is read from its one
file and left exactly there. Host keys land in a per-workspace
`known_hosts` under the msks cache root — `MSKSC_CACHE_DIR` when
it is set, else `XDG_CACHE_HOME` or `~/.cache/msks`, then
`<workspace-id>/known_hosts` (#246 — the cache keys on the
workspace's immutable id, so a workspace recreated under the same
name starts with a fresh cache and trusts its own first-boot keys
again) — under `accept-new`; they persist across stop/start on the
workspace's overlay, so the first-connection entry keeps matching.
(The alias block keeps its own known_hosts under
`~/.cache/msks/msks-<ws>/` — the two paths record the same host
key independently.)

Agent forwarding asked for on the command line (`-A`, or
`-o ForwardAgent=yes`) forwards **your** agent — the socket
`SSH_AUTH_SOCK` names (#174): `ssh-add -l` inside the workspace
lists the same keys as on the host, and `git push git@github.com`,
`ssh`, and friends sign with the host's credentials. The session
authenticates through a transient in-process agent (above), and
stock ssh would forward _that_ one when asked to forward at all —
so msks rewrites the request into `ForwardAgent=<your socket>`,
an explicit path (an OpenSSH client 8.2 or newer parses the
form; an older client exits with its own usage error — RHEL 8's
8.0, for one); the workspace identity stays an
authentication credential and reaches the guest as nothing else.
A request that already names a socket
(`-o ForwardAgent=/path/to/sock`) keeps its own. `-A` with no
live agent behind `SSH_AUTH_SOCK` exits with one line naming it.
Forwarding set by an ssh config file keeps the stock meaning
under this command — it forwards the session agent; the command
line is where your agent is named.

Everything after the workspace id (the `--` is optional — any
argument ssh would take works verbatim) is passed to ssh. A
passthrough that starts with a plain word is a remote command
(`msks ssh my-workspace -- uname -a`); when it starts with an
option, ssh's own separator carries a command after the options
(`msks ssh my-workspace -- -A -- uname -a`). `-A` forwards your
agent, the same forwarding the paragraphs above describe —
before #174 it forwarded the session agent, the workspace
identity, which served nested logins to the same workspace (ssh
from inside to `localhost`). That use now names the identity
explicitly instead — `identity_file` names your own key, or a
pre-#486 daemon-held half can be written with `msks key
my-workspace --private` and added to your own agent once
`ssh-add`ed.
The exit code is ssh's own (255 for ssh failures), not the msks
command set. The session agent lives exactly as long as the
`msks ssh` process — `ControlPersist`/mux sessions that outlive it
belong to the alias path, whose identity is a key file. Explicit
passthrough options override the injected defaults (your own
`UserKnownHostsFile`, a different `ProxyCommand`) exactly as with
stock ssh: ssh takes the first value a repeated option receives,
and the passthrough comes first. The login user is the
workspace's recorded login user (#248 — the create-time `--user`,
defaulting to the creating operator's name; the image's `msks`
account for a workspace created before #248); ssh arguments that
name a user
(`-l root`, `-o User=root`) override it. The host argument ssh
sees is the workspace id itself — the transport is the proxy, so
the name never resolves. The ProxyCommand runs this very
client (the absolute interpreter and module form — `msks` need not
be on the ssh child's PATH), and it inherits your environment:
`MSKSC_URL`, `MSKSC_TOKEN`, and
`MSKSC_CAFILE` must be set where ssh runs (see the
networking chapter's alias workflow for the `Host msks-*`
configuration that hides all of this).

## `msks rsync`

Stock rsync against a workspace over the forward, with the
identity staged in memory (#190) — one command does the whole
setup itself: the workspace boots when needed, the identity
stages in memory, and the copy opens its own forward:

```bash
msks rsync my-workspace -- -av ./site/ root@:/root/site/    # push
msks rsync my-workspace -- -av root@:/root/out.tar ./out.tar  # pull
msks rsync my-workspace -- -aP ./src/ :src/                   # as msks
```

Everything after the workspace id (the `--` is optional) is given
to rsync; msks parses no rsync flags. The one shaping pass is
the empty host: a path whose host is empty (`:/root/site/`, or
`root@:/root/site/`) targets the workspace this command names,
its host filled in as the copy runs. The direction — push or
pull — comes entirely from the rsync arguments, and the login
user comes from the paths the same way: `root@:/root/site/`
logs in as root, a bare `:src/` as the workspace's login user
(whose home is the persistent `/home` volume — the shape for
copies under your own account). A path that names a host keeps
it, and the transport is the proxy either way, so the name never
resolves. `::module` paths are rsync's daemon protocol against
port 873 — the workspace image runs no rsync daemon (sshd stays
its one inbound service), so that form is left as typed and
fails as rsync's own error.

A word that starts with a colon reaches this fill even when it
is the split-apart value of an rsync option (`--filter :rules`),
because msks does not parse rsync's flags to tell the two apart:
that spelling uses the attached form (`--filter=:rules`), which
msks never touches.

The command boots the workspace first when the daemon reports it
as not running (the same notices as `msks console`), fetches the
identity over the authenticated API, and runs the host `rsync`
with the same transport `msks ssh` uses: the forward websocket as
the ssh ProxyCommand (each rsync connection opens its own), the
per-workspace `known_hosts` under `accept-new`, and the identity
staged in a transient in-process ssh-agent — the private half
exists only in memory, rsync's ssh children name it by its public
half and sign through the agent socket, and the command writes no
key file. The operator identity (#486 — `identity_file`), a
`--pubkey` workspace (#132), and the pre-#486 row shapes
(daemon-minted #111, client-minted #121) all work: the private
half resolves where it lives — from the operator's own file, over
the API, or from the per-workspace file — and a key that resolves
nowhere on this client exits with the line naming where it can
be.
A session that booted its workspace waits out the guest's first
boot exactly as `msks ssh` does (#168): a probe login retries
behind the identity seed (up to 30s, one line between attempts),
and the copy runs once the guest accepts the workspace key.

The login user is the workspace's recorded login user (#248),
stated as a generated per-session ssh config — so rsync's own
`user@` path spelling overrides it (`root@:/root/site/` logs in
as root). An explicit `-e` in the passthrough replaces msks's
remote shell entirely (rsync takes the last `-e`, and the
empty-host fill keeps pointing at the workspace id your own
transport must then reach), the same override shape ssh
passthrough options have. The host prerequisites are `ssh` and
`rsync`; the exit code is rsync's own.

## Errors, exit codes, and timeouts

Every command fails with one readable line on stderr and exit code 1
— never a traceback:

```text
msks: set MSKSC_TOKEN to a daemon token (MSKSC_URL for a non-default daemon)
msks: cannot reach https://192.168.77.2:8660: [Errno 113] ...
msks: 401: invalid or revoked token
msks: 409: workspace exists
msks: 422: body.id: String should match pattern '^[a-z0-9][a-z0-9-]*$'
```

The status lines carry the daemon's `detail` field verbatim. A
validation failure (422) reports each problem as `field: message`,
joined on one line — the same facts the API returns, minus the JSON
scaffolding. A timeout says so explicitly, because the daemon may
still complete a request the client stopped waiting for. Argument
errors exit with code 2 — the convention the argparse-era CLI set
and the typer-based one (#315) keeps; success is 0; a Ctrl-C during
a long boot prints `msks: interrupted` and exits 130.
