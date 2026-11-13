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

A forward proxy (`-L`) binds a port on the daemon's host. Every connection
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

A forward proxy's target is the workspace itself. The spec's host field names
the workspace, the way ssh's `-L` host names the far end, and it accepts
exactly two spellings: `localhost` or `127.0.0.1`. Anything else refuses with
a message that points at the `-R` direction, which is the one that reaches
daemon-side services.

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
listener. `failed` means the bind refused, and the cause follows the
word (a taken port, an address the host cannot bind). `stopped` means
nothing serves the row: a remote proxy on a workspace that is not running,
for example. A `listening` remote row whose admission swap failed carries
the cause the same way. The listener serves, and the guest cannot reach it.

Each transition publishes an event on the daemon's hub (`proxy.created`,
`proxy.listening`, `proxy.failed`, `proxy.closed`, `proxy.removed`). The TUI
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
`http://127.0.0.1:8080` and reaches the guest's server.

### Reaching a guest service from other machines

The same forward with a bind address:

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

A forward proxy binds at once, on a stopped workspace too; each connection
refuses until the guest runs. A remote proxy on a stopped workspace records
the row, and the workspace's next start brings its listener up. A stop
closes every listener the workspace owns. Each start binds them again. A
proxy whose bind fails records the failure as its status with the cause,
and every later start retries it.

## Managing proxies from the TUI

[The TUI chapter](tui.md#the-proxies-page) documents the page key by key.
This section walks the same work by hand.

Open the workspace's page (`msks tui my-workspace`, or Enter on the list's
row). The page carries a compact proxies line beside the consent and mounts
lines:

```text
8888 -> 8889, 9111 -> 9115 | 8881 <- 8882
```

The forwards sit before the bar, the remotes after it. Each pair reads
`listen -> target` (or `<-` for a remote). A listening pair shows green. A
pair whose bind refused shows red. The line moves on every proxy transition
the daemon publishes, without a refresh.

Press `p` (or Enter on the Proxies action row) for the workspace's proxies
page. The page lists that workspace's rows with the spec in the CLI's
grammar, the label, and the live status. Press `a` to add one.

The form holds the direction (`forward (-L)` or `reverse (-R)`), the spec in
the same grammar `msks proxy add` reads, and a label. A spec the parser refuses
keeps the form standing, with the rule on the note line. `D` (or Enter, on the
focused row) removes it behind a confirmation. `q` or Escape returns to the
workspace page.

The page re-reads the listing every five seconds and on every proxy
transition, so a bind that fails while the page stands paints itself without
a keypress.
