# Changelog

All notable changes to msks are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
tagged `vX.Y.Z`.

## \[Unreleased]

### Added

- **Dictation in the GUI (#1214).** A small pill at the right end of
  every page's bottom row is a toggle: one click starts a recording,
  a second ends it, and the transcript goes to the clipboard and the
  primary selection with a tone at each end and a desktop
  notification. A **Settings** tab edits the model, the microphone,
  and the vocabulary prompt, and downloads the whisper.cpp model with
  a progress bar. The settings save to `~/.config/msks/gui.yaml`, a
  file the GUI owns beside the client config, and `MSKSC_DICTATE_*`
  variables pin any of them. The host needs PipeWire's `pw-record` and
  `pw-cat` and whisper.cpp's `whisper-cli`; see
  [docs/gui.md](gui.md#dictation).

### Changed

- **A bare `msks` opens the GUI (#1221).** `msks` with no subcommand
  opens the desktop window, the same window `msks gui` opens. The TUI
  has its own command: `msks tui` opens the workspace tree, and `msks
tui <workspace>` opens that workspace's page. A bare `msks` over ssh
  or on a host with no display prints one line whose last clause is
  `msks tui opens the terminal UI instead`, and exits 1. See
  [docs/gui.md](gui.md) and [docs/tui.md](tui.md).
- **`msks-setup-macos-host` lands the dictation tools (#1216).** The
  tool now brews `whisper.cpp` and `ffmpeg` and writes `pw-record`,
  `pw-cat`, and `notify-send` shims at the brew prefix's bin, so the
  GUI's dictation works on a Mac. `--check` reports the engines and
  shims, and a Mac without them fails that check until the tool runs
  again. The shims carry the tool's mark, a re-run refreshes them,
  and `--uninstall` removes them. See
  [docs/installation.md](installation.md#macos-on-apple-silicon-hosts).
- **Guest toolchain pins (#1217).** The Debian, Fedora, and NixOS
  guests ship pi 1.7.0 (from 1.5.2) and Claude Code 2.1.332 (from
  2.1.325), with herdr unchanged at 0.9.5. pi 1.7.0 removes install
  telemetry upstream. `~/.pi/agent/settings.json`, the default the image
  plants, drops the now-unused `enableInstallTelemetry` key. Workspaces
  built from new guest assets report the new versions, and existing
  workspaces keep their baked toolchain until rebuilt.

### Fixed

- **A stopped holder takes the screenshot pipe's ssh child with
  it (#1209).** A holder that stopped while its ssh child was
  starting left that child running, and the guest's screenshot port
  stayed bound after the holder was gone. The stop now takes that
  child with it like any other.

## \[v0.11.1] - 2026-11-27

### Added

- **Tearing shell tabs off into windows in the GUI (#1203).** A
  left-button drag on a shell tab moves it along the strip, and the
  tab takes its new place on release. Dropped off every tab strip, the
  tab opens as a window of its own that keeps the session running, with
  the tab's close button, the rename, the shell keys, and a **+** tab
  of its own. Dropped on another window's tab strip, it becomes a tab
  there. The gestures work in terminal-only mode too. See
  [docs/gui.md](gui.md#tearing-a-shell-off-into-a-window).

### Fixed

- **The GUI's first tab strip keeps one height on a host without
  fonts (#1211).** The bundled terminal faces now sit in the font
  database before the first widget builds. The first window's tab
  bars measure with the same face as every later one. A host with
  fonts of its own shows no difference.

## \[v0.11.0] - 2026-11-27

### Added

- **The `screenshot` tool and `/screenshot` in the guest's pi
  (#1197).** pi inside a workspace gains a `screenshot` tool that
  captures the client's screen through the screenshot pipe and
  returns it as an image in one call, with the full-resolution file
  kept for crops. `/screenshot [monitor]` puts a capture in front of
  the agent, and `/screenshot model` picks a vision model from the
  model list: a picked model describes each capture in text when it
  is not the active model, and otherwise the active model sees it.
  The images ship the extension beside `llm-models.ts`. See
  [docs/screenshot.md](screenshot.md#the-guests-pi).

- **The screenshot pipe (#1194).** A skill running inside a
  workspace guest can capture one of the client's screens through a
  loopback port in the guest (default `127.0.0.1:8662`) that reaches,
  through the bridge holder's ssh reverse forward, the capture command
  the client runs. The new `screenshot_command` client setting enables
  the pipe by its presence and carries that command; `screenshot_port`
  overrides the port, and `msks screenshot hold` holds the pipe on
  demand. The client runs at most four captures at once (a fifth request
  answers `503`), and one JSONL line per capture under the client state
  root is the audit trail. See
  [docs/screenshot.md](screenshot.md).

- **Command keys in the GUI terminal on macOS (#1184).** On
  macOS, `Cmd+C` copies the selection, `Cmd+V` pastes, `Cmd+F`
  opens and closes the find bar, and `Cmd+Click` opens a link, as
  Terminal.app and iTerm2 do. The `Ctrl+Shift` keys keep working
  there, the Control key stays the program's `Ctrl`, and Linux and
  Windows keep today's keys. The right-click menu shows the keys
  of the platform. See [docs/gui.md](gui.md#the-shells-tab).

- **The GUI's opening settle (#1177).** The window's content
  appears once it stands in its final shape: the first
  refreshes have landed and the layout is quiet, so a page never
  shows its columns or scrollbars moving into place. Every tab
  change settles its page the same way, and the rows of its
  tables rise into place from the top down, a few milliseconds
  apart. See [docs/gui.md](gui.md).

- **Keys for the GUI's shell tabs (#1163).** On the workspace
  page, with a terminal focused or anywhere else on the page,
  `Ctrl+Shift+T` opens a shell, `Ctrl+Shift+W` closes the current
  one, `Ctrl+Tab` and `Ctrl+Shift+Tab` move to the next and the
  previous shell, `Ctrl+1` through `Ctrl+9` jump to the Nth shell,
  and `Alt+1` through `Alt+5` jump to the page's tabs. The key
  itself never reaches the program inside the terminal, a shell
  action brings the Shells tab forward from any page tab, and the
  Workspaces list and the Images and Secrets pages do not answer
  them. See [docs/gui.md](gui.md#the-shells-tab).

- **A shell tab's title follows the program inside (#1163).** A
  program that sets the terminal's title (vim, less, ssh, or a
  prompt with an OSC title in its `PS1`) sets its tab's title too.
  A long title ends in an ellipsis and reads whole in the tab's
  tooltip, a cleared title shows the session's number again, and
  a renamed tab keeps its name.

- **The GUI paints state words in three colors (#1160).** A
  state word wears green when it is as it should be, amber when
  it is in flight or wants a look, and red when it failed or
  cannot be placed, across the workspaces list, the Mounts,
  Proxies, and Secrets tabs, the consent panel's last seconds,
  and a status line's refusal or outage. The word stays in the
  cell, a selected row keeps the highlight's text color, and a
  scheme switch repaints the colors with the window. See
  [docs/gui.md](gui.md#state-colors).

- **The GUI Images tab's button rows (#1161).** Each group now
  carries a button row under its table: **In the catalog** has
  Details…, Set as the default, and Remove…, and **Published**
  has Import…. The buttons act on the selected row and stand
  disabled until one is selected. The right-click menus stay
  beside them. See [docs/gui.md](gui.md#the-images-tab).

- **Synchronized output in the GUI terminal (#1193).** The
  terminal follows synchronized-output blocks (mode 2026): a
  program's open block holds presentation until it closes, and
  the block's whole change paints once at its end. Quitting pi
  in a GUI shell no longer repaints its whole session
  transcript chunk by chunk. A block stuck open past half a
  second presents anyway. The cell painter also runs about
  twice as fast, with identical pixels. See
  [docs/gui.md](gui.md#the-shells-tab).

### Changed

- **pi runs in regular mode in the guest (#1204).** The default
  `~/.pi/agent/settings.json` now sets `tuiMode` to `"regular"`,
  so a workspace's pi keeps its transcript in the terminal's
  scrollback and leaves the mouse to the terminal. pi's own
  default is fullscreen mode, which draws the transcript in its
  own viewport and handles selection itself. A workspace built
  from an earlier image keeps the old file, and its accounts stay
  in fullscreen mode until the setting is added by hand. See
  [docs/llm.md](llm.md#integration-with-the-pi-coding-agent).

- **The proxy's model list declares image input (#1197).** The
  `llm-models.ts` pi extension registers every model the daemon's
  proxy serves with image input. The proxy's model list carries no
  modality data, and the text-only declaration made pi drop an
  image for every model in the guest. A pasted image now reaches a
  text-only upstream, which answers an error where pi used to put
  a placeholder.

- **A spinner while a vision model describes a screenshot
  (#1201).** When the `screenshot` tool or `/screenshot` waits on a
  picked vision model, the TUI shows a bordered spinner that reads
  `Describing screenshot…` in place of the editor. Escape or `Ctrl+C`
  cancels the description at once, the result reports the saved
  capture's path, and during an agent turn the turn stops as well. A
  terminal that supports OSC 7501 program status shows pi as blocked
  with the same message for as long as the description runs. See
  [docs/screenshot.md](screenshot.md#who-sees-the-capture).

- **The holder writes the pipe's port into the guest (#1197).**
  The bridge holder runs one ssh command beside the screenshot
  pipe's reverse forward. The command writes the configured
  `screenshot_port` to `/tmp/msks-screenshot-port` in the guest,
  which the guest's tooling reads instead of assuming `8662`. A
  failed write is reported once on the holder's stderr and
  retried.

- **Terminal-only mode: the page's top margin stands at zero
  (#1189).** The blank band the mode kept above the shell tab
  strip is gone. With no egress hold waiting, the strip is the
  window's first row, flush under the title bar. The restore
  button keeps the right edge the terminal-only button stood at
  and rides the tab row. Leaving the mode by mouse may take one
  move of the pointer, and `Ctrl+Shift+M` still leaves the mode
  from anywhere on the page. See
  [docs/gui.md](gui.md#terminal-only-mode).

- **The workspaces list under a heading, the picker in the
  bottom bar (#1177).** The list's table sits under a
  **Workspaces** heading like the Images and Secrets tables, so
  the first table of every tab starts at the same height and
  the header line stays put across a tab change. The status line
  and the connection picker moved from the top of the page into
  the bottom bar beside Add, Edit, Clone, and Remove. Every
  table's headers now read bold and left, aligned with their
  cells, where Qt centered them; the Egress tab's headings and
  audit columns read capitalized and bold like every other
  page's. See
  [docs/gui.md](gui.md#the-workspace-list).

- **The GUI Secrets tab's coverage, buttons, expiry cell, and
  audit columns (#1165).** The COVERAGE cell reads `daemon-wide`
  or `scoped: ws-a, ws-b`, the mint form's own words, and the
  column's header explains them as a tooltip. **Renew…** and
  **Revoke…** sit on the button row beside **Mint…** and
  **Unlock…**, enabled on a selected placeholder row, and the
  right-click menu stays. The EXPIRES cell reads `2h · 14:20`, the
  countdown then the deadline's local clock, with the full date
  and time as its tooltip. The audit table shows TIME, KIND,
  PLACEHOLDER, and DEST, and the `!` column is gone. See
  [docs/gui.md](gui.md#the-secrets-tab).

- **The GUI's create and edit dialogs take sizes in human units
  (#1164).** `8G`, `8GiB`, and `8192M` all mean 8192 MiB,
  `0.5G` means 512 MiB, and a bare number is still MiB. The
  create dialog's fields read `Memory`, `Root disk`, and
  `Home disk`, with a line under them that restates the four
  sizes as you type. The edit dialog opens with the workspace's
  sizes in the same spelling (`8G`). See
  [docs/gui.md](gui.md#creating-a-workspace).

### Fixed

- **`/screenshot model` opens on the first Enter
  (#1207).** In the guest's pi, typing `/screenshot model` and
  pressing Enter left the command in the editor: the completion
  popup still showed `model`, and pi gives Enter to a visible
  completion. The popup now closes once the word is complete, and
  Enter runs the command. See
  [docs/screenshot.md](screenshot.md#the-screenshot-command).

- **Quitting pi in a GUI shell returns to the prompt at once
  (#1192).** With pi in regular mode (#1204), quitting no longer
  writes the whole transcript to the terminal on the way out. A
  home that still runs pi in fullscreen mode can add
  `"tuiMode": "regular"` to its `~/.pi/agent/settings.json`, or
  set `fullscreenExitOutput` to `"resume-hint"` to keep fullscreen
  and print only the resume hint. See
  [docs/llm.md](llm.md#integration-with-the-pi-coding-agent).

- **The TUI and the GUI stack no second bridge holder on a start
  another client makes (#1198).** The open client read the rows the
  moment the daemon announced `running` and spawned its own holder
  beside the one the starting client leaves. The second holder's
  screenshot pipe could not bind the guest port and retried every 30
  seconds, and each retry showed in the events tab as a
  `forward.opened` and `forward.closed` pair on port 22. The watch now
  gives that holder 30 seconds, and rows that read `listening` spawn
  nothing. A start over the raw API leaves no holder, and the client
  re-holds it 30 seconds after the workspace is running. See
  [docs/proxies.md](proxies.md#the-bridge).

- **The GUI terminal's copy drops the selection (#1182).**
  Ctrl+Shift+C and the right-click menu's Copy action now clear
  the selection after the copy. The highlight used to stay
  until the next click.

- **The version-skew notice gives the Linux remedy on Linux (#1174).**
  A client newer than the daemon used to send every operator to
  `msks-setup-macos-host`, including a Linux host where the daemon
  runs on the host itself. On Linux the notice now says to restart
  the daemon (`sudo systemctl restart msksd`) to pick up an
  installed newer release, or to rerun `msks-setup-debian-host` or
  `msks-setup-fedora-host` when the host still has the old one. On
  macOS it keeps the Mac tool. See [docs/cli.md](cli.md#the-cli-client).

- **Enter in the GUI's lists on macOS (#1161).** Enter on the
  selected workspace now opens its page, and Enter on a
  selected catalog image opens its record. Qt's own item views
  leave the key unhandled on macOS, so the tables answer it
  themselves.

- **The `virtiofsd-as` helper resets the signal handlers and the
  signal mask before it execs virtiofsd (#1176).** The helper
  ends a share's virtiofsd when the daemon dies, and that signal
  is `SIGTERM`. An ignored disposition and a blocked mask both
  survive an exec. A daemon that ignored or blocked `SIGTERM`
  in the thread that spawned the share would have left every
  such share running after a daemon restart. The share now dies
  with the daemon whatever the daemon's signal state.

- **The TUI listing keeps its refresh across the power keys
  (#1179).** `s` or `S` pressed while a listing refresh was in
  flight cancelled that refresh, so the listing could keep the
  old status until the next event and the next key skipped
  against it. The refresh now runs apart from the power keys'
  workers. A key pressed while the list rebuilds acts on the row
  the listing returns to.

- **The TUI's detail strip stays hidden until a row has focus
  (#1179).** The strip used to show as a blank two-line box under
  `No workspaces — c creates one.` while the listing was empty or
  still loading. It now appears when the first row has focus.

- **The TUI listing's power keys read the daemon's answer
  (#1187).** `s` on a stopped workspace flashes `alpha running`.
  `S` pressed before the listing re-read landed answered
  `Stop skipped: workspace is stopped` and sent nothing to the
  daemon. The row now takes the reply's status before the re-read
  flies, and the next power key reaches the daemon. The same
  holds for `s` after `S`.

## \[v0.10.2] - 2026-11-25

### Added

- **The GUI's terminal-only mode (#1152).** The workspace page's
  new button, beside the external-terminal button, hides the
  window's tab strip, the page's top row, and the footer, and
  `Ctrl+Shift+M` toggles the mode from the keyboard, a terminal
  too. The window then shows the shell tabs and the terminal in
  the layout of a tabbed terminal app, with the workspace's name
  in gray at the tab row's right end beside the button that
  brings the controls back. Shell sessions stay open across the
  switch, and the mode ends when the page closes. See
  [docs/gui.md](gui.md#terminal-only-mode).

### Changed

- **The GUI terminal's wheel scrolls twelve rows per notch
  (#1156).** The rate doubles from six rows, so a screen of
  scrollback passes in half the notches. Touchpad travel doubles
  with it: one cell height of travel counts as one notch. A
  program that enables mouse reporting, such as `less`, keeps
  its own scroll amount.

### Fixed

- **Local proxies serve a renewed workspace (#1158).** After a
  seed renewal or a disk reset regenerated the guest's ssh host
  keys, every local proxy's connection closed with an empty reply
  while `msks ssh` kept working. The bridge holder now pins the
  guest's host key in the same per-workspace cache `msks ssh`
  pins in, so the two agree after a re-key.

- **The GUI Egress tab's audit trail edges (#1154).** The audit
  table's top edge is level with the static allowlist list, and
  its bottom edge is level with the in-effect verdicts list, at
  every window height. The Allowlist button sits at the right end
  of the row under the table, and the trail's refusal note shows
  beside it on the same row.

## \[v0.10.1] - 2026-11-25

### Added

- **The GUI unlocks secrets (#1147).** `msks gui` offers the secrets
  unlock when the window opens and after a connection pick, and the
  Secrets tab's new **Unlock…** button asks for it by hand. A
  passphrase-encrypted `identity_file` opens one masked passphrase
  dialog per window session, the same rule the TUI keeps: the
  automatic flow asks once, the button asks again. The outcome shows
  on the status line of the screen the ask stood on. See
  [docs/secrets.md](secrets.md#the-clients-half).
- **`nested` / `MSKSD_NESTED` (#1140).** A daemon setting, off by
  default, that lets workspaces run VMs of their own: every
  workspace start passes cloud-hypervisor's `nested` flag, so the
  guest sees VMX (Intel) or SVM (AMD). Cloud Hypervisor v54 passes
  the feature through only when the config asks. On a v54 host a
  VM run inside a workspace fails with `no supported hypervisor`
  until the setting is on. On x86_64 the daemon warns at startup
  and `msksd doctor` names the module option when the host's KVM
  does not nest. See [docs/config.md](config.md).
- **The ssh key chapter (`docs/ssh-key.md`) (#1149).** The docs
  explain how to supply a key at first run, and now also what to do
  when it changes: a rotation, a lost key, or a new client machine.
  The chapter covers the config edit, the workspaces created with
  the old key (the stored line is fixed at create, #41), the secrets
  after the change, and retiring the old key. A seed renewal
  regenerates the guest's ssh host keys on the Debian and Fedora
  images, and the NixOS image keeps them. See
  [docs/ssh-key.md](ssh-key.md).

### Fixed

- **A refused start in the TUI opens the failure panel (#1139).** `s`
  on the workspaces list and Start on the workspace page show the
  daemon's whole refusal in a panel that stays until dismissed. When the
  hypervisor exited before the workspace came up, the panel adds a
  paragraph that points at `msksd doctor` on the daemon's host, and the
  shell action's refusal flash suggests the doctor instead of
  `msks console`. See [docs/tui.md](tui.md#the-workspace-tree).
- **A click clears a GUI terminal selection when the pointer
  moves a few pixels during the click (#1138).** A click whose
  pointer moves a little inside the pressed cell now clears
  the selection, also one a drag made through the window edge
  into the scrollback. A pointer that crosses most of the cell
  still selects it. A click held in the top or bottom row still
  autoscrolls, because a held pointer there scrolls the
  viewport.
- **`msks-term-popup` keeps a refused shell readable (#1143).** A
  pane command that exits nonzero now holds its window open with a
  press-Enter close prompt, so a startup refusal (an encrypted identity
  the agent does not hold) stays on screen instead of vanishing with the
  window a second after it opened. A clean exit closes the window as
  before. The launcher also appends its own startup failures to
  `msks-term-popup.log` under the tmp dir.

## \[v0.10.0] - 2026-11-24

### Added

- **The TUI and GUI re-hold the bridge (#1127).** An open
  client now restarts a workspace's bridge holder when its
  failure names the missing one — the holder died, or a start
  left none (a raw API call) — so the operator with a TUI or
  GUI open never runs `msks proxy hold` by hand. The re-hold
  spawns the same detached holder a start leaves, says the
  recovery with one line on the open screen, and tries three
  times per workspace per session with a 30-second backoff.
  See [docs/proxies.md](proxies.md#the-bridge).
- **The one-command install (#1119).** Every release carries
  two bootstrap wrapper assets, `msks-setup-debian.sh` and
  `msks-setup-fedora.sh`, so a stock host installs with
  `curl -LsSf <release-url>/msks-setup-debian.sh | sudo sh`:
  the wrapper fetches the release wheel, verifies every
  artifact it fetches against the release's checksum record,
  provisions Python 3.14 through uv when the host holds none,
  and runs the platform setup tool from a throwaway venv that
  is discarded afterwards. Flags after `-s --` pass through to
  the tool (`--no-service`, `--wheel`, `--uninstall`).
- **The rewritten installation chapter (#1120).** The
  chapter's install step is the one-command bootstrap, and its
  "what the setup lands" section inventories the full host
  shape: the `/opt/msks` venv, the system unit, the accounts
  and the shared socket, the client links, and the bundled
  `virtiofsd-as` helper. The by-hand chapter spells out the
  bootstrap's steps (interpreter, venv, wheel, tool
  invocation), and the uninstall chapter covers the removals
  and the kept `/var/lib/msksd` with its size report. The
  `uv tool install` step, the env-source step, and the quoted
  `command -v` invocation leave the docs. An install holding
  state under a user's home takes the clean cut #1118's entry
  states.
- **The `/opt/msks` venv install (#1118).** The Debian and
  Fedora setup tools gain a first phase that creates
  `/opt/msks/venv` — Python 3.14 provisioned through uv when
  the host holds none — and installs the msks release matching
  the tool's version into it. Every later phase resolves
  against that venv: the system unit's `ExecStart`, the
  `/usr/local/bin` links, and a second user's `msks` runs
  through them. `--wheel <path>` installs a wheel in hand
  instead of the fetched release, and a re-run with a newer
  tool refreshes the wheel (the upgrade path). `--uninstall`
  removes `/opt/msks`, keeps `/var/lib/msksd`, and the summary
  names the kept directory with its size; on macOS, the
  brew-prefix links point at the same shared install.
- **The clean cut for existing installs (#1118, #1080).** An
  install holding state under a user's home uninstalls and
  re-runs with the system-unit tool. The state directory does
  not move.
- **`-L` proxies reach whatever the workspace can reach (#1102).** The
  `-L` host field resolves inside the guest, ssh's own far-end meaning:
  `localhost` and `127.0.0.1` reach the guest's own loopback, and any
  other name or address rides the row for the guest's sshd to resolve.
  The reach rides a bridge a client holds: `msks start` — from the CLI,
  the TUI, or the GUI — leaves a background holder behind that serves the
  rows' connections over ssh, the same transport `msks ssh` rides (`msks
proxy hold` runs one on demand). The daemon runs no ssh and stores no
  key material. The rows serve while a holder stands; with none they read
  `failed` with the cause.
- **A right-click on a Mounts tab row (#1098).** The click
  selects the row and opens a menu with **Edit** and
  **Remove**. The entries carry the same paths and the same
  gating as the button row under the table. A press below the
  rows opens nothing.
- **The system unit install (#1094).** The Debian and Fedora
  setup tools now land `/etc/systemd/system/msksd.service`:
  the daemon runs under a dedicated `msksd` system account
  (nologin, `/var/lib/msksd`) with `SupplementaryGroups=msks-kvm`
  and `cap_net_admin`/`cap_net_bind_service` granted
  natively, serving the shared `/run/msks/msksd.sock` to the
  new `msks` access group — the executing user gains the
  membership, so members connect with no token and no CA. The
  `msks-caps` wrapper narrows to root:`msks` 0750 (the system
  unit no longer runs through it), `msksd doctor` gains rows
  for the group grant, the unit state, and the socket, and
  `--uninstall` mirrors the service, the account, and the
  group while naming `/var/lib/msksd` as kept. The user-unit
  machinery (runuser, linger, bus-race retries) is gone.
- **Owner-scoped secret events (#1090).** The live `secret.*`
  frames on the events websocket now reach the row's owner
  alone — mint, revoke, unlock, expiry, and the per-request swap
  and sighting frames publish to the owning uid, so one operator
  no longer receives another operator's placeholder names,
  coverage, or destination allowlists on a shared daemon. Root
  and the daemon's own uid keep receiving every owner's frames,
  and every non-secret event still broadcasts to every socket.
  The audit replay and the live tail now agree.
- **`socket_group` and the system socket candidate (#1093).**
  `MSKSD_SOCKET_GROUP` names the group the daemon hands its
  local listener to at startup: the socket's runtime directory
  takes 0750 and the socket 0660 under the group, so members
  connect with no token. The unconfigured client now falls to
  the system deployment's `/run/msks/msksd.sock` when its own
  runtime root holds no socket, and its refusal names both
  paths it checked. The daemon-side half of #1080's phase 3;
  the system unit that drives the setting lands with #1094.

- **Owner-tracked virtiofsd exec (#1088).** Each share's
  virtiofsd now runs as the workspace's owner: the daemon binds
  the share's vhost socket itself and hands the listening socket
  across the exec (`--fd`, no `--socket-path`), execing through
  the `virtiofsd-as` helper when the owner differs from the
  daemon's own account (`MSKSD_VIRTIOFSD_AS` overrides the
  helper's location) and directly, exactly as before, when it
  does not — the dev daemon and single-user installs keep their
  shape. A workspace whose owner uid no account holds skips its
  mounts with the named cause `owner uid N no longer exists` and
  still starts. The helper execs a virtiofsd path compiled into
  itself; a cross-owner start whose resolved virtiofsd differs
  from that target refuses by name, so the `MSKSD_VIRTIOFSD`
  override never drifts a cross-owner share onto a binary the
  daemon did not probe.

- **The `virtiofsd-as` owner-exec helper (#1087).** The repo
  carries the helper's C source: `virtiofsd-as <uid> --fd <n> --
<argv>` validates the invocation, clears supplementary groups,
  drops every capability, closes every other file descriptor, and
  execs one fixed virtiofsd path as the named uid with a scrubbed
  environment and one journal audit line per exec. Each release
  publishes a manylinux wheel per architecture carrying the
  static binary beside the pure wheel, and the NixOS module builds
  it from the in-repo source into
  `security.wrappers.msks-virtiofsd-as`. The daemon wiring follows
  in #1088.
- **Root addressing for shared names (#1084).** A bare workspace
  name two or more owners hold is its own error kind: exit 6 under
  `--json`, a line naming every owner otherwise. The workspace
  verbs (`start`, `stop`, `rm`, `clone`, `key`, `resize`, `mount`,
  `umount`) take `--user OWNER`, root only, to resolve the name
  against that owner's rows alone. `msks ls` gains an `owner`
  column when the listing spans owners, and the console and forward
  dials name the ambiguity with their own close code.
- **Owner-scoped secrets (#1083).** Placeholder rows record the
  uid of the peer that minted them, and the daemon-wide coverage
  (`[]`) became owner-wide: a mint with no workspace target
  reaches every workspace the minting user owns and never
  another user's. Every secret surface serves the caller's own
  rows alone — the listing, the audit trail, the pending
  ciphertext list, revoke, renew, the row fetch, and the unlock
  — with root and the daemon's user seeing every owner's. A
  bypass caller's scoped mint at another user's workspace is
  refused by name instead of minting a row that covers nothing.
  The audit trail names the owner on every lifecycle event.
  Existing databases migrate with every row owned by the
  daemon's own uid — behavior unchanged on a single-user host.
- **Workspace ownership (#1082).** Every workspace row records
  the uid of the peer credential that created it, and workspace
  names are unique per owner instead of per daemon: two users on
  one host may each hold a `build`. A listing serves the caller's
  own rows alone, and cross-owner access answers 404, the same as
  an absent workspace; root and the daemon's own uid see every
  row, and token minting, listing, and revocation is the
  operator's alone (403 for other group members). The TCP surface
  maps every caller onto the daemon's own uid, so the dev daemon
  and single-user hosts keep exactly their visibility. Existing
  databases migrate with every row owned by the daemon's uid —
  behavior unchanged.
- **The GUI workspaces page's Clone action (#1063).** The
  page's bottom button row carries a Clone button beside Edit,
  and a right-click on a row carries a Clone entry. Both ask
  for the new workspace's name in a centered prompt and clone
  through the same route `msks clone` uses.
- **The Egress tab's audit trail, verdict revoke, and
  deny-to-allowlist (#1051).** The GUI workspace page's Egress
  tab lists the workspace's in-effect verdicts and revokes one
  in place: a question first, then the destination gates again
  on its next connection. The audit trail fills the tab's right
  column with the recorded consent rows newest first,
  filterable by decision. Under `static` mode the recorded
  NXDOMAIN denials show what to allowlist next. A denial row's
  Allowlist action adds the denied name to the static allowlist
  through the egress policy route, the same write the allowlist
  editor makes.
- **The Egress tab's static allowlist editor (#1050).** The
  GUI workspace page's Egress tab lists the workspace's static
  allowlist entries and edits them in place: one entry added at
  a time (a host, a scope, an address, a CIDR, or `host:port`)
  and any entry removed, each write posting the whole new list
  through the egress policy route. A rejected entry surfaces
  the route's reason on the tab's note line and the list stands.
  Removing the last entry under `static` mode asks the same
  offline-workspace confirmation the mode switch asks.
- **The GUI launch plants the desktop shortcut (#1045).** On
  a Linux desktop, every `msks gui` launch installs the
  desktop entry and the icon renders under the XDG data home
  before the window opens. The taskbar shows the msks icon
  from the first launch after a fresh install. The
  `msks gui-shortcut` command stays as the manual plant and
  prints the entry's path.
- **The Egress tab on the GUI workspace page (#1049).** The
  page's tab strip carries an Egress tab beside Events. The tab
  shows the workspace's egress mode with its meaning and
  switches the mode in place through the daemon's egress policy
  route: a running workspace swaps its nft table in one
  transaction, and a stopped workspace builds the new posture
  at its next start. A switch to `static` with nothing
  effectively allowed asks its confirmation as a question
  popup before it takes effect.

- **The GUI docs chapter (`docs/gui.md`) (#1008).** The
  operator docs for the desktop client: the install, the
  connection with its picker, the workspace list and the detail
  page's four tabs, the Images and Secrets tabs, the zoom keys,
  and the external-terminal launcher. The chapter embeds an
  offscreen-captured screenshot for every documented surface,
  and the docs site's nav lists it beside the TUI chapter.

- **The macOS menu bar (#1007).** The GUI window on macOS
  carries the platform's menu bar with the two standard roles:
  About opens a dialog centered on the window naming the client
  and its version, and Quit on Cmd+Q closes through the
  window's own quit path, the one that closes every open shell.
  Linux and Windows windows keep the shape they have.

- **The GUI window follows the desktop's color scheme
  (#1006).** A light desktop paints the window light and a
  dark one dark, and a desktop switch mid-session repaints
  live with no restart. The terminal grids switch with the
  chrome, and a program's own terminal colors keep winning
  over the theme. A desktop that names no scheme keeps the
  palette the platform theme handed the window.

- **The GUI window icon (`msks gui-shortcut`) (#1021).** The GUI
  window carries a custom msks icon: it sets its desktop identity
  and icon directly. The new `msks gui-shortcut` command installs
  the desktop entry and the icon renders under the XDG data home,
  where a Wayland taskbar resolves them. The install is
  idempotent, so re-running it after an upgrade refreshes the
  renders.

- **The GUI connection picker (#1009).** The workspace list's
  status row carries a connection button naming the daemon the
  window talks to, with a menu of the client's presets: the
  config file's daemon aliases, the environment the shell
  exported, and the dev daemon's state-dir preset. Picking one
  reconnects the window over the picked values through the
  same config resolution `msks --daemon` uses; the open
  terminals close with the connection they belonged to, and
  the font zoom carries over.

- **Shells consent panel (#1001).** Pending connections appear above the
  terminal tabs with a countdown and inline Allow and Deny actions.
  Each action offers a duration menu, and the panel collapses when the
  queue is empty.

- **The GUI consent presenter (#1000).** The workspace page's
  decider registration now feeds a consent presenter: the
  held flows build as row models carrying the destination
  label and the countdown, and the verdict seam sends the
  same decide exchange `msks egress decide` makes with the
  duration chosen in the same action. A row clears on the
  daemon's resolved frame, never on the click itself. The
  consent panel that renders the rows from the GUI lands
  with the rest of phase 5.5.

- **The GUI's decider registration (#999).** Opening a
  workspace page in `msks gui` registers the page as that
  workspace's egress decider: an `interactive` workspace's
  held flows wait for the page while it shows, the pending
  holds and the egress posture arriving on the page's own
  events connection. Closing the page, going back, or closing
  the window retires the registration, and a dropped connection
  re-registers with a fresh snapshot on reconnect. A refused
  registration (an unknown workspace) flashes its reason on the
  page's status line; the consent panel that answers the holds
  from the GUI lands with the rest of phase 5.5.
- **The GUI whole-client zoom (#976).** Ctrl+Plus, Ctrl+Equal,
  and Ctrl+Minus step the UI fonts and the terminal fonts
  together, and Ctrl+0 sets both back to their defaults. The
  keys answer wherever the focus stands, and the window
  resizes around the visible terminal so its columns and rows
  hold at the new size. The terminal keeps its 7–28 pt window;
  the UI fonts clamp to 8–28 pt.

- **The GUI workspace events tab (#989).** The workspace page
  grows an Events tab beside Proxies. Each row shows a receipt
  stamp, the kind, and the summary, and a pushed frame appends
  without a manual action.

- **The GUI workspace proxies tab (#988).** The workspace page
  grows a Proxies tab beside Mounts. The tab lists the
  workspace's local and remote proxies with their live status, and
  Add and Edit parse the spec with the CLI's own parsers before
  anything sends; Removal asks first. Every proxy transition the daemon
  publishes for the open workspace refreshes the tab through the
  events feed, so a bind that fails while the tab stands paints
  itself.

- **The GUI workspace mounts tab (#987).** The workspace page
  grows a Mounts tab beside Shells. The tab lists the
  workspace's mounts with their derived state, and Add, Edit,
  and Remove compose the next whole list and save it through
  the route `msks mount` drives — next-start semantics, one
  flight at a time. A refused save shows the daemon's message
  and keeps the edited list, and the tab states in words while
  the configured set differs from the attached one.

- **The GUI workspace edit action (#986).** The workspace list's
  context menu grows an Edit action opening an editor in the
  create dialog's shape, seeded with the workspace's values.
  The changed sizes and topology send through the resize route
  `msks resize` drives, and the list refreshes when the daemon
  answers. A row whose edits wait for the next start leads its
  status with the `↻N` marker, the tooltip and the detail
  strips stating in words how many changes the next start
  applies. The name field stays off the form until the daemon's
  rename route lands.

- **The GUI secrets tab (#966).** The desktop GUI's Secrets
  tab renders the TUI secrets page: the placeholder listing
  with coverage, destinations, a live TTL countdown, the
  created label, and the lock state, with the sentinel panel
  beside it. Activating a row fetches its sentinel and fills
  the panel masked; the reveal sits behind a confirmation and
  the copy writes the real value to the clipboard. Mint opens
  the form (name, destinations, coverage, TTL, and the value
  typed or pasted directly), renew opens the TTL picker, and
  revoke asks first — each action landing through the bridge
  with the listing refreshed behind it. The TTL column counts
  down from the deadline epoch the presenter lands, with no
  daemon call per tick, and the audit group beneath replays
  the recorded rows newest first.

- **The GUI secrets presenter (#965).** The secrets tab
  gains its data layer: the daemon's placeholder rows build
  as plain row models carrying the id, coverage, destinations,
  TTL countdown, created label, and lock state; the sentinel
  panel's model masks the sentinel a row exposes beside the
  reach its prefix names; the mint form's TTL choices and the
  audit view's rows build as plain data. The tab's widget
  lands next on the `pyside-gui` branch.

- **The GUI images tab (#964).** The desktop GUI's Images tab
  renders the TUI image page's two groups: the daemon's catalog
  with the default marker, hash prefixes, and import dates, and
  the published channel with descriptions, sizes, build dates,
  and the `[imported]`/`[local]` membership markers. Rows the
  daemon's machine cannot boot stay off the published group.
  Activating a published row imports it behind a progress dialog
  that holds the wait while the daemon downloads, verifies, and
  floor-checks, and the catalog refreshes when the import lands.
  A catalog row opens its full record, Remove asks for
  confirmation, and Set as the default posts the designation;
  a right-click on a published row offers the import behind
  the same progress dialog; the channel URL follows
  `image_manifest_url` with the same default the TUI page
  reads. The two groups' standing tally —
  `N in the catalog · M published` — reads on a selectable
  line beneath the published pane, the workspace list's own
  bottom strip's style. Both panes' bold column headers
  read as one table's chrome and their columns sort — the
  sorted-by column carries its arrow, the dates and sizes
  ordering by their moments and whole MiB rather than their
  labels' spelling. The catalog pane's columns share the
  window's whole width, and the published pane spreads its
  spare room across the size, hash, build date, and marker —
  the description keeping its whole value with no empty tail —
  while a tight pane takes its character budget from the
  description first (a middle ellipsis down to a handful of
  characters), then the hash, then the ref and build date, with
  the size and marker reading whole in every pane a real window
  shows; no horizontal scrollbar ever hides a row's cells.

- **The GUI images presenter (#963).** The images tab gains
  its data layer: the daemon's catalog and the published
  channel's manifest build as plain row models. The catalog
  group carries each row's reference, default designation, hash
  prefix, and import date; the published group carries the
  descriptions, sizes, build dates, and the
  `[imported]`/`[local]` membership markers, with the rows the
  daemon's machine cannot boot left off. The tab's widget
  lands next on the `pyside-gui` branch.

- **The GUI workspaces pane fills its width (#964).** The
  workspace list's columns now consume the pane's whole width
  at any width over the default — the screen's own resize
  event read the viewport one layout pass behind, so every
  widen left its last delta as blank space beside the rows, a
  maximized window most of all. The pane's own resize re-flows
  the columns.

- **GUI row actions on the workspace list (#972).** The
  workspace list's toolbar is gone: the Start, Stop, Shell,
  External, and Refresh buttons leave the page and the rows
  reclaim the vertical space. Each row carries its own action
  cell — a start/stop toggle that reads the row's state (play
  while the workspace stands stopped or only created, stop
  otherwise) and a shell button. The list keeps refreshing on
  its own five-second timer. A right-click on a row opens the
  same actions plus Delete, which removes the workspace behind
  a confirmation. The external-terminal action moved to the
  workspace detail page.

- **GUI tabbed main window (#962).** The desktop GUI's main
  window now opens on a top-level tab strip: the Workspaces
  tab holds the workspace list and detail screens, and the
  Images and Secrets tabs of the plan land beside it. Arrow
  keys cross the tabs, and Up from the list's top row returns
  to the tab strip. A green gradient badge on the Workspaces
  tab — a drawn disc, bright at the crown and deep at the
  foot, a white plus on it, the pointing hand over it — opens
  the create-workspace dialog, the TUI create form's counterpart: the same fields,
  blank fields left to the daemon's defaults, and a refusal
  keeping the dialog open with the failure on its note line.

- **GUI top-level Shells tab (#974).** The
  workspace detail page's tab row, on the back arrow's own row,
  now carries a Shells tab, with Shells the default. The shell
  sessions live as subtabs inside the Shells tab. Opening a
  detail page with no shell subtab open creates one and focuses
  it.

- **GUI detail-page footer and tab-strip layout (#971).** The
  workspace detail page carries the listing's one-line
  description bar at its bottom, beneath the terminal tabs. The
  header line at the top is gone: the window title carries the
  workspace's name while the page shows. The tab strip's "+" is
  the last tab itself, directly right of the rightmost shell
  tab, instead of pinned to the strip's right edge.

- **GUI terminal-tab polish (#969).** A "+" at the right edge
  of the detail page's tab strip opens a new shell tab. Shell
  tabs keep a minimum width of about three characters beside
  their close button. The page's top chrome loses its Shell
  button: the "+" owns that action.

- **GUI workspace shells in terminal tabs (#954).** The GUI's
  workspace detail page is now tabbed: every open shell on the
  workspace gets its own terminal tab, titled with its session
  number (double-click renames). Shells keep running when you
  leave the page; closing a tab ends its session. A Shell
  action on the workspace list opens the page on a new shell
  tab, and an External action opens the same shell in the
  terminal named by `terminal_open_cmd`, as the TUI does.

- **The GUI terminal's user surfaces (#953).** The terminal
  widget grows grid-anchored selection (drag, double-click
  word, triple-click line, edge autoscroll into scrollback),
  the clipboard paths (Ctrl+Shift+C/V, PRIMARY select-then-
  middle-click, bracketed paste under mode 2004, OSC 52
  writes from the workspace), hyperlinks (OSC 8 and
  plain-text URLs, pointing hand, Ctrl+Click opening gated to
  browser schemes), and a find bar over the library's search
  API (live needle restart, match stepping with a counter,
  viewport highlighting). A program that enables mouse
  reporting owns the mouse: selection stands aside.

- **The GUI terminal widget (#952).** `msks/client/gui/terminal/` is the
  cell-grid screen over the libghostty-vt render state: per-cell colors,
  styles, and the cursor paint offscreen through the binding, keys and
  mouse encode through the library's encoders (legacy, application
  cursor keys, the Kitty keyboard protocol), wheel and Shift+PgUp/PgDn
  page the scrollback, resize with reflow reaches the child as
  `TIOCSWINSZ`, and font stepping follows Konsole's cell metrics from one
  measured face. Tabs arrive in #954; the widget ships ready for them.

- **The GUI terminal session (#951).** `msks/client/gui/termsession.py` runs a workspace shell's child on a pseudo-terminal — the `msks ssh` argv the TUI's own new-terminal action spawns — behind what becomes a terminal tab in the GUI window (#924 phase 4.5). The module is pure stdlib: the master fd is exposed for the tab's event-loop integration, resizes reach the child through `TIOCSWINSZ`, and a child that traps hangup is force-killed after a short grace so closing a tab never stalls the window.

- **The GUI's libghostty-vt binding (#950).** The terminal
  emulation core lands in the client as a pure-ctypes binding:
  the render state, the key and mouse encoders, and the
  selection gesture the phase 4.5 terminal widget will drive,
  with every C struct mirrored exactly (padding members
  included) and pinned by size against the ABI. The base
  install carries `pyghostty` (#1023), whose wheel bundles the
  official `libghostty-vt` build the binding loads.
- **The GUI live events feed (#946).** The window subscribes to
  the daemon's events socket over the bridge's loop: a pushed
  lifecycle frame refreshes the screen the operator sees at
  once, the polling timer staying the backstop, and lands in
  the workspace page's events section. A dropped socket
  reconnects with backoff, and the outage flashes on the
  current screen while it lasts.
- **The GUI workspace detail page (#945).** Opening a row in the
  workspace list — a double-click or Enter on it — swaps to that
  workspace's page: the mounts with their derived state and
  next-start note, the daemon's health facts, and the recent
  events, refreshed on the listing's own cadence. A failed
  refresh flashes while the page keeps its
  content; Back (or Escape) returns to the list, and a
  workspace another surface deleted closes its page behind a
  notice.
- **The GUI start and stop actions (#940).** The workspace
  list's Start and Stop buttons act on the selected workspace
  over the same bridge the refresh uses; Stop asks for
  confirmation, with No as the default, and a refusal sends
  nothing to the daemon. The list refreshes when an action
  finishes, flashing the outcome the daemon's answer carries; a
- **The GUI start and stop actions (#940).** The workspace
  list's Start and Stop buttons act on the selected workspace
  over the same bridge the refresh uses; Stop asks for
  confirmation, with No as the default, and a refusal sends
  nothing to the daemon. The list refreshes when an action
  finishes, flashing the outcome the daemon's answer carries; a
  failed action flashes the status line and the rows stand. A
  selectable detail line at the listing's bottom follows the
  selection: the held workspace's identity, posture, and costs
  on one `·`-separated row (id, image, cpu, memory, egress
  mode, the mounts and pending markers, and the storage cost
  the capacity report charges).
- **`msks gui` (#928).** The desktop GUI client opens its
  phase-1 window shell on Linux and macOS: a PySide6 front end
  over the daemon's REST API, with the workspace screens to
  follow. The GUI dependencies are base dependencies (#1023);
  a session without a display hears a one-line notice instead
  of a crash.
- **The GUI workspace list (#939).** The GUI window opens on
  the workspace list: the TUI listing's five columns (name,
  status with the unresolved-mounts marker, id, image,
  created), refreshed on a five-second tick and a Refresh
  button. A refused refresh flashes a status line while the
  rows stand; an outage holds its message until the daemon
  answers again. The window opens at 1280×720 and runs its
  chrome (tabs, buttons, table headers) at 15 logical pixels,
  the size the terminal face's 15pt default rasterizes to on
  macOS. The window converts the anchor through the screen's
  font dpi, so the chrome opens at the same size on every
  platform (#1056). The terminal content keeps its own 15pt
  default. `MSKSC_GUI_FONT_SCALE` multiplies the anchor
  (default `1.0`; `1.3` restores the old macOS look). The
  window reopens at the geometry it last closed at, kept under
  the client state root (see `docs/cli.md` → Client
  environment).

### Removed

- **The GUI workspace page's Info tab (#1017).** The page's
  tab strip carries Shells, Mounts, Proxies, and Events. The
  health facts the tab showed are gone with it. They are the
  daemon's own facts rather than the workspace's: the machine
  fact already reads through the images tab, and a refused
  read's flash on the status line already tells the daemon's
  reachability. The page no longer asks the daemon for a
  health doc on its refresh tick.

### Changed

- **`-L` rows need a bridge holder (#1102).** A forward proxy now
  resolves its host field inside the guest, so its reach moved from the
  guest's tap address to the guest's own loopback and beyond it to
  whatever the workspace can reach, carried by a client holder. Rows
  serve while a holder stands; start the workspace from any client to
  leave one behind, or run `msks proxy hold`. The ephemeral `msks
forward` keeps its tap dial unchanged.
- **Owner-scoped event frames (#1095).** Every frame on the
  events websocket that names a workspace now reaches that
  workspace's owner alone: the workspace lifecycle, mounts,
  resize, and home-volume import/export frames, the consent
  hold and rules frames, and the proxy and forward frames join
  the `secret.*` frames #1090 scoped, so one operator's socket
  no longer receives another operator's workspace ids, names,
  or hosts on a shared daemon. Root and the daemon's own uid
  keep receiving every owner's frames, the daemon-wide families
  (`image.changed`, `storage.pressure`) still reach every
  socket, and the daemon's journal still records every publish.

- **Proxy direction terms (#1032).** The TUI's proxy form, the
  `msks proxy add` help and listings, and the docs chapters now
  call the two directions local (`-L`) and remote (`-R`), ssh's
  own pair, named for the side that holds the listener. The
  flags, the form's stored values, and the API rows are
  unchanged.
- **The conditional certifi overlay (#785).** The nix builds
  swap certifi in the python package set only while the pinned
  nixpkgs sits below the `certifi>=2026.10.23` floor pyproject
  declares. The pinned nixpkgs (unstable, since #991) already
  carries 2026.10.23, so `nix build .#server` serves the python
  closure from the binary cache instead of rebuilding httpx,
  mitmproxy, and every other dependent from source. An eval-time
  gate (`nix/certifi-gate.nix`) names a composed set that misses
  the floor, reading it from pyproject.toml.
- **The GUI's Images and Secrets pages update on pushed events
  (#1060).** Both pages re-read at every show and on the
  daemon's pushed frames: an `image.changed` frame repaints the
  catalog group, and the secret family's frames repaint the
  placeholders and audit (batched into one re-read per half
  second, so a busy interception stream stays cheap). The
  events feed's drop flashes on whichever page owns the window,
  and a reconnecting feed re-reads the page whole. The two
  Refresh buttons are gone; the published channel manifest
  still refetches only through the page's re-reads because the
  channel publishes no frame.
- **The attached clients follow the daemon over the events
  websocket instead of polling (#1019).** The TUI's workspaces
  list, workspace page, and proxies page, and the GUI's
  workspaces list and workspace page, re-read on the frames
  the daemon publishes instead of on timers. The daemon now
  publishes `workspace.created`, `workspace.removed`,
  `workspace.mounts`, the request-path `workspace.status`
  transitions (start, stop, reset), and `image.changed` on
  catalog moves, beside the watcher's scans. A daemon at rest
  receives no periodic listing traffic from an attached idle
  client. The TUI's shell counts stay on a local timer (they
  read per-host session records, not daemon state). See
  `docs/tui.md` and `docs/gui.md`.
- **The GUI dependencies are base dependencies (#1023).** A
  plain `pip install msks` now installs the desktop GUI, and
  the `gui` extra is gone. Every install gains about 250 MB
  (PySide6-Essentials and pyghostty). A stripped install
  (PySide6 removed or broken) hears a reinstall hint on `msks
gui`. The nix package keeps the daemon's closure lean, so on
  a nix host the GUI is a pip surface (`pip install msks`).

- **The GUI's active-tab mark (#1012).** The main
  Workspaces/Images/Secrets strip and the workspace detail
  strip paint the selected tab with an accent-tinted
  background, so the page the operator stands on reads at a
  glance. The tint follows the session palette, and the
  keyboard keeps a dotted rim on the focused tab.
- **The workspace detail panes use the window's full width
  (#1025).** The Mounts, Proxies, and Events tables share the
  whole horizontal width of their tab: the mode, state,
  direction, and status columns hug their text, and the wide
  columns share the remainder. Event summaries wrap within the
  pane instead of growing a horizontal scroll.
- **The GUI's popups read with capitalized words (#1018).**
  The create, edit, and mint dialogs' field labels, note
  lines, and buttons now start each label with a capital
  letter — `Name`, `VCPUs`, `Memory MiB`, `Create`, `Cancel`,
  `Apply` — matching the window titles' own style, and the
  mount and proxy forms' labels and `Save`/`Cancel` buttons
  follow. Acronyms keep their unit casing (`VCPUs`, `TTL`,
  `MiB`).

- **The GUI's tab pages carry their action buttons at the
  bottom (#1011).** The Workspaces tab's green plus badge is
  gone from the tab strip: Add, Edit, and Remove sit in a row
  at the bottom of the page (the pattern the detail page's
  Mounts and Proxies pages set), Add opening the create dialog
  and Edit and Remove acting on the selected row, Remove
  behind its confirmation. The Secrets tab's Refresh and Mint
  buttons and the Images tab's Refresh button moved from the
  top of their pages to the same bottom row.

- **The GUI secrets page fills the window and drops the
  sentinel pane (#997).** The placeholder listing and the audit
  table each span the window's whole width, their columns
  sharing the spare space and re-flowing on every window
  resize. The Sentinel pane beside the listing is gone: a row
  activation or a mint no longer fetches a sentinel, and the
  sentinel stays the CLI's and the TUI's own surface.

- **The nixpkgs pin behind devenv and the release flake now
  tracks `nixpkgs-unstable` (#990).** The dev shell's fontconfig
  matches the host's generation again, and the warning wall at
  `msks gui` startup is gone. Guest images and flake outputs
  build against unstable, so operators building images from
  source see closure diffs from the channel move.

### Fixed

- **The Images and Secrets tabs recover from a daemon outage like
  the workspace list (#1132).** An outage on either tab kept its
  text only until the events feed's next retry flash, then the
  line went blank, and the recovery said nothing. Both tabs now
  keep the outage's text between the feed's retries and say the
  recovery once when a read answers. The refresh an action
  triggers no longer clears the action's result from the status
  line.
- **The GUI's workspace list recovers when the daemon comes up
  late (#1129).** A window opened while msksd was down showed an
  empty list and a standing outage message, and nothing re-read
  the list once the daemon answered. The events feed now
  triggers one re-read on every connection it makes. The open
  tab fills by itself when the daemon comes up, a hidden tab
  at its next show, and the status line says the recovery once.
  See [docs/gui.md](gui.md#the-workspace-list).
- **GUI terminal rendering on scaled and Wayland displays
  (#1113).** The terminal's cells now paint straight onto the
  widget, one damaged row at a time over its own background
  fill, with no offscreen pixmap kept between paints. The
  pixmap design broke twice on display scaling — a stale
  band below the blit on KDE Wayland fractional scaling
  (the window at 1.5 while the backing store rendered at the
  screen's integer 2.0), and ghosted, overprinted glyphs
  under fast-scrolling output — and both classes are gone
  with the pixmap. The scrollbar now draws in a small lane
  reserved right of the grid, so its thumb never covers a
  cell.
- **GUI terminal on scaled displays (#1110).** The cell
  cache from #1107 built its pixmap at the widget's logical
  size. On a display with scaling (a device pixel ratio above
  1), the blit left most of the terminal showing stale
  frames: stacked prompts, garbage glyphs, and cursor blocks
  that never cleared. The pixmap now carries the screen's
  device pixel ratio, so painting stays in logical
  coordinates and the blit lands one device pixel for one
  device pixel. A window moved between displays that scale
  differently rebuilds its cache.
- **GUI terminal echo lag (#1104).** Typing in a GUI
  terminal tab now echoes immediately: each paint repairs
  only the rows the terminal emulator flags as damaged
  instead of repainting every cell of the grid (a full-grid
  paint cost ~80 ms per echoed character, and echoes fell
  further behind while typing). The cells live in an
  offscreen cache blitted whole each frame, so the cursor,
  scrollbar, and overlays draw fresh above it.
- **The GUI terminal's exit report (#1099 follow-up).** The
  kernel closes a child's terminal fds before the child is
  waitable, and the exit signal could fire in that window with
  the 0 placeholder instead of the program's real exit code —
  a race a loaded CI runner caught. The report now waits out a
  half-second grace for the child to finish exiting, so the
  real code goes out; a wedged child still reports on time.
- **Ctrl+Click on a URL wrapped across terminal rows
  (#1100).** The GUI terminal's URL scan now stitches
  soft-wrapped rows into one text before matching, and every
  row a URL touches carries the whole link — a Ctrl+Click on
  any fragment opens the full URL instead of the piece on the
  clicked row. A match reaching the last cell of a line that
  still wraps past the viewport's bottom edge answers no
  link, because the rows below the viewport cannot be read to
  tell whether the address continues there.
- **`nix build .#server` on the unstable nixpkgs pin (#1062).**
  The build again completes: the pinned set's filelock, openai,
  and huggingface-hub had moved outside litellm 1.106.2's
  dependency ranges and the runtime check refused the litellm
  package. The litellm pin moves to 1.110.2 (pyproject floor,
  uv.lock, and the nix wheel together), whose widened ranges
  cover the set. A new Nix flake workflow builds the server
  closure on the nix surface, so this break class fails a check
  run instead of waiting for a human's `nix build`.
- **Tab completion inside a GUI terminal (#1066).** Tab and
  Shift+Tab in a terminal subtab now reach the program inside
  the terminal: Tab sends the tab byte the shell's completion
  reads, and Shift+Tab sends the shifted tab (the
  reverse-completion sequence). Before, both moved the
  keyboard focus to the next widget in the window.
- **Esc in a GUI shell terminal (#1099).** Pressing Esc while
  a workspace shell's terminal holds focus no longer leaves
  the workspace page: the key reaches the program inside the
  terminal (canceling a vim search, leaving a `less` view).
  The find bar's Esc closes the bar instead of navigating
  Back, and a standing tab rename's Esc cancels the edit the
  same way. Esc still goes Back while the page's own controls
  hold the keyboard focus.
- **The secrets audit rows' time labels (#1064).** The audit
  rows in the GUI's Secrets tab and in the TUI's audit views
  show the local clock for a row from the viewer's own day,
  and carry the date beside the clock for a row from an
  earlier day. The GUI's `!` column header carries a tooltip
  naming the sighting marker and where the marker appears.

- **The GUI's menu bar on macOS names the client msks
  (#1043).** The bold application menu read the process name —
  `python3.14` for the console script — until the application
  name was set; it now reads `msks`, matching the About dialog.
- **A mount set that leaves and returns re-provisions again
  (#1069).** The seed's instance-id suffix now mixes the
  workspace's seed epoch beside the CA and the mount-set
  fingerprint. Before, removing a mount and re-adding it
  re-minted an instance-id the guest had already consumed, and
  cloud-init skipped the re-provision — the daemon reported the
  share attached while the guest path stayed an empty
  directory. Every renewal now bakes an id the guest has not
  consumed, and the guest installs the returned share's mount
  unit on its next start.

- **`scripts/cut-release.sh` resolves `python3` explicitly
  (#1038).** The script's two python reads (the tagged version
  and the release-notes gate) ran a bare `python`, which macOS
  and minimal Linux hosts do not ship; they now run the
  resolved `python3`, and a host with none — or an interpreter
  too old for tomllib — dies naming it before anything is
  pushed.

- **Ctrl-C closes the GUI window (#1037).** A Ctrl-C in the
  terminal that started `msks gui` now closes the window
  through its own quit path, the one the close button takes.
  Every open shell closes and the window geometry saves. The
  client exits with 130, the terminal's interrupted status.

- **Quitting the GUI with a focused terminal no longer
  crashes the client (#1034).** Closing a terminal freed the
  emulator's handles before Qt delivered the focus-out of the
  hide that followed. The focus report read the freed handle,
  and the read took the process down on every quit whose
  close path touched a focused shell, the menu bar's Cmd+Q
  included. A closed screen now answers no focus report and
  sizes no grid.

- **The GUI's popups open centered on the main window
  (#1014).** Every popup the GUI opens — the create and edit
  forms, the mounts and proxies add and edit forms, the image
  detail record and import progress, the mint form, and the
  stop, delete, remove, and revoke questions — opens centered
  on the main window instead of wherever the window manager
  places it: the forms were parentless top-level windows, and
  the questions' placement stayed the manager's even parented.

- **The GUI window names its own widget style (#937).** A
  desktop session no longer picks the style for the window: a
  Plasma session with the union style selected previously
  handed the bundled Qt a session-built style plugin that
  failed to load its parts. The window runs the Fusion style,
  which ships inside the Qt binding itself, and a user's own
  `QT_STYLE_OVERRIDE` still wins.

## \[v0.9.2] - 2026-11-15

### Fixed

- **The TUI start unlock survives a daemon outage (#920).** A
  tree opened before msksd answers now unlocks by itself once
  the daemon arrives: the start flow's outage arms one re-run,
  and the listing's first answered refresh fires it. The
  passphrase prompt still fires only when the daemon reports
  locked rows, and the automatic prompt stays once per session.
  See `docs/secrets.md` → the unlock window.
- **`pubkey_file` docs name the unlock's key requirement (#918).** The
  docs and the first-run config template's comments claimed
  `pubkey_file` alone covers a key you keep encrypted. The secrets
  unlock still reads the private half from `identity_file`, and those
  passages now say so.
- **The unlock note renders whole (#916).** The passphrase
  prompt's note fits the form's 50-column text width:
  `<key file> passphrase for secrets unlock`. Key names up to
  20 characters render whole. See `docs/secrets.md` → the
  unlock window.

### Added

- **Confirm dialogs show their answer keys (#913).** Every TUI
  confirmation shows `y / n` beside its question. `y` (or
  Enter) confirms. `n`, `q`, or Escape cancels. See
  `docs/tui.md` → the workspace tree.
- **Pending-changes marker on the workspace page (#912).** The
  workspace page's header now carries the `↻N` marker ahead of
  its status while changes wait for the next start, the same
  count the workspaces list's status column carries. The marker
  leaves when the next start applies the change. See
  `docs/tui.md` → the workspace page section.
- **The TUI stands through a daemon outage (#904).** The
  workspace TUI stays open when msksd goes away. The list's
  status line carries a `daemon down — retrying` marker while
  the five-second refresh finds no answer. Every refresh tick
  is a retry, and a one-line flash notes the recovery when the
  daemon answers again. A daemon that answers with a refusal
  (a revoked token, a failing endpoint) flashes its reason on
  every refresh. The down marker never stands for a refusal:
  the daemon is answering. See `docs/tui.md` → the status bar
  section.
- **Pending-changes marker on workspace rows (#898).** A
  workspace whose edits wait for its next start now shows it in
  the TUI's list: a `↻N` marker ahead of the status word counts
  the waiting changes (a mounts edit, a sizes or topology
  edit), and the focused row's facts strip carries the same
  count as `next ↻N`. Every workspace row carries a new
  `sizes_pending` flag, derived like `mounts_pending`: the
  row's deferred sizes (root growth, vcpus, memory) against
  the ones the last start booted. After an upgrade every
  existing workspace reads pending until its next start: no
  record of a last boot exists yet. The marker leaves as each
  one starts.

### Changed

- **Workspace page width (#902).** The page's header lines and
  status lines fill the terminal's width; the action panel keeps
  its centered block. On a terminal wider than 80 columns, the
  spare width fills the clipped meta fields whole (the id
  first), and the mounts and proxies lines use the full width
  before any ellipsis.

## \[v0.9.1] - 2026-11-14

### Added

- **Workspace page quick keys (#880).** Every action row on
  the workspace page has a one-key shortcut: `e` the egress
  consent page, `h` a shell, `m` the mounts editor, `p` the
  proxies page, `t` the edit dialog, `c` the clone prompt. The
  footer's descriptions abbreviate (the list's own style) so the
  whole key set fits one 80-column line, and the `?` help
  overlay lists every key.
- **Proxies page edit form (#890).** `e`, Enter, or a click on a
  row opens the add form's own shape seeded with that row
  (direction, spec, label) and saves it through a new
  `PATCH /api/v1/workspaces/{id}/proxies/{proxy_id}` route; the
  daemon closes the old listener, rewrites the row, and binds
  the new spec at once, with the create's own conflict rules.
  `D` keeps the remove behind its confirmation.

### Changed

- **Workspace page edit action renamed (#890).** The action row
  that opens the sizes-and-topology dialog reads `Edit` (from
  `Settings`), matching the `e`/Enter edit vocabulary the mounts
  editor and the proxies page carry.

- **Workspace page block (#881).** The page's whole content (the
  header lines, the status lines, and the action panel) sits in
  one 80-column block, centered on a wider terminal. At 80
  columns and under, the layout is unchanged. On a wider terminal,
  each meta field keeps its fixed clip, and the egress consent
  page widens its own meta line as before.
- **Guest toolchain pins (#887).** The Debian, Fedora, and
  NixOS guests ship pi 1.5.2 (from 1.4.0), herdr 0.9.5 (from
  0.9.1), and Claude Code 2.1.325 (from 2.1.281). Workspaces
  built from new guest assets report the new versions; existing
  workspaces keep their baked toolchain until rebuilt.
- **TUI mounts editor restyle (#886).** The mounts editor and its
  add/edit form now use the proxies page's style: a status line with
  the mount count beside the workspace's name, a bordered SPEC/STATE
  column list, and an empty state that names the add key. The form
  (host path, guest path, mode) centers on the page; a field the
  CLI's parser refuses keeps the form standing with the rule on its
  note line.
- **Workspace page action rows (#882).** The name and the
  description now sit in two aligned columns: every description
  starts at the same offset across the use, configure, and power
  groups, and the em-dash separator is gone. A dimmed power row's
  reason takes the description's column. The shell and edit
  rows now read **Shell** and **Settings**.

### Fixed

- **TUI proxy add form cancel (#888).** The form's Escape and
  `q` keys now dismiss it. Both bindings existed but had no
  action behind them, so the keys did nothing.
- **`msks agent-card` (#877).** The card now says the exit codes
  3, 4, and 5 hold under `--json` only: without it, every API
  failure exits 1 with its detail on stderr. It also documents
  how a session finds the workspace key's private half
  (`MSKSC_IDENTITY_FILE`, `identity_file`, or the ssh agent), the
  exact-match rule for `name:version` image refs, and the
  `login_user` account ssh logs in as.
- **TUI form notes render whole (#894).** The proxies form's and
  the mounts editor's standing notes fit the form's text width,
  so no line clips to an ellipsis at an 80-column terminal. The
  workspace edit note's wording is plainer in the same pass
  (#892).

## \[v0.9.0] - 2026-11-13

### Added

- **Version-skew marker in the TUI status bar (#852).** While
  the daemon's version and the client's differ, the workspaces
  status line carries a warning-colored marker after the URL
  naming both versions (`msksd 1.2.3 ≠ client 1.2.4`); the
  CLI's one-line notice with its remedy keeps its own behavior.
  The marker refreshes on the listing's five-second reload, a
  `/health` connection that fails leaves the standing marker in place,
  and the marker leaves when the versions agree. The URL and the
  state summary crop before the marker does, so the marker stays
  visible at 80 columns.

- **Open-shell counts in the TUI (#858).** Every interactive
  `msks ssh` writes a heartbeat session record under
  `~/.local/state/msks/sessions` (relocatable with the `state_dir`
  key / `MSKSC_STATE_DIR`), and the workspaces list shows the live
  count beside a row's status word (`»2`) and on the detail strip
  (`shells »2`); the workspace page's header carries the count
  beside the status (`2 shells open`). The count covers the shells
  open on the host the TUI runs on, survives a tree restart, and a
  session killed without cleanup stops counting about twenty
  seconds after its death. Windows the tree opens carry the
  workspace's name and the session record's five-character token in
  their title (`msks tree-vm f8x2n`); a later release will use the
  token to match a session to its window.

- **Detail strip at the bottom of the workspaces list (#849).**
  A two-line strip renders at the bottom of the listing's box
  and carries the focused workspace's quick facts: name, id,
  image, cpu count, memory, egress mode, the mounts-issue count,
  and the storage the workspace charges the state disk (the same
  root-plus-home cost `msks storage` reports). The strip follows
  the selection and refreshes on the list's standing cadence;
  the storage figure uses a cached capacity report, so moving
  the selection costs no roundtrip. An empty list hides the
  strip.

- **State summary in the TUI status bar (#853).** The workspaces
  count now carries a summary of the fleet's states:
  `4 workspaces · all stopped` when every row holds one state,
  or each state's count (`4 workspaces · 1 running 3 stopped`)
  when they mix. The summary follows the standing five-second
  refresh. The daemon's URL comes after the summary, and it crops
  first when the terminal runs narrow.

- **`?` key overlay in the TUI (#851).** Every page of the tree
  answers `?` with an overlay listing that page's bindings,
  hidden ones included (the escape leaves among them), each with
  its action label. Any key closes the overlay. The footer stays
  as it was. The overlay is where the full key list lives.

- **Sortable listing columns in the TUI (#854).** Clicking a column
  header in the workspaces list sorts by that column; clicking the
  active header flips the direction, and the arrow in the header
  shows the active sort. CREATED sorts by creation date (newest
  first by default), STATUS in lifecycle order, the other columns
  by their text. You sort by clicking a header; the keymap stays
  as it was, and the sort holds across the standing refresh.

- **Agent reference card (#842).** `msks agent-card` prints the agent
  reference card: one markdown text grouped by task (read state,
  lifecycle, run inside, egress consent) with the status vocabulary
  and the exit-code table whole. The same text is the
  [Agent Reference Card](https://mcdonc.github.io/msks/agents/)
  docs chapter. The card covers the status vocabulary, the exit-code
  table, and the non-interactive egress-consent flow.

- **Machine-readable CLI (#841).** Every printing `msks` command takes
  `--json`: the mutating commands emit the affected workspace row as one
  JSON document (`create`, `clone`, `start`, `stop`, `resize`, `mount`,
  `umount`; `rm` prints one JSON line per removed id), the read commands
  emit their documents, and failures print
  `{"error": {"status": ..., "detail": ...}}` on stderr. The exit codes
  are a documented table (docs/cli.md): 3 not found, 4 name conflict,
  5 wrong state. `msksd doctor --json` prints its check rows as one
  document. Human output is unchanged.

- **`host.msks` (#816).** Every egress workspace's resolver answers the
  name with its own tap address, so a guest connects to the gateway by name
  (`https://host.msks:8660` with a `-R 8660:localhost:8660` proxy stored)
  instead of discovering it through `ip route show default`. The name
  answers in every consent mode, feeds no policy row or address pin, and
  a well-formed internet-class query for it is never sent upstream.
- **The proxies docs chapter (#814).** `docs/proxies.md` owns the whole
  proxies story in one place: the persistent proxy port concept, the
  `-L`/`-R` directions in ssh's grammar, the live status vocabulary, and
  worked examples for both the CLI and the TUI. The networking, CLI, and
  TUI chapters link to it where they introduce proxies.
- **The TUI's proxies page (#809, part of #807).** Each workspace's
  page opens its own proxies page (the action row, or `p`), which adds
  and removes that workspace's persisted `-L`/`-R` proxies with the
  same spec grammar `msks proxy add` reads, and shows live status —
  refreshed on every proxy transition the daemon publishes beside a
  five-second standing re-read. The workspace page carries a compact
  port-pair line (`8888 -> 8889 | 8881 <- 8882`), each pair green
  while it listens and red while its bind refused.
- **`msks proxy` (#808).** A new command manages the daemon's
  persisted workspace proxies: `add` with ssh's own spec shape
  (`-L [BIND:]PORT:HOST:PORT` binds the daemon host and bridges to
  the workspace; `-R RPORT:HOST:TPORT` binds the workspace's
  gateway and bridges to a daemon-side service), `ls` with live
  status, and `rm`. The daemon owns every listener, rows live in its
  database, every workspace start re-establishes them, and `add`
  is idempotent for an identical spec. Part of #807.

### Changed

- **The workspaces list's ID column (#849).** The EGRESS column
  became an ID column: each row carries its immutable workspace
  id. The egress posture reads on the detail strip instead,
  beside the row's other facts. Clicking the ID header sorts by
  the id itself; the NAME header keeps the name sort.
- **Graded status colors in the TUI listing (#850).** The
  workspaces list colors the status column by state class:
  `running` keeps the success color, `stopped` and `created`
  render in the muted text color, `starting`, `stopping`, and
  `paused` in the warning color, and `absent` and `unknown` in
  the error color. A fleet at rest reads calm. The transitional,
  paused, or lost row is the one the eye finds. The `✗N` mounts-issue
  marker keeps the warning color beside whatever color the
  status word takes.

- **The arm64 stop caveat is documented (#826).** cloud-hypervisor's
  arm64 power button (the PL061 gpio key) delivers only the first
  press, so a `msks stop` cannot rescue a lost press by re-pressing.
  The fallback kill at the `MSKSD_SHUTDOWN_TIMEOUT_S` deadline carries
  the stop instead. Size that setting to cover the guest's slowest
  clean shutdown. A value above 120s outlives the client's wait, and
  the client reports a timeout while the daemon still finishes the
  stop.

### Breaking

- **`terminal_title` / `MSKSC_TERMINAL_TITLE` removed (#858).** The
  window title template setting is gone; every terminal window msks
  opens for a workspace shell now carries one fixed title,
  `msks <workspace-name> <token>`, and windows msks does not open keep
  the terminal emulator's own title. The client refuses a config
  file that still carries the `terminal_title` key, on every
  command, until the key is deleted — delete it from your
  `msks.yaml` and unset the variable. The refusal names the key.

### Fixed

- **The legacy create default cmdline (#833).** A create with
  explicit kernel/rootfs paths and no `cmdline` now boots with
  this host's console device in the default: `ttyAMA0` on an
  aarch64 daemon, `ttyS0` elsewhere. The console answers on
  both; the catalog-record path was already per-arch and is
  unchanged.

- **`msks doctor` qemu-kvm apk hint (#832).** The hint now
  prints this host's Alpine emulator package:
  `qemu-system-aarch64` on an aarch64 host,
  `qemu-system-x86_64` elsewhere.

- **`msks doctor` cloud-hypervisor install hint (#831).** The
  hint now prints this host's release assets: the `-aarch64`
  builds on an aarch64 host, the unsuffixed x86-64 builds
  elsewhere. The installation chapter states the same
  architecture distinction beside its install block, and
  installs `ch-remote` under the unsuffixed command name the
  demo and dev tooling calls.

- **Egress slice release on a failed teardown (#828).** A `detach`
  whose table or tap delete raised left the workspace's slice
  claimed with no retry path, so the next `msks start` was refused
  with "held by another live workspace" until the daemon
  restarted. A failed teardown now leaves a record the
  workspace's next stop or start retries, and the slice releases
  when that retry completes.

- **Workspace starts after a guest-initiated poweroff (#825).** A
  VMM that exited outside the stop flow — the guest's own `poweroff`,
  or a guest landing its shutdown just past a stop deadline — left a
  stale entry that refused every later `msks start` with "VM already
  exists" until an operator passed a `stop`. The start now reconciles
  the dead VMM's residue (its tracked entry, share daemons, and tap)
  and boots.

- **Fedora guest's sshd address-wait wiring (#819).** The Fedora
  image's sshd ordering dropin now lands in `sshd.service.d` (Fedora's
  own unit names) instead of Debian's `ssh.service.d`, so
  `msks-wait-address.service` actually runs on a Fedora egress
  workspace and sshd listens once the NIC has its address. The image
  build fails when the wiring names a unit the image does not ship.

- **The NixOS guest's arch-dependent module set (#821).** The
  runtime module list and the nested-KVM unit's modprobe line now
  follow the evaluating architecture: aarch64 loads `crc32c-generic`
  and the `gpio_keys` power-button driver and probes the plain `kvm`
  module; x86_64 keeps `crc32c-intel` and the `kvm-intel`/`kvm-amd`
  pair. An aarch64 NixOS workspace now exposes `/dev/kvm` on hosts
  that pass nested virtualization, and `stop` runs through the
  guest's own shutdown. The image build pins the split per arch.

- **The console's first prompt carries the workspace's name
  (#818).** Both guests' console getty now orders after
  `cloud-init-network.service`, the cloud-init stage that sets
  the seed's hostname, so the first prompt and an interactive
  root session's `$HOSTNAME` show the workspace's name instead of
  the image default `msks-guest`. The image builds now fail when
  a systemd dropin names a unit the image does not ship — the
  same dead-ordering class #819 fixed by hand, caught here for
  every dropin the images carry.
- **`scripts/cut-release.sh` (#805).** One command pushes a
  release cut in the order the docs deploy needs: the tag alone
  first, then main once the tag's versioned docs deploy has
  finished green. Pushing the tag and main together races the
  docs workflow's concurrency group, and the release publishes
  without its versioned docs and the site's `latest` alias keeps
  the previous version until the cancelled run is re-run by hand.

## \[v0.8.3] - 2026-11-08

### Fixed

- **`msks ssh` after a host-share change (#803).** A changed mount
  set renews the workspace's seed at the next start, and the
  re-provisioning guest regenerates its ssh host keys. The
  workspace's identity row now serves a seed epoch (a counter,
  one bump per bake or renewal), and the client rotates its
  host-key pin on it, so the next session re-pins the new key
  instead of refusing it.

## \[v0.8.2] - 2026-11-07

### Breaking

- **The `client` flake output is gone (#793).** The flake's
  `client` package and devenv's `outputs.client` are removed.
  The wheel installs every console script, so `msksd` (the
  default flake output) and `outputs.server` carry the client CLI
  in the daemon closure. Install `.#msksd` (or `.#server` /
  `devenv build outputs.server`) for everything the `client` output
  and `outputs.client` used to provide. Wheel installs with pip or
  uv do not change.

### Added

- **The Debian image ships apt list state; the NixOS guest turns
  on nix-command and flakes (#783).** A fresh Debian workspace's
  first `apt-get install` resolves with no update run — the image
  stages the trixie main InRelease and Packages index under the
  names apt itself derives. The NixOS guest's nix.conf enables
  both experimental nix features, so a flake-carrying checkout
  builds before any per-user override.
- **Workspace rows read `stopping` through a stop's graceful
  window (#789).** A stop request now writes `stopping` before
  the graceful shutdown begins, so `msks ls` and a second TUI
  see the truth for a window that lasts up to the shutdown
  timeout. The row lands on `stopped` when the shutdown finishes,
  and returns to the status it carried before the stop when the
  shutdown times out. A daemon crash mid-stop heals on the next
  watcher scan, and the gates that read the status (volume
  moves, resize, clone) refuse while the row reads `stopping`.
  The TUI's stop action dims with "workspace is stopping", and
  the attach commands (`msks ssh`, `msks console`, `msks forward`,
  `msks rsync`) wait out the window instead of booting into the
  shutdown.

### Changed

- **New workspaces default to 4 vCPUs (#791).** A create that
  leaves the count unset boots 4 vCPUs, up from 2. The 1–64 bound
  is unchanged, an explicit `--cpus` still wins, and existing
  workspaces keep their stored count.

- **`shutdown_timeout_s` defaults to 90 seconds (#787).** A
  workspace stop now waits up to 90s for the guest to power off
  (was 20s). An idle guest still answers in seconds.
  `MSKSD_SHUTDOWN_TIMEOUT_S` still overrides. The dev daemon's
  supervised stop grace moves to 180s to cover the longer stop
  cycle, matching the product unit's window.

- **New workspaces default to a 20 GiB root overlay (#784).** The
  create default for `root_mib` moves from 10240 to 20480 on every
  surface (TUI create form, CLI, API) — NixOS guests' nix store
  outgrows 10 GiB after a few rebuilds. `MSKSD_ROOT_MIB` still
  overrides the default, an explicit size on create still wins
  over both, and existing workspaces keep the size they were
  created with. The create form's root placeholder names the
  daemon's real default on every healthy load (it always did for
  home), and the first-run config template's `home_mib` comment
  now states the real 20480 default (it read 2048).

- **The create form's image dropdown shows the import date and
  sorts newest-first (#781).** Each option now carries the import
  date beside the reference, and the options order by distro name
  A→Z with each distro's newest build first — the newest build of
  a release leads instead of the oldest. The designated default
  row is marked with a trailing asterisk (the blank option's own
  line names the marker), where it read "— default" before.
  Options for the same reference still resolve by hash, and the
  plain reference still names the build the daemon itself
  resolves it to.

## \[v0.8.1] - 2026-11-06

### Fixed

- **The term popup's drag selection reaches the system
  clipboard.** `msks-term-popup`'s tmux runs with mouse mode, so
  a plain drag copied into tmux's paste buffer alone: a paste in
  another window delivered whatever last wrote the system
  clipboard, not the selection just made. The launch now sets
  `set-clipboard on` and claims the clipboard feature for
  Konsole (whose terminfo carries no capability for tmux to
  read), so a drag copies to the system clipboard through the
  OSC 52 write — taken by Konsole builds since 2024, kitty,
  foot, wezterm, ghostty, and alacritty. On a terminal without
  the write, the Shift-drag native selection works as before.

## \[v0.8.0] - 2026-11-06

### Fixed

- **The listing refresh no longer cancels a swap mid-flight (#776).** The
  workspace list's standing refresh cancelled the running reload when a
  new one fired, and a cancellation landing inside the list swap left the
  listing stranded: the old list removed, the fresh one never mounted,
  and every later refresh skipped against rows recorded as painted. A
  refresh arriving mid-flight now skips instead; the next standing tick
  (five seconds) serves the rows.

- **The TUI status line shows the unix socket path decoded
  (#779).** The line printed the daemon's transport address
  verbatim, so the local socket read
  `http+unix://%2Frun%2Fus…%2Fmsksd.sock`. It now decodes the
  path — `http+unix:///run/user/1001/msks/msksd.sock` — keeping
  the quoting where it belongs, in the address the transports
  connect to. A TCP daemon's URL shows unchanged.

### Added

- **`msks clone` (#773).** One stopped workspace copies into a
  new one: both disks (the root overlay and the `/home` volume,
  reflinked where the filesystem can share blocks) and the
  create-time config — image, sizes, egress mode, mounts, login
  user, the `user_data` payload. The egress allowlist, in-effect
  verdicts, and workspace-scoped placeholders stay with the
  source. `--copy-allowlist` carries the source's allowlist; a
  clone without it starts with an empty list under the copied
  mode. In-guest-minted secrets (ssh host
  keys above all) duplicate — the cloning chapter names the
  rotation remedy. Also served as `POST
/api/v1/workspaces/{id}/clone` and the workspace page's Clone
  action. The source must be stopped; a running source answers
  the named 409.

- **The images page.** The TUI's image page is now a full page
  with its key bindings on the footer row. `d` designates the
  highlighted catalog row the default image (the same POST
  `msks image default` makes), and a published row whose build
  the catalog already holds (`[imported]`) skips the
  re-import. Both groups show each build's 12-character hash
  prefix beside the ref — the join the `[imported]` and
  `[local]` markers compare — and the catalog group sorts by
  ref with the newest import first. The rows align as columns
  that re-lay on terminal resize, with the description column
  dropping when the terminal is narrow.

## \[v0.7.0] - 2026-11-05

### Fixed

- **The mounts `applies at next start` note (#756).** The
  workspace page's mounts line and the `msks mount`/`msks umount`
  outcome showed the note whenever the workspace mounted
  anything, even after start/stop cycles with an unchanged
  config. Every workspace row now carries a `mounts_pending`
  flag: the daemon compares the configured mounts with the set
  the last start attached, and the note shows only while the two
  differ (an edit the next start takes, a mount never yet
  attached). A workspace created with mounts shows the note until
  its first start.

- **The local socket's liveness probe reads a busy listener as
  live (#742).** A second daemon starting beside a live one no
  longer unlinks the live daemon's socket file when a burst of
  local clients fills the listener's accept backlog: the probe
  now unlinks only a socket the kernel refuses outright
  (`ECONNREFUSED`), treats a backlog-full hold (`EAGAIN`, a
  timed-out connect) as "someone may be listening", and raises a
  named error when it cannot tell. The socket file is also
  created directly with its final `0660` mode, closing the brief
  window in which a permissive umask left it connectible before
  the mode change landed.

- **The daemon's sqlite engine runs WAL with a busy timeout
  (#744).** The model engine previously took sqlite's defaults
  (rollback journal, 5-second lock wait), so two concurrent
  writers — a create racing the watcher's status transition,
  consent verdict pins during bulk activity — could exhaust the
  wait and surface as a client-facing 500. Writers now queue
  behind one another for up to 30 seconds, and readers no
  longer block writers. A clean shutdown removes the WAL
  siblings; after a crash, `-wal`/`-shm` files may sit beside
  `<state_dir>/msks.db`: the next start recovers them, and the
  next clean shutdown removes them.

- **`msks-setup-macos-host --check` refuses an empty CA fetch
  (#738).** The check pass now refuses with the re-run remedy
  when the daemon's CA has not come back from the VM. The
  chain check previously verified against the system trust
  store in that case, so a port-colliding listener serving a
  publicly-trusted certificate read green for the wrong
  daemon.

- **`msks-setup-macos-host` proves the daemon's CA before any
  client file lands (#733).** The verify pass checks the TLS
  chain against the fetched CA before writing the CA, the
  token, or the client config. A torn read of the daemon's CA
  file (text that starts the PEM block but holds no
  certificate) now refuses with the previous files untouched,
  so client runs keep working between the refused setup and the
  re-run.

- **A CA file that cannot load is a named refusal (#732).** A
  `cafile` / `MSKSC_CAFILE` that points at a missing file or one
  with no parsable certificate now prints one line naming the
  daemon's CA file and where to copy it from. Previously the
  client raised a raw `ssl.SSLError` traceback at startup.
- **The console session survives a burst on a non-blocking
  stdout (#725).** The TUI's shell fallback streams the guest's
  boot console through stdout, and when the tty's fd sits in
  non-blocking mode the boot log's burst crashed the session on
  a flush that answered EAGAIN. The console's write path now
  waits for the tty to become writable and retries; the session
  lands in the shell with every byte intact.

### Security

- **Local socket group admission (#743).** The token-less local
  listener resolves peer group membership the way login does:
  every supplementary group the account database holds for the
  user counts, including the nesting a directory backend (SSSD
  with AD) expands itself. A member removed from the socket's
  group loses token-less access at the next request instead of
  at the next daemon restart.

### Changed

- **Workspaces no longer seed the Anthropic pair (#765).**
  The first-boot seed stops exporting `ANTHROPIC_BASE_URL` and
  `ANTHROPIC_AUTH_TOKEN`: claude-code reads that pair as bearer
  auth and skips its OAuth login, so a seeded default decided
  the credential model for every workspace. claude-code on the
  proxy route is a two-line manual export, documented in
  `docs/llm.md` → "The guest environment"; the subscription
  route needs no export at all. `MSKSWS_*` is unaffected, and
  existing workspaces keep working until rebuilt.

- **The TUI's shell action reports an ssh refusal instead of
  opening a console (#748).** The "Open a shell" action used to
  fall back to a `msks console` session when the ssh attempt was
  refused. It now opens nothing in the session's place. The
  tree's first flash leads with a `msks console` suggestion and
  follows with the refusal's first line; the full refusal is
  logged. The action's hint reads "ssh in this terminal".

- **TUI workspace page (#752).** The action list sits in a framed
  panel with a muted caption above each group (use, configure,
  power), the consent and mounts lines share one label-column
  grammar with `·` joins, and the header's meta line widens its
  budgets on a wide terminal so the id, image, and host read
  whole. Bold now marks the primary action alone; the highlight
  bar and the marker name the focused row.

- **`msks-setup-macos-host` keeps an existing client config
  (#730).** The tool writes `~/.config/msks/msks.yaml` only when
  the file is missing; an existing file stays byte-for-byte,
  whatever wrote it, so operator edits survive every rerun. A
  kept file whose active daemon still points at this daemon is
  named as such; any other kept file gets the alias entry to
  paste or update, and `--check`'s remedy names the same
  hand-edit path. The generated file carries the commented
  `identity_file`/`pubkey_file` pair — the keys a `msks create`
  reads — one uncomment away.

- **The forward stdio bridge and `home export -` write stdout
  through the EAGAIN-draining path (#728).** Both sinks now
  share the console's wait-for-writability write shape (#725): a
  stdout fd left in non-blocking mode under a burst waits for
  writability instead of crashing the session mid-stream.
  `rest.download` awaits a sink-provided `drain()` (the asyncio
  StreamWriter shape) after every chunk; plain file sinks are
  untouched.

### Added

- **The term-popup status row shows the egress mode and the
  popup key (#764).** `msks-term-popup`'s shell window pins
  `interactive · C-b p popup` to the right side of its tmux
  status row. The key hint lands at launch, and the hidden
  decider app fills the mode in front of it from its first
  rules frame, updating it on every change whatever surface
  decided it. A window with no workspace (a plain shell) ships
  neither half.

- **The Anthropic Messages route on the workspace LLM proxy
  (#765).** The proxy serves `POST /v1/messages` beside the
  OpenAI pair, so claude-code talks to the daemon's proxy
  directly. In passthrough mode the request forwards verbatim to
  the upstream's own Messages endpoint (the upstream must serve
  the Messages API); the count-tokens probe forwards too. In
  multi-provider mode the proxy translates between the Messages
  shape and chat completions, streamed answers included. The
  router now drops a request field the target provider rejects
  instead of failing the request (a side effect of asking routed
  streams for chunk usage). See `docs/llm.md` → "The Anthropic
  Messages route".

- **The daemon journals every published event (#762).** Every
  frame the events hub publishes also writes one INFO line to
  the daemon's journal, carrying the whole frame as JSON. On
  Linux read it with `journalctl --user -u msksd`; on a macOS
  host, `sudo journalctl -u msksd` inside the Lima VM. The
  event lines are INFO and sit in journald's own rate-limit
  pool, separate from the daemon's error lines; journald's
  retention rotates the record. Secret values and sentinels
  never appear in event frames.

- **The Linux host setups link `msks-term-popup` beside `msks`
  (#755).** `msks-setup-debian-host` and `msks-setup-fedora-host`
  now land a `/usr/local/bin/msks-term-popup` symlink beside the
  `msks` link, so a `terminal_open_cmd` window resolves the
  consent-terminal launcher on its own PATH; `--uninstall`
  removes both links.

- **The listings' columns widen with the terminal (#749).** The
  workspaces list and the secrets page now grow their columns when the
  terminal runs wider than 80 columns: the name and image columns (the
  workspaces list) and the coverage, name, and destinations columns
  (the secrets page) take the extra cells, so long names and image
  references read whole instead of clipping. At 80 columns or
  narrower, the layout stays fixed as before.

- **`msks-setup-macos-host` links `msks-term-popup` beside
  `msks` (#734).** The brew prefix's bin dir carries both client
  commands, so a consent-terminal window opened through an
  `open`-launched terminal (Ghostty on macOS) resolves
  `msks-term-popup` on the fresh login PATH the window runs
  with. `--check` reports both links; `--uninstall` removes
  both.

- **`msks-setup-macos-host --cpus/--memory/--disk` size the msksd VM
  (#721).** The flags set the VM's CPUs, memory (GiB), and disk (GiB)
  at creation and on every re-run: changed values restart the VM
  (running workspaces stop with it; each starts again with
  `msks start`), equal values change nothing, and the
  disk only grows. The floors are 2 CPUs, 4 GiB, and 20 GiB. When
  the VM's disk runs low, the storage warning and the 507 refusal
  name the grow command. See
  [Sizing the msksd VM](https://mcdonc.github.io/msks/installation/#sizing-the-msksd-vm).

- **`msks` lands on the host PATH after setup (#719).** The
  Debian and Fedora setup tools link `/usr/local/bin/msks` to
  the console script beside the tool (the real estate the
  wrapper and the VMM already occupy), and the macOS tool
  links `$(brew --prefix)/bin/msks` (user-owned, on PATH for
  every brew user, no sudo). A foreign file at the path refuses
  by name, a dangling link re-points (stale by definition),
  `--uninstall`
  removes the link, and the macOS `--check` carries the row.

- **The published image list filters foreign-arch rows
  (#717).** `GET /api/v1/health` grows the daemon host's
  machine name, and the TUI's published group carries the rows
  that machine can boot — a row declaring the other
  architecture stays off the page (the daemon's #638 import
  refusal stays the boundary), and a row that declares no arch
  shows everywhere. A daemon predating the field answers
  without the machine name and the page shows every row.

## \[v0.6.0] - 2026-11-02

### Changed

- **`devenv processes up` on a macOS host (#707).** The `msksd`
  process is registered on Linux hosts only, so on a Mac the
  command exits at once (`No processes defined`) instead of
  supervising the dev daemon's platform refusal into a restart
  loop. `msks-dev`'s refusal on a Mac points at the new
  [Developing on a macOS host](https://mcdonc.github.io/msks/development/#developing-on-a-macos-host)
  chapter, which documents both ways a Mac reaches a live daemon:
  the client over an ssh tunnel to a Linux dev host, and the
  product Lima host installed from a built wheel
  (`msks-setup-macos-host --wheel`).
- **`msks-setup-macos-host` without `--port` (#699).** The run
  now talks through the forward the existing msksd VM already
  carries — the forward is fixed at VM creation, so the VM's own
  answer is the deployment's port — and finishes the verify
  pass, the client config, and the preset on it. A VM created
  with a non-default host port needs no recreation; an explicit
  `--port` that disagrees with the existing VM's forward stays a
  named refusal naming the recreation remedy.

### Removed

- **The `mount_shares` config feature (#685).** Host paths are
  the one share surface: the `mount_shares` key and
  `MSKSD_MOUNT_SHARES` are removed, `msks create --share` /
  `msks mount HOST:GUEST[:MODE]` are the one grammar, and the
  TUI's mounts editor takes host paths only. A config that still
  names the key or the env var fails at load with a message
  pointing at `mount_host_roots` and `--share`; workspaces whose
  stored rows carry named mounts drop them at read and at the
  next set (they mount nothing for them at the next start) —
  recreate those mounts with host paths.

### Added

- **Version skew between the client and the daemon is named
  (#706).** Every `msks` invocation but the `image` group (whose
  `check` member runs offline) makes one best-effort
  `/health` roundtrip and prints a notice naming both versions
  and the remedy the skew's direction asks for (#715): a newer
  release daemon names the client's update, a development build
  that leads names the tree it came from — never a rerun that
  would downgrade it. The setup tool's check
  pass grows a row pairing the VM's daemon version with the
  tool's own, and the bootstrap refuses — with the version pair
  named, ahead of the unit restart — when the daemon it installs
  is not the tool's build or rejects the config the same run
  writes, instead of crash-looping behind the 90s verify timeout
  (#704's shape).

- **The macOS share stack documented (#675).** The [mounts
  chapter](mounts.md#shares-on-a-macos-host) gains the macOS
  share geometry — a Mac folder crosses vz virtiofs into the
  daemon's VM under `/mnt/msks`, then virtiofsd into the
  workspace — with the four contracts that cross with the tree:
  watchers on shared trees run in polling mode (file events do
  not cross virtiofs), case and Unicode normalization follow the
  Mac volume, an `rw` share names the workspace its single
  writer (Mac-side writers honor none of its locks), and a
  volume shared into a running workspace ejects only after the
  workspace stops.

- **Lima self-install in `msks-setup-macos-host` (#699).** A Mac
  without Lima now gets it through `brew install lima` run by the
  tool itself, so a stock Mac reaches a working msksd in one
  run; a Mac without brew names the Homebrew line and stops.

- **`msks-setup-macos-host` writes the client config and a
  login agent (#689).** The tool now writes `msks.yaml` itself —
  the daemon alias pointing a bare `msks` at the forwarded port
  with the token file and CA beside it — and a launchd agent
  runs `limactl start msksd` immediately on install and at every
  login, so the daemon answers now and after a reboot with no
  exports in the shell profile. An operator's own
  `msks.yaml` is kept untouched (the printed exports remain the
  copy-paste fallback); `--check` reports both the config file
  and the agent, and `--uninstall` removes the agent along with
  the VM.

- **`msks-preflight` prints per-section durations (#690).**
  Every gate section carries a wall-clock column and a total in
  the summary, a `--collect-only` pass names an unimportable
  module in seconds before the suite runs, and the suite run
  itself carries a 40-minute wall-clock budget that fails loudly
  — a wedged run can no longer hang a pre-flight session
  indefinitely.

- **`msks-setup-macos-host` (#676).** The macOS host setup: a
  stock Apple-Silicon Mac reaches a working msksd in one run —
  the tool creates the msksd Lima VM from the shipped template
  (the invoking user's home mounted under the reserved prefix,
  the daemon's port forwarded on both loopbacks), installs the
  daemon inside it (the PyPI release matching the tool's
  version, or `--wheel` for a build in hand), writes its config
  with host-share mode `prefix` and the user's home the default
  allowed root, the daemon's tool set (egress plumbing, volume
  trio, seed tooling) with its kernel routing setting and the
  pinned cloud-hypervisor release beside it, enables its
  service, and prints the client preset (`MSKSC_URL`/`MSKSC_TOKEN`/`MSKSC_CAFILE` — the CA
  written under `~/.config/msks/`). `--check` runs the doctor
  rows (Lima at the template's floor, the VM running, the
  nested-virtualization marker, virtiofsd inside, the listener
  answering); `--uninstall` deletes the VM. Every phase is
  idempotent; reruns preserve the token, the CA, and the
  daemon's state.

- **`msks image check` grows the `virtiofs` row (#674).** The
  guest-image capability check now probes the virtiofs
  filesystem driver a share mounts through: Debian's pinned
  kernels build the driver in; Fedora's image ships it as a
  module in the runtime closure — current images pass, and an
  image without it fails its own row with the named gap. A
  workspace that attaches shares on such an image reports the
  same named gap on the guest's console and cloud-init log at
  start (the per-boot mount installer checks the driver before
  its units come up).

- **`msks create --share HOST:GUEST[:MODE]` and the host form on
  `msks mount`/`umount` (#673).** The client surface for #667's
  host-path shares: a repeatable create flag whose `~` expands
  against your home before the request, the same spec grammar on
  `msks mount`, and `msks umount` names what leaves by host
  path. A refused
  share surfaces
  the daemon's gate message verbatim — which gate refused and
  the remedy. See the [mounts chapter](mounts.md#what-a-mount-is).

- **Host-path workspace shares behind the #667 gates (#672).**
  A workspace's mounts now take a host-path form —
  `mounts: [{host: ~/code, guest: /mnt/code, mode: rw}]` —
  alongside the named-share form: the daemon translates the path
  by its host mode (`MSKSD_MOUNT_HOST_MODE`, `identity` on a
  bare-Linux host or `prefix` on the Lima/macOS host), gates it
  against the roots its VM actually mounts and the allowed roots
  in `MSKSD_MOUNT_HOST_ROOTS` (both refusing at set time with
  their remedy: VM recreation, or the config edit), and refuses
  symlinks that resolve outside what the operator allowed. The
  host form defaults to `rw`, the same default the named form
  carries — the trust decision (the share listed or the root
  allowed) precedes the mode. The virtiofsd
  lifecycle, the cloud-hypervisor `--fs` wiring, and the guest
  mount units are the #620 machinery, keyed by a tag derived
  from the path. See the [mounts
  chapter](mounts.md#the-mount-gates) for the two gates and their
  refusal remedies.

- **Lima template share roots under the reserved prefix
  (#671).** The msksd-VM Lima template ships in the wheel
  (`msks/server/assets/msksd-vm.yaml`) with every share root
  mounted under `/mnt/msks` — the WSL `/mnt/c` shape, so an
  operator's `/Users/name` appears at
  `/mnt/msks/Users/name` — and the default root (the invoking
  user's home, writable) declared at VM creation. The
  host-share vocabulary in `msks.spec.mounts` carries the
  prefix, the host-mode translation (`identity` on bare Linux,
  `prefix` on VM hosts), and the template validation that
  refuses a mount outside the reserved-prefix shape.

### Fixed

- **A development-build tool no longer installs a release into
  the VM (#704).** The tree's version now carries a `.devN`
  marker between releases, and a `msks-setup-macos-host` run
  without `--wheel` refuses by name on the marker with the
  build-a-wheel remedy. Previously a tree whose version number
  matched a release silently installed that release's daemon,
  which rejected the config the same run wrote.

- **A failed staging-agent bind reports the real error (#710).**
  The ssh/rsync staging agent's cleanup closed a server a failed
  bind never built, so a bind refusal (a socket path past the
  AF_UNIX limit, an unwritable temp dir) printed an
  `UnboundLocalError` about the cleanup instead of the error
  itself. The cleanup now runs only for a server that was built,
  and the original refusal reaches the operator.

- **`msks-setup-macos-host --uninstall` names a missing limactl
  (#701).** On a Mac where `limactl` is not on PATH, the
  uninstall arm died with a raw `FileNotFoundError` traceback;
  it now prints the install-Lima remedy on stderr and exits 1,
  the same named-refusal shape the setup and `--check` paths
  answer with.

- **ssh re-pins host keys when a workspace's disks are recreated
  (#697).** The daemon records a disk epoch on each workspace and
  advances it whenever it recreates the root overlay under a live
  row (a healed loss, a factory reset) — the guest regenerates its
  ssh host keys with the fresh overlay. The identity answer
  carries the epoch, the client rotates its per-workspace
  host-key cache on a change and prints a notice naming the
  recreation, and every invocation — `msks ssh <name>` and the
  TUI's shell action alike — pins under the workspace's id, so a
  regenerated key no longer refuses from one entry point while
  the other silently re-pins.

- **`msks-preflight` goes green on a Mac dev host (#691).** The
  remaining darwin failures in the gated suite are closed: the
  Linux-only surfaces skip with their platform reasons named, the
  host-independent paths they hid run on every host, and the
  suite's coverage bar adapts off Linux where those surfaces
  cannot execute. A Mac pre-flight runs the full gate stack green
  in about a minute; Linux CI is unchanged.

- **The gated suite terminates on a Mac dev host (#690).** The
  microvm driver's VMM and virtiofsd identity checks read the
  process command line through procfs only, so on a Mac every
  kill, shutdown, and launch sweep read its own spawned daemons
  as foreign — the signal never fired, the test waited forever,
  and a pre-flight run wedged past half an hour. The read now
  falls back to `ps(1)` on darwin (Linux keeps the procfs path),
  the stale-socket probe sweeps a non-socket path that darwin's
  `connect()` answers `ENOTSOCK` where Linux answers
  `ECONNREFUSED`, and the Linux-only pieces (the NFQUEUE
  binding's import, a SIGSTOP escalation path the darwin event
  loop cannot schedule) skip with their reasons named. The
  suite drops from a hang to about a minute on an
  Apple-Silicon dev host.

- **Guest images retry the home-volume mount on every boot
  (#688).** A restarted workspace that lost the udev race — the
  volume's by-label symlink landing after the mount unit's 30s
  device timeout — came up with `/home` unmounted indefinitely
  (the first-boot retry that comes with the identity seed never runs
  on a later boot). Every guest image now ships a udev rule that
  asks systemd for the mount when the labeled disk appears, so
  `/home` mounts moments after the label lands, on every boot a
  rebuilt image serves; existing images keep the seed's retry
  for first boots.

- **The seed retries a dependency-failed home-volume mount
  (#686).** On a boot slow enough that udev's by-label symlink
  lands after the fstab unit's 30s device timeout, `home.mount`
  fails as a dependency and systemd never retries it — the
  volume disk is present, and first boot paid the seed's whole
  20-minute wait (observed on the macOS nested stack). The
  seed's wait now asks systemd for the start itself when the
  volume disk is present: paced at 30s, best-effort, and only
  the start verb. The retry mount took under a second on every
  reproduction.

- **doctor's apple-kvm row gates on the NV-mode marker (#664).**
  The warning row now requires an NV-mode marker — the
  `kvm-arm.mode=nested` command line, sysfs's kvm mode
  parameter, or the kernel log's NV init line — beside the
  Apple MIDR, so a Linux VM under macOS's
  Virtualization.framework, whose silicon reads as Apple but
  whose nested KVM boots guests to ssh, runs the report clean;
  Apple-silicon hosts running KVM in NV mode keep the warning.
  See [docs/doctor.md](doctor.md).
- **First boot seeds the login user onto the `/home` volume
  instead of under it (#662).** The first-boot seeding script
  waits for the persistent `/home` volume's mount before writing
  anything under `/home`: on a host whose first boot is slow
  enough that the volume's first fsck+mount finishes after the
  seeding, the seeded homes and their `authorized_keys` used to
  land on the root filesystem and be buried when the volume
  mounted — the login user held ssh for the first minutes of
  first boot and lost it the moment the mount completed. A boot
  with no volume attached stands the wait down after a minute
  and seeds under the root filesystem's `/home`; a boot whose
  volume is attached but never completes its mount holds the
  wait's full 20 minutes first, then seeds the same way.

- **The devenv shell opens on macOS hosts (#665).** The dev shell
  evaluates and opens on aarch64-darwin: the Linux-only VMM
  toolchain (cloud-hypervisor and the netfilter family) joins the
  profile only on Linux, while the `msks` client, the Python/uv
  toolchain, and the lint gates are available on every host. A Mac
  checkout can now drive a remote daemon with `MSKSC_URL` straight
  from `devenv shell`. Linux shells carry the same package set as
  before, unchanged.

- **arm64 workspaces now power off on `msks stop` (#649).** The
  arm64 guest images' runtime module tree ships `gpio_keys`, the
  input driver for cloud-hypervisor's device-tree power button, so
  the graceful-shutdown press reaches logind's power-key handling
  and the guest powers off within the deadline. The x86 ACPI
  button path is unchanged, and `msks image check`'s
  `acpi-shutdown` row now passes on arm64 hosts.
- **Release image channel (#659).** A version release now
  carries the previous release's `images.json` from the moment
  it is created, so image imports keep working — pulling the
  previous images — while the release's guest images build (up
  to hours); before, the manifest fetch failed during that
  window. The manifest is replaced when the new images finish
  publishing.

## \[v0.5] - 2026-10-28

### Changed

- **virtiofsd 1.13.0 floor for host mounts (#655).** Host mounts
  need virtiofsd 1.13.0 or newer: the uid/gid translation and
  read-only enforcement the shares run on landed upstream there,
  and older builds refuse the daemon's arguments at start.
  `msksd doctor` now reads the version (a below-floor binary
  warns naming the gap and the `MSKSD_VIRTIOFSD` override —
  Linux Mint 22 ships 1.10.0), and a mount start on a too-old
  binary fails with the same named requirement instead of an
  "exited at start" log tail. See
  [docs/mounts.md](mounts.md).

### Fixed

- **Host share mounts: git loose objects no longer fail with
  EACCES (#657).** Creating a file without owner-write bits and
  writing through the same open file descriptor (git's
  loose-object path) failed on every virtiofs share. The share's
  xattr remapping now denies `security.capability` at the map
  and leaves the name unmapped, keeping file-capability clearing
  with the host kernel — and, whatever the daemon's privilege, a
  guest can neither set nor read the attribute through a share.
  Applies at each workspace start.

## \[v0.4] - 2026-10-27

### Added

- **Fedora 44 guest image (#651).** A third catalog image family
  builds beside Debian and NixOS: `msks-build-guest fedora` (and
  the `-x86_64`/`-aarch64` flavors) produces the `fedora:44`
  archive from Fedora's pinned Generic cloud image, with the same
  agent toolchain, console, mounts, and egress contract the
  Debian image ships. The guest boots with SELinux disabled — the
  image build reconstructs the filesystem without SELinux labels,
  and the workspace's isolation boundary is the VM itself. See
  [docs/images.md](images.md).
- **Apple Silicon host warning in doctor (#649).** `msksd doctor`
  names the Asahi-class kernel limitation where it exists — cpu0's
  MIDR names Apple — as a warning row: this kernel's KVM does not
  yet deliver guest interrupts, so workspaces start and their
  kernels boot, then stall before ssh answers; everything short
  of a guest boot works. The installation chapter documents the
  same host class, and the row stays off x86_64 and non-Apple
  arm64 hosts.
- **Per-arch channel images and arch-checked imports (#638).**
  The release channel publishes one row per flavor and arch:
  `images.json` carries an `arch` field beside the kernel and
  size, built by per-arch legs and merged into one manifest.
  `msks image import` compares the image's declared `arch`
  against the host's own machine and refuses a mismatch naming
  both sides — a foreign-arch kernel would hang wordlessly at
  boot. An image without the field imports as before, and
  `msks image check` and `image info` print the declared arch.
- **arm64 guest images (#637).** The guest pipeline builds for
  x86_64 and aarch64: `msks-build-guest` follows the building
  host (the `-x86_64`/`-aarch64` flavors pin it), the arm64
  guests run 16K-page kernels (Debian's own `arm64-16k` flavour;
  a kernel patch on the NixOS side) so nested KVM initializes on
  16K hosts like the Asahi class, and `image.json` declares its
  `arch` — images without the field import as before. See
  [docs/images.md](images.md).
- **`msks-setup-fedora-host` (#635).** The Fedora deployment-host
  setup — `msks-setup-debian-host`'s phases over dnf: the capability
  wrapper, the routing kernel settings, the dependency packages,
  the kvm grant, the systemd user service, and `--uninstall`'s
  mirror. `sudo "$(command -v msks-setup-fedora-host)"` prepares
  a stock Fedora host, the Asahi remix included, end to end. See
  [docs/installation.md](installation.md).
- **The dev daemon finds its wrapper and uplink (#635).**
  `msks-dev` resolves the setup tool's
  `/usr/local/bin/msks-caps` and the host's default-route
  interface by itself, so a bare `devenv processes up` starts on
  a host NixOS does not manage — `DEV_CAPS` and `DEV_UPLINK`
  remain the overrides.
- **Host share mounts (#620).** A workspace can mount host
  directory trees into its guest: the daemon's config names the
  shares (`mount_shares`, reloadable on SIGHUP), a workspace
  mounts them by share, guest path, and mode (`msks create
--mount`, `msks mount`/`umount`, or the TUI's mounts editor),
  and changes apply at the workspace's next start. Files present
  in the guest as the workspace's own user. See
  [docs/mounts.md](mounts.md).
- **Guest timezone from the host (#633).** A create captures the
  host machine's timezone and the guest's clock follows it — the
  seed's first-boot script sets it, with no operator action. The
  capture is create-time and immutable: a host timezone change
  later affects only new creates, and a host on UTC leaves the
  guest on the image's own default. See
  [docs/images.md](images.md) (first-boot provisioning).

## \[v0.3] - 2026-10-24

- The guest images' pi coding agent moves to 1.4.0, and a default
  `~/.pi/agent/settings.json` now ships for every user: thinking
  blocks stay out of the transcript and pi's install telemetry
  beacon stays off (#616). The project adopts the MIT license
  (#612), and the docs split the egress consent chapter out of
  guest networking.

## \[v0.2] - 2026-10-23

- Guest image updates, docs changes.

## \[v0.1] - 2026-10-18

### Added

- **The NixOS workspace guest image (#250).** A second catalog image
  built from the devenv-pinned nixpkgs with
  `msks-build-guest nixos` (into `.devenv/state/guest-nixos`;
  `GUEST_NIXOS_DIR` relocates it), registering under the
  `nixos` name beside `debian`. The archive carries the same
  containerDisk contract and the same declared capabilities —
  cloud-init, the prelude-v1 console — so the daemon serves it
  with no new machinery and nothing keyed off the image's name.
  See [docs/images.md](images.md#the-shipped-builder).
- **`msks egress tui` (#195).** The consent decider TUI: a textual app
  that registers as the workspace's decider, shows held requests with
  countdowns (`a`/`d` verdicts, `A`/`D` duration picker), and adds a
  rules screen with revocation — over the frames and endpoints #69
  serves, reconnecting with backoff (reset after each healthy
  connection). Countdowns follow the deadline the daemon carries on
  each request frame, and a registration the daemon rejects (an
  unknown workspace) exits with the reason instead of waiting. The
  `watch`/`decide`/`revoke` subcommands remain the scripting
  surface. See [docs/cli.md](cli.md#msks-egress).
- **Placeholder secret store, `msks secret` commands (#198).**
  Mint a sentinel for a workspace (`msks secret mint <ws> --name
<label> --dest <hosts> --secret-file <path>|-`) and the real
  secret lives in a SecretSpec-backed store, provider as a setting
  (`secret_store_provider`: `file` default, `age` for at-rest
  encryption, `awssm`, `bws`), never in the workspace; `ls`,
  `renew --ttl`, `revoke`, and `check` round out the surface.
  Secrets are passed through stdin or a file, never argv; each lifecycle event
  (mint, revoke, expiry) lands in the audit table. See
  [docs/secrets.md](secrets.md), the [key reference](config.md),
  and [cli.md](cli.md#msks-secret).
- **`msks rsync` (#190).** Zero-setup file copies over the forward:
  `msks rsync <ws> -- -av ./site/ root@:/root/site/` boots the workspace
  when the daemon reports it as not running, stages the ssh
  identity in memory the way `msks ssh` does, and runs the host
  rsync through a forward the copy opens itself — push and pull,
  with the identity in memory only and no key file written. A
  path with an empty host (`:src/`, `root@:/root/site/`) targets
  the workspace the command names, its login user taken from the
  paths' own `user@` spelling; a `--pubkey` workspace exits
  naming the recovery for its private half. See
  [docs/cli.md](cli.md#msks-rsync).
- **Egress consent (#69).** Workspaces pick a consent posture at
  create (`--egress-mode allow|static|interactive` plus repeatable
  `--allow` specs; `allow` is the default and preserves the current
  unrestricted egress). `static` resolves and reaches only the
  allowlist (off-list names answer NXDOMAIN and are recorded);
  `interactive` holds each new connection's first packet in the
  deployment host's kernel until a decider answers over the events channel —
  `msks egress watch/decide/revoke/rules/requests` drive the whole
  verdict lifecycle (`once`/`5m`/`15m`/`tilrestart`/`forever`), with
  revocation also dropping a destination's live connections. Ports
  53/853 drop toward anything but the daemon's resolver (the naming
  layer), verdicts pin names not just addresses, and the queue
  fails closed (msksd down or a full queue drops). New settings
  `MSKSD_EGRESS_MODE`, `MSKSD_EGRESS_CONSENT_TIMEOUT_S`,
  `MSKSD_EGRESS_CONSENT_RATE_LIMIT`,
  `MSKSD_EGRESS_CONSENT_RETENTION_DAYS`,
  `MSKSD_EGRESS_CONSENT_ROW_CAP`, `MSKSD_EGRESS_QUEUE_BASE`,
  `MSKSD_CONNTRACK_TOOL`, and `MSKSD_AUDIT_HMAC_KEY` (consent rows
  carry HMAC tags when it is set). See
  [docs/egress-consent.md](egress-consent.md) and
  [docs/cli.md](cli.md#msks-egress).

- **`msks storage` image import times (#186).** Each catalog row
  in the image cost table shows when the archive came in (local
  time to the minute) and the `GET /api/v1/storage` image entries
  carry the time beside the hash, so entries sharing a reference
  (a rebuilt image imported again) read as distinct; the table
  orders them oldest-first. Re-importing the same archive
  refreshes the time. See
  [docs/storage.md](storage.md#capacity-reporting).
- **`msks resize` and `POST /api/v1/workspaces/{id}/resize` (#184).**
  A stopped workspace's disk sizes move after create: the `/home`
  volume grows or shrinks (`e2fsck` quiets the journal, `resize2fs`
  moves the filesystem, a shrink below used blocks answers a named
  `409`), and the root overlay grows with the next start's
  cloud-init filling the larger device. The row, `msks ls --json`,
  and `msks storage` report the new ceiling immediately, and a
  completed resize announces `workspace.resized`. New settings
  `MSKSD_RESIZE2FS` and `MSKSD_E2FSCK` name the tools.
- **`msks storage` and `GET /api/v1/storage` (#184).** The daemon
  reports the state-disk budget (`used`/`free` with a named
  `pressure` condition), each workspace's cost beside its size
  ceiling, and each catalog image's cost. The watcher publishes a
  `storage.pressure` event on every change with a named log line,
  and workspace creates, image imports, and home-volume imports
  answer `507` below `MSKSD_STORAGE_FLOOR_MIB` (default 512 MiB;
  the warn line sits at `MSKSD_STORAGE_WARN_PCT`, default 90),
  with imports sized when their bytes are knowable (the archive
  counted twice; a `Content-Length` body counted once), instead of
  filling the disk until guests see I/O errors.
- **`msks ssh -- -A` forwards the operator's agent (#174).** A
  command-line `-A` (or `-o ForwardAgent=yes`) now names the agent
  behind `SSH_AUTH_SOCK` explicitly, so `ssh-add -l` and
  `git push` inside the workspace use the operator's keys — no
  `Host msks-*` ssh-config block needed. The workspace identity
  stays the session's authentication credential; the guest
  receives the operator's keys and nothing else. An explicit
  `-o ForwardAgent=<path>` keeps its own socket (it wins over
  every flag and value, as with stock ssh), and a line stock ssh
  resolves to off — including `-A -a` — passes through untouched.
  The explicit form needs an OpenSSH client of 8.2 or newer (the
  previous behavior forwards the session agent on older clients).

- **Console challenge-response (#123, first half).** The guest
  console demands a signature the daemon cannot produce: workspaces
  seeded with an identity now also plant an allowed_signers trust
  store, and each console connection carries a fresh nonce the
  client must sign (SSHSIG, `msks-console` namespace) before any
  shell — verified by the guest's own ssh-keygen, refused closed
  otherwise. `msks console` answers transparently with the same key
  as ssh (escrow, client data root, or the operator's ssh-agent,
  hardware keys included — msks never reads an agent-held half).
  Pre-change guests serve no challenge. Enrollment, minted-key
  rotation, and the audited re-enable follow in the second half.
- **Operator-supplied ssh key at create (#132).** `msks create
--pubkey FILE` (or `-` on stdin) builds the workspace around a
  public key the operator already owns, at any well-formed key
  type: the daemon accepts the supplied line as-is (shape-checked,
  re-annotated, seeded like any identity) and holds no private
  half. Minted keys keep the FIPS-approvable type set; login uses
  the operator's own key, and `msks ssh`'s recovery text names it.
  See `docs/networking.md`.
- **Client-minted workspace identity as the create default (#121).**
  `msks create` mints the workspace's ssh keypair on the client and
  sends the public half only: the daemon stores and seeds that half
  exactly like its own minted one and holds no private half — `msks
key` answers the public line, and `msks ssh` serves the private
  half from the client data root
  (`~/.local/share/msks/<id>/identity`, mode 0600). `--daemon-mint`
  keeps the daemon-minted escrow mode (#111); `--key-type` selects
  the type
  (`ed25519` default, #138). See `docs/networking.md`.
- **git-out through egress with a forwarded agent (#81).** The
  dogfood loop's outbound half is proven end to end by a new opt-in
  root smoke, `test_local_egress_git_out` (it runs in the KVM
  workflow's egress step): the workspace installs git from Debian's
  mirrors and fetches an unrelated HTTPS host through the NAT'd
  uplink, then pushes a commit to a scratch git server on the host
  through a test-widened input pin, authenticating only with an
  operator key that came through the forward as a forwarded agent — no
  credential in the image or the seed. The workflow and its
  ControlMaster caveat are in `docs/networking.md`.
- **`msks ssh` (#112).** One command logs into a workspace with stock
  ssh over the forward websocket: it boots the workspace if needed,
  fetches the minted identity over the authenticated API, and serves
  the private half from a transient in-process ssh-agent — ssh names
  the identity by its public half and signs through the socket, so
  the key never becomes a file on the client. Logs in as the `msks`
  workspace user by default (`-l root` for recovery); per-workspace
  `known_hosts` under `accept-new`. See `docs/cli.md` and the alias
  workflow in `docs/networking.md`.
- **Home-volume export/import (#80).** A workspace's `/home` volume
  now moves through the daemon's authenticated listener as byte
  streams: `GET`/`PUT /api/v1/workspaces/{id}/home` stream the
  volume file out and replace it from an uploaded ext4 image, with
  `msks home export` / `msks home import` on top (`-` speaks stdio,
  so the bytes compose with gzip or ssh). The workspace must be
  stopped (`unknown` refuses too — the VM may be live), moves
  serialize against boots and deletes per workspace (waiters
  answer a named 409 after `MSKSD_MOVE_WAIT_TIMEOUT_S`, and the
  daemon re-checks the live VMM under the lock), an upload that
  fails the ext4 check or dies mid-body leaves the existing volume
  in place, all-zero windows come back sparse, and each completed
  move publishes a `home.exported` / `home.imported` event. See
  [storage](/storage/#home-volume-export-and-import) and
  [the CLI](/cli/#msks-home).
- **Minted workspace identity and `msks key` (#111).** msksd mints a
  per-workspace ssh keypair at create — Ed25519 by default (#115,
  #138), with the type configurable via
  `MSKSD_SSH_KEY_TYPE` — and stores both halves with the workspace's
  state. The public half is planted into `authorized_keys` for root
  and the `msks` workspace user through the first-boot seed (composed
  with any `user_data` payload), so a fresh workspace accepts ssh
  with no manual key steps. `msks key <ws>` fetches the identity over
  the token-gated API; `--private` prints the private half, `--out`
  writes it mode 0600. The halves persist across daemon restarts and
  workspace stop/start.
- **Guest sshd and rsync (#110).** The workspace image serves TCP on
  its forward path: sshd (Debian's own package, enabled) listens on
  all interfaces behind a boot-time unit that waits for the NIC's
  address (15s ceiling), with key-only login (`PasswordAuthentication
no`, `PermitRootLogin prohibit-password`) pinned by a config dropin,
  and rsync ships in the image. Host keys are generated on first boot
  into the persistent root overlay, so they survive stop/start — the
  recorded `known_hosts` entry keeps matching. The egress input
  rules admit the conntrack-established replies to the
  daemon-originated forward connection; guest-initiated connections
  arrive state NEW and still drop. See
  [networking](/networking/#reaching-guest-services-the-forward).
- **`msks forward` and the forward websocket (#109).** A workspace's
  guest TCP ports reach the client through a new authenticated
  endpoint, `WS /api/v1/workspaces/{id}/forward/{port}`: the daemon
  connects to the workspace's deterministic tap address (retrying through
  the DHCP/service bring-up race) and pumps raw bytes both ways —
  one websocket per guest TCP connection, no framing, with
  `forward.opened`/`forward.closed` events on the events channel.
  The client command bridges stdio (the ssh ProxyCommand shape) or,
  with `--local PORT`, a loopback listener where every accepted
  connection gets its own forward. The token authenticates through
  the `Authorization` header — the REST surface's Bearer form, kept
  out of URLs and their logs; a workspace without egress is refused
  with the reason naming the missing NIC. See `docs/cli.md`.

- **The dev-workspace bootstrap seed (#77).**
  `scripts/dev-workspace.sh`, passed to `msks create --user-data`,
  bootstraps an msks development environment inside a workspace over
  its own egress NIC at first boot: uv (with its own Python 3.14),
  the repo checkout, and `uv sync` — the venv the `unit-tests`
  invocation runs from — all on the persistent root overlay; stop/
  start keeps it, a factory reset re-provisions, re-running is a
  no-op. Proved end to end by the `TEST_EGRESS=1` root
  smokes.

- **`msks shell --user` and the console identity prelude (#63).** The
  vsock console now serves shells as root or the image's workspace
  user: a guest-side helper negotiates the identity, the client
  terminal's size, and its TERM value in-band on each connection, and
  the listener accepts host-originated connections only (guest-local
  peers are refused). A workspace bound to an image whose record
  cannot be read is refused with close code 4501 — the console never
  silently downgrades to a raw root shell. Images declare
  `console_protocol: prelude-v1` and
  `console_users` in their manifest; older images keep today's raw
  root shell through the daemon's legacy path. The helper is a Rust
  crate under `src/console-helper`, gated at 100% line and branch
  coverage (`.github/workflows/rust.yml`).
- **YAML config file for msksd (#46).** `msksd` reads `msksd.yaml` — `--config <path>` for an explicit file, `--config=none` for env-vars-only, and a bare `msksd` resolves `$MSKSD_CONFIG_DIR/msksd.yaml` (default `~/.config/msksd/msksd.yaml`), generating a commented template on first run. Precedence is `MSKSD_*` env vars > file > defaults; a config key is its variable minus the prefix, lowercased (`MSKSD_PORT` → `port`, `MSKSD_EGRESS_SUBNET` → `egress_subnet` — flat, klangkd's convention, derived by one rule so the spellings cannot drift), keys take native YAML scalars, and unknown keys, duplicate keys, or invalid values fail at startup. SIGHUP re-reads the file into the live settings without a restart. The deployment-host module (#229) generates the file for the service with the build's store paths, and a bare `alembic` run resolves the database URL from the default file when present. See `docs/config.md` for the key-by-key reference.
- **`msks-preflight` (#43).** One command delivers the Python-side pre-commit gates' complete feedback before the first commit attempt: every ruff and deferred-import finding, every xenon offender, the jscpd report, and — when sources under `src/msks/` changed — the gated suite run followed by every missing coverage line and branch arc for the changed files (`scripts/covgaps.py`). AGENTS.md makes it part of the loop: fix everything it names in one editing pass, re-run, then commit. The xenon and jscpd gates now also grade untracked-but-present files, so a new file grades from the moment it exists.

- **Doubled-escape and chunked input in `msks shell` (#36).** The client reads stdin in 4,096-byte chunks and sends one websocket frame per chunk, so a large paste lands a few frames instead of one TLS frame per byte. Ctrl-] Ctrl-] — the second press within 50 ms, or both bytes in one chunk — sends one literal Ctrl-] (0x1d) to the guest; a single Ctrl-] still detaches, and bytes typed before the escape are delivered before the session closes. See `docs/cli.md`.
- **Per-workspace `user_data` provisioning (#41).** A workspace created with `user_data` (API field; `msks create --user-data FILE`, `-` for stdin, ≤64 Ki characters) runs the payload on its first boot through cloud-init: the daemon builds a `cidata`-labeled iso9660 seed disk (NoCloud's own format, `MSKSD_MKISOFS`), attaches it read-only, stores it mode 0600, and deletes it with the workspace; both `#!` scripts and cloud-config documents run. The payload is create-time and immutable (`PUT`/`PATCH` answer a named 405). See `docs/images.md`.

- **Pre-commit hook suite (#72).** Eleven hooks join the commit-time suite: the deferred-imports checker (`scripts/check_deferred_imports.py`, ported from klangk — imports live at module scope, with `# allow-deferred-import` as the escape hatch), shfmt, shellcheck, check-executables-have-shebangs, markdownlint, actionlint, trufflehog, nixfmt, check-toml, yamllint, and prettier. `devenv shell` writes a generated `.prettierignore`; the lint rules live in `devenv.nix`; CI's lint workflow runs the whole suite (`pre-commit run --all-files`). trufflehog scans staged file contents and fails only on credentials that verify live (the stock git-history variant scans no commits at commit time). The tree was formatted once to the new gates (docs, workflows, Nix, shell scripts).
- **`msks image` catalog commands (#65).** The CLI now manages the image catalog without curl: `msks image ls` prints one line per image (reference, short hash, default designation, kernel facts; `--json` for the raw listing), `msks image import <path>` registers a daemon-side archive and prints the imported reference (the help states the daemon reads the path — nothing is uploaded), `msks image rm <ref>` removes by any catalog reference form (`name:version`, bare name, `name@hash`, full hash, or a unique hash prefix) and surfaces the 409 naming a workspace that boots the image, and `msks image info <ref>` prints one image's full record. See `docs/cli.md`.

- **`msks-jscpd` token-clone gate (#71).** The jscpd scanner (5.0.16, a pinned per-platform prebuilt binary fetched from npm — linux x64/arm64 gnu and macOS arm64/x64; unsupported platforms fail at eval time — ported from klangk #2904) joins the dev environment as both a report (`msks-jscpd`) and a pre-commit gate: `scripts/jscpd-gate.sh` is the single definition of the invocation (tracked sources in `src/msks/msks`, `--min-tokens 70`), shared with the `msks-xenon` pattern. A commit fails when any exact clone of ≥ 70 tokens exists among those sources; the backend baselines clean (0 clones), so the gate starts from zero.

- **Workspace egress networking, on by default (#52).** Workspaces are networked from creation: each boots with a virtio-net NIC onto a per-VM tap on the deployment host, takes its address, gateway, and the daemon's resolver over DHCP from a per-workspace /30 of `MSKSD_EGRESS_SUBNET` (the slice recorded on the row, so the address is stable across restarts), resolves through the daemon's UDP forwarder, and NATs out the host uplink through a per-VM nftables table (forward: the guest's source out the uplink and established replies back, all else drops; input: DHCP and the resolver only, so guests cannot reach the daemon's own services) — create with `"egress": false` (`msks create ws --no-egress`) to boot NIC-less. The plumbing arms while `MSKSD_EGRESS_ENABLED=true` and the daemon holds `CAP_NET_ADMIN`; the host's forwarding comes from the msks module (#229). The guest overlay ships the DHCP client (systemd-networkd unit + resolved stub). Consent gating also arrives with #69. See `docs/networking.md`.

- **Client CLI: `msks stop`, `msks rm`, and the workspace listing named `msks ls` (#66).** `msks stop <id>` powers a running workspace off — a graceful, deadline-bounded guest shutdown — and prints `<id> stopped`; `msks rm <id>…` deletes one or more workspaces (stopped or running) together with their persistent root overlay and `/home` volume, printing one `<id> deleted` line per id. The workspace listing command is `msks ls` (the `#59` behavior under its final name, matching `msks image ls`), completing the lifecycle set: ls / create / start / stop / rm / shell. Missing workspaces (404), workspaces recorded on another host (409), and missed shutdown deadlines (503) print one readable line and exit non-zero. See `docs/cli.md`.

- **Client CLI: `msks ls`, `msks create`, and `msks start` (#59).** `msks ls` prints one line per workspace (id, status, image hash, host; `--json` for a machine-readable document), and `msks create` POSTs the API's create body — `--image` for a catalog reference, `--cpus`/`--mem-mib`/`--root-mib`/`--home-mib` for sizing, explicit `--kernel`/`--rootfs` to bypass the catalog — printing the new workspace's id; `--start` also boots it, and `msks start <id>` boots an existing workspace later. `msks shell` now boots a not-running workspace itself before attaching (notices on stderr). All commands use the `MSKSC_URL`/`MSKSC_TOKEN`/`MSKSC_CAFILE` conventions from `msks shell`; connection failures, timeouts, and API or validation errors print one readable line, not a traceback. See `docs/cli.md` for the command and environment reference.

- **Per-workspace persistent state (#14).** Every workspace owns two persistent artifacts created with it: a root overlay (qcow2, copy-on-write over its image — package installs and root edits persist across `stop`/`start`) and a `/home` volume (ext4, labeled `msks-home`, mounted at `/home`). Both survive stop/start and are removed with `delete`; the new `POST /api/v1/workspaces/{id}/reset` drops only the overlay; sizes come from the create request (`root_mib`/`home_mib`) with `MSKSD_ROOT_MIB`/`MSKSD_HOME_MIB` defaults, and the workspace row records the owning host, so a start/stop/reset/delete from another host answers a named 409. The shipped image boots its root read-write through the overlay. See `docs/storage.md` for the layout, lifecycle, and env-var reference.

- **Workspace boot performance (#37).** Start → interactive shell is now ~3.1s p50 on the reference host (was 6.7s; goal < 5s), measured by the new `scripts/perf-boot.py` harness, which also reports the VMM's peak resident set (165–185 MiB for a 1024 MiB guest). The shipped image boots Debian's cloud kernel (ext4/virtio-pci built in) with a msks-built minimal initramfs (one module, ~0.05s) and starts the vsock console before the boot completes; AppArmor, networkd, timesyncd, resolved, unattended-upgrades, and e2scrub units are off the boot. `docs/boot-speed.md` documents the breakdown and the measurement.
- **Workspace image guide (#38).** `docs/images.md` documents the image contract (container-image tar, containerDisk layout, `disk/image.json` schema 2), building an image (the shipped builder and a from-scratch outline), registering via `POST /api/v1/images` (default designation, storage cost, removal rules), and referencing images from workspace create (`name:version`, bare name, `name@hash`, bare hash).

- **Workspace image catalog with containerDisk images (#40).** The canonical image is now a container-image tar (`podman load` compatible) in the containerDisk convention (`workspace-<name>-<version>.tar`: `boot/vmlinuz`, `boot/initrd.img`, `disk/rootfs.ext4`, `disk/image.json` schema 2) — importable with stock tools. `GET/POST /api/v1/images` list and import (per-hash boot-file cache; workspace launches never unpack); workspace create accepts `"image": "name:version"` (or bare name, or hash) and falls back to the designated default, so a bare create works on a fresh deployment — `MSKSD_DEFAULT_IMAGE` imports and designates at first boot. Explicit `kernel`/`rootfs` fields still win.

- **Workspace shell (#21).** `msks shell <workspace-id>` gives an interactive shell inside a running workspace microvm, from any host that can reach the daemon: the client speaks the authenticated `/api/v1/workspaces/{id}/console` websocket (TLS + token, Ctrl-] detach, raw tty mode), and the daemon proxies it over virtio-vsock — the VM's vsock unix socket after a `CONNECT <port>` handshake — into a per-connection busybox ash on a pty served by static socat in the guest (root shell today, per #5's guest userland; `MSKSC_URL`/`MSKSC_TOKEN`/`MSKSC_CAFILE` configure the client). The guest assets gained the vsock module, `/dev/vsock` creation, devpts/ptmx setup, and socat; the daemon retries the console handshake across the guest's post-boot bring-up window.

### Breaking

- **Dev state moved under `.devenv/state/` (#156).** The repo-root
  state dirs — `.guest/`, `.msksd/` — now live at
  `.devenv/state/{guest,msksd}/`, each relocatable via
  `GUEST_DIR` or `MSKSD_STATE_DIR` (the daemon's own setting;
  use an absolute value so the tasks and the daemon land in the same
  place). Existing state does not migrate: move the directory you
  want to keep or rebuild (`devenv processes up -d`,
  `msks-dev-ready`), then delete the old dirs — they are no longer
  gitignored. The devenv shell also prunes its own stale one-shot
  wrappers (`.devenv/shell-*.sh` older than an hour) automatically.

### Changed

- **The NixOS deployment host (#229).** msks ships as a flake
  whose NixOS module runs msksd and cloud-hypervisor directly on a
  NixOS deployment host; workspaces boot under cloud-hypervisor on
  the host's `/dev/kvm` — one VM boundary between a workspace and
  the metal — and egress consent is enforced in the host kernel
  (per-VM taps, the nftables/NFQUEUE consent path, DHCP, DNS, and
  NAT arrive with the module). The appliance layer
  (`msks-appliance-*`, the #220 update channel, the state-disk
  machinery) and the Kubernetes deployment target are removed:
  updates and rollback come from rebuilding the host's NixOS
  configuration. The roadmap of the time (docs/ROADMAP.md, since removed) carried the plan.
- **Plain scripts instead of dependency-free devenv tasks (#166).**
  Every msks devenv task without `after`/`before` ordering is now a
  plain script on the devenv shell's PATH (`msks-build-guest`,
  `msks-preflight`, and the rest) — run it from a devenv shell
  directly or `devenv --quiet -O dotenv.enable:bool false shell --
<name>` from outside; `msks:uv-sync` remains the one task. The
  pinned nixpkgs source is exported as `GUEST_NIXPKGS` to every
  devenv context, and the build and lifecycle scripts print their
  start and outcome.

- **Minted identity keys default to `ed25519` (#138).** The
  client mint (`--key-type`, `msks create`) and the daemon mint
  (`ssh_key_type` / `MSKSD_SSH_KEY_TYPE`) now mint Ed25519 keys —
  FIPS-approvable (FIPS 186-5) and accepted by ssh clients
  restricted to the common `ssh-ed25519,ssh-rsa` set, so `msks ssh`
  into a default-created workspace needs no algorithm passthrough.
  `--key-type ecdsa` (the former default) and `--key-type rsa`
  remain available for deployments whose validated crypto module
  predates EdDSA. See `docs/networking.md`.
- **`msks console` (#117).** The interactive workspace command is
  renamed from `msks shell` to `msks console`, matching the
  `/api/v1/workspaces/{id}/console` websocket it speaks. A hard
  rename: `msks shell` now exits with the standard unknown-command
  error. See `docs/cli.md`.

- **msksd runs as a non-root service user (#101).** The msks
  module's systemd unit executes the daemon as a dedicated `msksd`
  system user holding exactly two ambient capabilities —
  `CAP_NET_ADMIN` (taps, nftables, and the VMM's tap opens) and
  `CAP_NET_BIND_SERVICE` (DHCP 67, DNS 53) — and nothing in its
  process tree runs as uid 0; `/dev/kvm` reaches it through the
  `kvm` group, and the state directory is service-user-owned
  (`/var/lib/msksd`). `net.ipv4.ip_forward=1` is a host setting the
  module carries when egress is enabled: msksd verifies it and
  refuses egress with the cause naming `net.ipv4.ip_forward` when it
  reads `0`. See `docs/networking.md`.

- **The workspace guest boots Debian's generic kernel (#96).** The
  workspace image archive shrinks ~19 MiB compressed (147.8 → 128.7
  MiB xz)
  as Debian's full cloud module tree leaves in favor of the
  twelve-file runtime closure the build derives from `modprobe`
  metadata (and pins by comparing the full tree's closure against
  the shipped tree's at build time). Boot
  speed holds (p50 start→prompt 3.24 s against the cloud flavor's
  2.95–3.26 s host spread; goal < 5 s), and guest memory at first
  prompt is unchanged (~140 MiB); numbers recorded in
  `docs/boot-speed.md`.

- **The workspace image ships cloud-init (#41).** The image is built from Debian's `genericcloud` cloud image instead of the cloud-init-free `nocloud` variant: cloud-init and its python3 runtime arrive with the base (~130M larger), and two dropins pin NoCloud as the only datasource and keep cloud-init off the guest's networking. The interactive boot budget is unchanged (vsock shell ~3.0s p50); existing workspaces keep the images they were created with — recreate workspaces to move them onto the new image.

- **Workspace stop is now a clean poweroff (#14).** The local backend's stop presses the ACPI power button (`vm.power-button`) and the guest's systemd-logind runs a full shutdown before the VMM exits. The endpoint stop used before was cloud-hypervisor v52's hard stop: the guest was never notified, and with persistent disks every stop dropped the writes still sitting in the guest's page cache.
- **The workspace guest is Debian 13 trixie (#30).** The rootfs comes from Debian's official nocloud cloud image (pinned by dated URL + sha512): systemd as PID 1, apt (present but inert while the root is read-only), and Debian's own kernel direct-booted. `msks-build-guest`, the manifest contract, and the smoke path are unchanged; the busybox guest is gone. See the README for build details, timings, and the setuid/ownership notes.

- **Local-driver and TLS hardening fixes from the first e2e (#10).** The local driver now honors the absent-VM contract on stop/kill (a stale api socket behind a dead VMM reported ECONNREFUSED and delete-after-stop 500'd), the rootfs disk is declared `readonly` + `image_type: Raw` in `vm.create` (v52's autodetection otherwise disables sector-0 writes, and O_RDWR on the read-only store share fails), and `tls._write` loops `os.write` (a short write through virtio-backed storage left a truncated CA key).

- **The `msksd` daemon and its `/api/v1` API (#8).** msksd now serves versioned endpoints over one HTTPS+WSS listener — public health, hashed bearer-token auth with revocation (`MSKSD_BOOTSTRAP_TOKEN` seeds the first credential), workspace create/list/status/start/stop/delete driving cloud-hypervisor through the microvm seam, and a websocket event channel for lifecycle transitions. TLS is operator-provided (`MSKSD_TLS_CERT`/`KEY`) or a self-signed CA generated on first run whose fingerprint is logged for trust-on-first-use pinning; `--no-tls` serves plain HTTP for development. State lives in an SQLite database under the state dir (`MSKSD_STATE_DIR`), managed by Alembic migrations.
- **Nix-built guest assets (`msks-build-guest`, `msks-demo-vm`).** The devenv now produces everything needed to boot a microvm — kernel, initrd, read-only ext4 rootfs into the guest state dir (`.devenv/state/guest/`) — from the nixpkgs revision devenv itself pins, on any Linux host with nix; the manual-download flow is gone. Boot tests pick the built artifacts up automatically (explicit `TEST_VMLINUX`/`TEST_ROOTFS`/`TEST_INITRD` variables keep precedence) and skip themselves when the guest was never built or `/dev/kvm` is unusable. `msks-demo-vm` boots one interactive VM from the artifacts with `ch-remote` ready (#5).

- **`msksd --reload` (development, #144).** The daemon gains a
  development flag that watches the msks package tree it runs from
  and restarts the process when it changes.

### Fixed

- **Console websocket closes protocol-clean (#217).** The console
  bridge now ends every session with a close frame instead of
  dropping the TCP: a guest that refuses the console signature
  closes with 4403 and the guest's `MSKS ERR` text as the reason
  (watched until the helper's AUTH OK verdict, so a logged ERR line
  in later shell output is not one), a guest stream that ends
  (shell exit) closes with 1000, and only a client disconnect ends
  without one. Clients can tell refusal apart from transport death.

- **The nix-built msks package ships `textual` (#203).** The
  package set shipped without `textual`: the msks wheel build
  failed its runtime dependency check, and a daemon built from it
  crashed at import time. `nix/textual-pkg.nix` builds the pinned
  8.2.8 wheel into the closure, and a new test keeps
  `nix/msks-pkg.nix` mirroring `[project.dependencies]` — the next
  dependency addition fails a unit test instead of the nix build.
- **Guest images keep the base image's file
  ownership (#179).** `apt` inside a workspace now sees the
  stock Debian metadata the base image ships: `mandb`'s cache is
  `man:man` again (an `apt upgrade` printed `Permission denied` on
  the whole man cache), and every other non-root owner — the
  shadow-group password files, `utmp` logs, the
  `systemd-network`, `dbus`, and `polkit` state directories —
  round-trips with it. Build-time pins fail the image build when
  `sudo` loses its setuid bit or the man cache loses its owner.
  Workspaces must be recreated to pick up a rebuilt image.

- **Workspace home and the guest's account list (#171).** The
  first-boot identity seed now creates `/home/msks` owned by the
  workspace user and populates it from `/etc/skel` (a home left
  root-owned is repaired on the next provisioning), and the guest
  image ships a cloud-init dropin that keeps the Debian cloud
  image's own default account (`debian`, with `/home/debian` and a
  passwordless-sudo entry) from ever being created — a workspace
  created from this image carries `msks` as its only account.
- **`msks ssh` waits out a just-booted workspace's identity seed
  (#168).** The first `msks ssh` against a freshly created
  workspace could exit `Permission denied (publickey)`: the guest's
  sshd answers before first-boot provisioning has written
  `authorized_keys`, and a bare retry succeeds. A session whose
  pre-flight booted the workspace now probes the login (a
  throwaway `true` command, retried up to 30s) before opening the
  real session, which runs exactly once either way.

- **Workspace `sudo` works — the guest image keeps the setuid bits
  (#169).** The image build extracts the Debian root unprivileged,
  which dropped every setuid/setgid bit (`sudo`, `su`, `mount`, …);
  the `msks` workspace user hit sudo's refusal — "must be owned by
  uid 0 and have the setuid bit set". The build now restores the
  bits from the source image's own inode modes, and the image
  grants the `msks` user passwordless sudo (the locked password
  makes NOPASSWD the only form that can run), so `sudo apt update`
  works over a workspace console or ssh. Workspaces created from an
  older image keep their old backing rootfs until recreated
  (`msks rm`, then `msks create`).

- **Failed starts keep an actionable status (#158).** A `vm.boot`
  failure reaps the half-created VMM and the watcher's dead-socket
  probe consults the identity check, so the workspace reports
  `stopped` — not the incident's `unknown`, which a bare pid check
  satisfied with a recycled pid — and the next `msks start`
  retries. The incident's exact shape, recycled-pid trigger
  included, is pinned by tests, and a stop/start lands a merged
  fix.

- **Stale workspace sockets no longer break start (#151).** A VMM
  killed without cleanup (host crash, hard stop) left
  `api.sock`/`vsock.sock` behind under `vms/<id>/`; the next start
  died binding them and surfaced a misleading
  unreachable-API/ENOENT 503. The start path now sweeps refused
  sockets (a live listener is never unlinked), removes the stale
  `ch.pid` (a recycled pid after a host reboot no longer blocks
  starts with "already exists"), and a VMM that dies at spawn names
  its own cause in the error (the tail of its ch.log) instead of a
  bare exit code. VMM liveness is identity-checked against
  `/proc/<pid>/cmdline`: a pid that is not this workspace's VMM is
  never signaled, and one owned by another user is named in the
  error instead of silently reported stopped.
- **Legacy CA crash-loop on leaf remint (#148).** A CA minted before
  the strict-clean change parses but carries no Subject Key
  Identifier; the leaf remint that a host change triggers read the
  missing extension and exited, and `Restart=always` looped the
  daemon forever (observed live on a long-lived state disk after
  #146 changed the bind host). Such a CA is now replaced wholesale —
  a stderr line names the replacement beside the new fingerprint —
  instead of crash-looping. Current
  strict-clean pairs keep leaf-only remints (same CA fingerprint).
- **Strict-clean minted TLS certificates (#141).** The CA and leaf
  msksd generates now carry Subject/Authority Key Identifiers and
  the CA a `keyCertSign` KeyUsage, so Python 3.14 clients — whose
  default SSL context enables `VERIFY_X509_STRICT` — verify the pair
  instead of rejecting it. Existing state directories keep their
  current pair; removing the `msks-ca*.pem`/`msks-cert*.pem` files
  under the state dir lets the next start mint the strict-clean
  replacement.
- **Mid-session console stalls no longer wedge a session open (#103).**
  The guest console helper's byte pump blocked on whichever direction
  stalled first, so a wedged vsock transport froze the whole session —
  input echoed but never executed, no output, no close. The pump now
  moves each direction independently and a stream that accepts no
  writes for 300 s ends the session; on the daemon side,
  `console_stall_timeout_s` (`MSKSD_CONSOLE_STALL_TIMEOUT_S`, 60 s
  default, `0` disables) closes a console websocket with 4502 when
  client input draws no guest bytes for the window — `msks shell`
  names the close, and reconnecting opens a fresh session. The
  echo-off caveat (password prompts) and the helper-window
  relationship are documented in `docs/cli.md` and `docs/config.md`.
- **Egress DHCP/DNS under uvloop-running daemons (#144).** The
  development venv pulls uvloop (via `uvicorn[standard]`), which
  does not implement `loop.sock_recvfrom`/`sock_sendto`; egress
  workspaces created by such a daemon never received a DHCP lease
  or DNS answer. The DHCP and DNS services now receive datagrams
  through a loop-portable helper and behave identically under
  asyncio and uvloop. This was also the real cause behind the
  "guest NIC up but no frames" symptom recorded on #143 — not the
  container boundary.

- **`/home` could fail to mount on slow boots (#14).** The guest fstab mounted the home volume by label under `x-systemd.device-timeout=2s`; that clock starts at sysinit job enqueue, before udevd runs, and on a first boot from a fresh overlay (every root read a copy-on-write miss) the udev label probe can exceed what is left of the budget — `home.mount` then fails for the whole boot (`nofail` keeps the boot moving and never retries). The device timeout is now 30s, so the mount survives a slow coldplug; a boot with no volume at all waits the same 30s once and continues.

- **Egress workspaces never took a DHCP lease on fast hosts (#36).** The guest reaches multi-user immediately after sysinit, and a networkd that enumerates its NIC while udev is still renaming it (eth0 to ens3) never manages the renamed link — DHCP never runs. The guest image orders networkd after `systemd-udev-trigger.service` and `systemd-udevd.service`, so the interface name is final before the first enumeration.

- **`ws_url` quotes the token and workspace id (#36).** The console URL interpolates `MSKSC_TOKEN` raw into the query string; minted tokens are urlsafe today, so this was theoretical, but any future charset with `+`, `&`, `=`, or `%` would have mangled it. The token now passes through `urllib.parse.quote_plus` (a query value), and the workspace id through `urllib.parse.quote` with no safe characters (a path segment, where `+` would reach the server literally).

- **Workspace cleanup and image import release their resources (#56).** `cleanup` now stops the workspace's VMM before deleting its directory — tracked or pidfile, like `kill` — where it previously orphaned the process. Image import closes the container-image archive after unpacking; each import previously left the archive's file handle open until garbage collection.
- **Workspace shell input now echoes (#61).** The vsock console's
  guest pty ran with `echo=0,icanon=0` while the client keeps the
  local tty raw, and the service environment's `TERM=dumb` left
  bash's readline off — so typed characters reached nothing that
  would show them. The pty is now a plain canonical terminal
  (`echo=1,icanon=1`) and the unit sets `TERM=xterm`: line editing
  and history work, and programs reading stdin directly get normal
  tty echo. The console ships in the workspace image; a workspace
  keeps the image it was created with, so delete existing
  workspaces and the old image from the catalog — new workspaces
  then bind the new image.
- **`devenv shell` no longer runs the pre-commit suite at shell entry (#32).** A devenv 2.3.x scheduler regression pulled `devenv:git-hooks:run` into the shell's task graph, so a failing hook (e.g. the xenon complexity gate) aborted shell entry before it opened — with no way to use the shell to fix the failure. The suite still runs on `git commit` and as the `msks-xenon` task.
