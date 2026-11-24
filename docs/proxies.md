# Workspace proxies

A proxy is a TCP port that can connect the daemon host to a guest or one that
connect a guest to the daemon host or another location.

It is defined once. The daemon owns the listener, and every start of the
workspace brings it back. This chapter shows the two directions a proxy
bridges, and how to manage proxies from the [CLI](cli.md#msks-proxy) and the
[TUI](tui.md#the-proxies-page).

## What a persistent proxy is

The daemon holds every proxy definition as a row in its database. The row
survives a daemon restart and a workspace stop. Each time the workspace
starts, the daemon binds the row's listener again.

Two directions exist, and both borrow ssh's letters and ssh's spec grammar.
The spec reads `listen : target`, left to right. The flag letter says which
side listens.

A local proxy (`-L`) binds a port on the daemon's host. Every connection
to that port bridges into the workspace, to the guest port the spec names.
This is the ssh `-L` shape: a port on your side reaching a port on the far
end.

A remote proxy (`-R`) binds a port on the workspace's own gateway. A process
inside the guest connects to its DHCP gateway on that port, and the daemon bridges
the connection to a daemon-side service the spec names. This is the ssh `-R`
shape: a port on the far end reaching a service on your side.

The contrast with [`msks forward`](cli.md#msks-forward) is ownership. A
forward's listener lives as long as the command holding it, and it binds on
the machine the command runs on. A proxy's listener lives in the daemon. Use
a forward for a one-off pipe during a session. Use a proxy for a port you
want there more permanently.

### What each side connects to

A local proxy's target is the workspace itself. The spec's host field
resolves at the far end, inside the guest, exactly as it does in ssh's
`-L`: `localhost` and `127.0.0.1` reach the guest's own loopback, and any
other name or address reaches whatever the workspace itself can reach —
the guest's sshd makes the connection and resolves it. The reach is the
workspace's own: workspaces cannot talk to each other (the daemon drops
traffic between taps), and a daemon-side service is the `-R` direction's
job. The connection on the far end is made by the workspace's bridge, the
holder the next section covers.

The daemon connects to a remote proxy's target from the daemon's host. In
the spec `-R 9200:localhost:9200`, the `localhost` is the daemon's own
loopback. A name like `db.internal` resolves through the daemon host's
resolver.

A remote proxy's listen address is fixed by the workspace's tap, so the
grammar carries no bind field for `-R`.

Inside the guest, connect to `host.msks`. Every workspace's resolver answers the
name with its own tap address. `ip route show default` prints the same
address when a name is unavailable:

```bash
curl http://$(ip route show default | awk '{print $3}'):9200
```

A workspace created without egress has no NIC. Adding a proxy to it is
refused: `workspace … has no NIC (created without egress)`.

### The bridge

A local proxy reaches the guest through the workspace's bridge: a client
that holds the workspace identity serves the rows' connections. Each
connection to a proxy port travels to that client, and the client carries
it into the guest over ssh, to `127.0.0.1` on the spec's port. A service
bound to the guest's loopback answers. This is the ssh `-L` behavior: the
host field resolves where the ssh session ends.

The daemon runs no ssh and holds no key material. The client's ssh logs in
over the forward websocket with the operator's key the create seeded into
the guest, from the client's agent (`SSH_AUTH_SOCK`) or its configured
identity file. The daemon relays bytes between its listener and the
client, nothing more.

`msks start` leaves a holder behind when the workspace has local rows, so
the proxies serve after the command exits. The holder is a small process
on the machine the command ran on. It serves until the workspace stops or
the daemon goes away, and it exits then by itself. `msks proxy hold
<workspace>` runs one on demand.

The rows work while a holder serves. A start over the raw API, or from a
client without the key, still boots the workspace; its local rows record
`failed` with the cause until a holder connects. When several holders
stand, the first to connect serves the connections. The client's ssh pins
the guest's host key in its own cache, the same file `msks ssh` uses.

An open TUI or GUI re-holds by itself (#1127). When a holder dies while
the workspace keeps running, or a start leaves none behind, the client
spawns a fresh holder: the same detached one a start leaves. One line on
the screen it is showing says the bridge came back. It tries three times
per workspace per session, waiting 30 seconds between tries. A client
that cannot resolve the workspace identity stops there and leaves the
row's cause visible. The CLI never does this: a one-shot command is not
a session, and `msks proxy hold <workspace>` remains the by-hand path.

The listener binds on the daemon host, so whoever can reach
`listen_host:listen_port` — loopback only by default — connects through
the workspace's reach. A bind beyond loopback widens that audience
deliberately, the same way it does for any daemon-side listener.

### Reach and consent

The daemon connects to a remote proxy's target itself. The consent machinery
gates the guest's traffic through the tap; it never sees the daemon's own
connections. A remote row therefore reaches its target without a verdict. [The
consent chapter](egress-consent.md) covers the guest's connections; this connection
is the daemon's own.

The tap's ingress chain admits a remote port only while its listener is up.
Each accept line is pinned to that tap's address and that guest's source. A
bind that fails admits nothing. A live add or removal swaps the admission in
one nft transaction, so the rule and the service always agree.

### Status and events

Every proxy carries a live status. `listening` means the daemon holds the
listener. For a local row that also means the workspace's bridge stands:
its connections reach the guest. `failed` means the bind refused, or the
bridge is down, and the cause follows the word (a taken port, an address
the host cannot bind, the bridge's named cause). `stopped` means
nothing serves the row: a remote proxy on a workspace that is not running,
for example. A `listening` remote row whose admission swap failed carries
the cause the same way. The listener serves, and the guest cannot reach it.

Each transition publishes an event on the daemon's hub (`proxy.created`,
`proxy.updated`, `proxy.listening`, `proxy.failed`, `proxy.closed`,
`proxy.removed`). The TUI
subscribes, so its proxy surfaces move without a keypress.

## Managing proxies from the CLI

The `msks proxy` command group owns the rows. [Its reference
section](cli.md#msks-proxy) lists every flag; this section walks the usual
work.

### Reaching a guest service from the daemon host

A web server inside the workspace, on guest port 8080:

```bash
msks proxy add my-workspace -L 8080:localhost:8080
```

The daemon binds `127.0.0.1:8080` on its own host. A browser there opens
`http://127.0.0.1:8080` and reaches the guest's server. The server may
bind the guest's loopback: the bridge resolves the spec's `localhost`
inside the guest. Start the workspace from a client that holds its
identity, and the bridge comes up with it.

### Reaching a guest service from other machines

The same local proxy with a bind address:

```bash
msks proxy add my-workspace -L 0.0.0.0:8080:localhost:8080
```

The daemon listens on every interface of its host. Any machine that can
route to the daemon host reaches the guest's server. The bind is the
daemon's, so the firewall rules of the daemon host govern the reach.

### Letting a guest reach a daemon-side service

A database on the daemon host, port 5432, labeled for the listing:

```bash
msks proxy add my-workspace -R 5432:localhost:5432 --label db
```

The workspace's gateway listens on 5432. Inside the guest, an application
connects to `host.msks` on that port:

```bash
psql -h host.msks -p 5432
```

While [secret interception](secrets.md) is armed, the tap's 80 and 443
answer the interceptor; a `-R` proxy on either port stays unreachable from
the guest until interception stands down.

The daemon connects to `localhost:5432` on its own host for every guest
connection. A target on another host works the same way
(`-R 5432:db.internal:5432`). The daemon host's resolver resolves it.

### Reading and removing

```bash
msks proxy ls            # every stored proxy, with live status
msks proxy ls --json     # the same rows as JSON
msks proxy rm my-workspace 3
```

The listing shows each row's direction, listen and target endpoints, label,
and live status. A removal closes the listener at once and drops the row.

`add` is declarative. Asking twice for the same spec returns the standing
row. A different spec on the same bind is refused with the row that holds
it.

A local proxy binds at once, on a stopped workspace too. Each connection
refuses until a holder serves the workspace: `msks start` leaves one
behind (a row already reading `listening` means one stands, and the start
spawns nothing), and `msks proxy hold` runs one on demand. A remote proxy
on a stopped workspace records the row, and the workspace's next start
brings its listener up. A stop closes every listener the workspace owns
and ends its holders. A proxy whose bind fails records the failure as its
status with the cause, and every later start retries it.

## Managing proxies from the TUI

[The TUI chapter](tui.md#the-proxies-page) documents the page key by key.
This section walks the same work by hand.

Open the workspace's page (`msks tui my-workspace`, or Enter on the list's
row). The page carries a compact proxies line beside the consent and mounts
lines:

```text
8888 -> 8889, 9111 -> 9115 | 8881 <- 8882
```

The local pairs sit before the bar, the remote pairs after it. Each pair reads
`listen -> target` (or `<-` for a remote). A listening pair shows green. A
pair whose bind refused shows red. The line moves on every proxy transition
the daemon publishes, without a refresh.

Press `p` (or Enter on the Proxies action row) for the workspace's proxies
page. The page lists that workspace's rows with the spec in the CLI's
grammar, the label, and the live status. Press `a` to add one.

The form holds the direction (`local (-L)` or `remote (-R)`), the spec in
the same grammar `msks proxy add` reads, and a label. A spec the parser refuses
keeps the form standing, with the rule on the note line. `e` or Enter opens the
same form seeded with the focused row (its direction, its spec, and its label)
and saves the row whole; the daemon swaps the listener to the new spec at once.
`D` removes the focused row behind a confirmation. `q` or Escape returns to the
workspace page.

The page re-reads the listing on every proxy transition the daemon
publishes, so a bind that fails while the page stands paints itself without
a keypress. There is no polling timer: a daemon at rest sends the page
nothing.
