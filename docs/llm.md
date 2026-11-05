# The workspace LLM proxy

Every workspace can consume LLM endpoints through msksd. The daemon holds the
provider credentials and the routing configuration; each workspace holds only
its own proxy credential and talks to endpoints served on its own tap.

Provider keys never enter a workspace, and a workspace uses LLMs with its
egress consent untouched. This holds even for a workspace with no egress at all
(once one is attached), or a `static` workspace whose allowlist carries no
providers: the daemon makes the upstream connection itself.

`msks` uses **LiteLLM** under the hood for this capability.

## The two routing modes

The model list is one `msks.yaml` config file setting, `llm_models`. Its
entries are spelled in YAML dict format. The `MSKSD_LLM_MODELS` environment
variable can also be used: the environment form takes them comma-separated ala
`provider/model:api_base:api_key`.

Here's an example of the config file format:

```yaml
llm_models:
  - model_name: gpt-4o
    params:
      model: openai/gpt-4o
      api_base: https://api.openai.com/v1
      api_key: file:/etc/msksd/openai.key
  - model-name: claude-sonnet-4
    params:
      model: anthropic/claude-sonnet-4
      api_key: cmd:pass show anthropic
      rpm: 10
```

Dict keys accept kebab- or snake-case, `params` is a shorthand for LiteLLM's
`litellm_params`. `file:`/`cmd:` indirection works on `api_key`/`api_base`
inside the block.

An environment string overrides the config file's list wholesale. The colons of
the string grammar split with two exceptions, so the common shapes all parse:

- an entry whose key is a `file:` or `cmd:` reference keeps
  everything from the marker (paths and commands carry colons:
  `openai/gpt-4o::file:/etc/msksd/openai.key`);
- a scheme-bearing base keeps its port and any key after its path
  (`openai/gpt-4o:https://api.openai.com/v1:sk-...`).

A **single** entry whose model name is `*` is _passthrough
mode_. The entry's base gives one upstream, requests forward
verbatim over httpx, and `/models` is discovered dynamically
from the upstream. Every model the upstream serves is
available with no further configuration.

```yaml
llm_models:
  - model_name: "*"
    params:
      api_base: https://api.openai.com/v1
      api_key: file:/etc/msksd/openai.key
```

Any other list is _multi-provider mode_: the entries become a
[`litellm`](https://github.com/BerriAI/litellm) router.

A workspace addresses a model by its logical name: the part after the provider
slash for a string entry, or the explicit `model_name` for a dict one. A
request that gives no model, or a name the list does not carry, uses the first
configured model. Known providers (`openai`, `anthropic`, `cohere`, `mistral`,
`groq`, `together_ai`, `deepseek`, `fireworks_ai`) fill their default base when
an entry gives none.

An empty list (the default) presents no LLM surface at all:
nothing binds on any tap, and the per-workspace input chain admits
no proxy port.

## The Anthropic Messages route

The proxy also serves `/v1/messages`, the Anthropic Messages API
claude-code speaks. The route serves both modes, on the same port
as the OpenAI pair.

In passthrough mode the request forwards verbatim to the
upstream's own `/v1/messages` endpoint. The upstream must serve
the Messages API itself for this route: Anthropic's API, or any
Anthropic-compatible gateway. The entry's key rides as
`x-api-key`, and the `anthropic-version` and `anthropic-beta`
headers forward with `2023-06-01` as the default version. A
passthrough entry pointed at an OpenAI-shaped upstream still
serves the OpenAI pair; the Messages route answers that upstream's
own error.

In multi-provider mode the proxy translates. A Messages request
becomes a chat completion, the router answers, and the answer
comes back as a Messages message: content blocks, `stop_reason`,
and usage mapped field for field. A streamed answer renders as
Messages events (`message_start` through `message_stop`). A
request that names a model the list does not carry uses the first
configured model, the same rule the OpenAI pair follows, so
claude-code's own model names need no mapping.

The route carries the same gates as the OpenAI pair: the tap is
the credential, the body caps are the same, and the request's
forwardable fields are an allowlist. A guest body naming
`api_key` or `api_base` drops those fields, in both modes.

## Secrets stay out of the config file

Every secret-bearing value resolves indirection at configure time: `file:`
reads a path, `cmd:` runs a shell command and takes its stdout. It works on
each entry's key and on the shared default `llm_api_key`
(`MSKSD_LLM_API_KEY`). A broken reference fails the configure: the daemon log
carries the named error (the entry's own text, operator-visible), and requests
answer a 503 that gives only the failure, never the entry, instead of sending
an empty key upstream.

## The guest environment

The first-boot VM seed configures the clients; the identity seed
is the vehicle, and the tap is the one described in
[Guest networking](networking.md). `/etc/profile.d/msks-llm.sh`
exports `MSKSWS_BASE_URL`, built from the DHCP lease's gateway
and the daemon's port.

It also exports `MSKSWS_API_KEY`, a placeholder the proxy never reads: it
exists for OpenAI-shaped clients that refuse an empty key. The MSKSWS names
carry the msks prefix because the proxy is this daemon's own service; a
vendor-shaped name would claim otherwise. The seed exports one vendor-shaped
pair on purpose (`#765`): `ANTHROPIC_BASE_URL` points at the proxy's Messages
route on the same port, and `ANTHROPIC_AUTH_TOKEN` is the same placeholder.
claude-code reads those two variables and no others, so a login shell inside
the workspace is already configured for it and for every MSKSWS-aware client:

```console
$ env | grep -e MSKSWS -e ANTHROPIC
MSKSWS_BASE_URL=http://172.31.0.2:8770/v1
MSKSWS_API_KEY=msks-local-proxy
ANTHROPIC_BASE_URL=http://172.31.0.2:8770
ANTHROPIC_AUTH_TOKEN=msks-local-proxy
```

The Anthropic pair is seeded even when the daemon serves no model
list, like the MSKSWS pair. claude-code then points at a port with
no listener and fails fast until a model list is configured.

All guest images ship the agent toolchain beside it: each image bakes pinned
Node and the pinned **pi** coding agent on every account's default `PATH`: the
Debian/Fedora image under `/usr/local`, the NixOS image through its system
profile.

Each also plants a pi extension (`llm-models.ts`) in `/etc/skel` and
root's home. Every account the identity seed provisions copies the skeleton, so
each login user ends up with the extension in `~/.pi/agent/extensions/`, where
pi discovers it. At every pi startup the extension makes one fetch of
`${MSKSWS_BASE_URL}/models`, aborted at 1.5 seconds, and registers the catalog
under the `msks` provider, so `/model` always shows the daemon's live model
list.

A workspace whose daemon serves no model list registers
nothing after that one attempt, and pi starts without the
provider. The environment pair is seeded even when no proxy
listens, so the fetch, not the shell, is what discovers the
difference. A peer that answers headers fast and trickles its
body can hold the read slightly past the abort. A copy a user has
edited stays edited; nothing re-overwrites it.

Two limits to know. A home that existed before the image carried
the extension keeps its old state: the extension reaches accounts
provisioned after the image did, and an older home can copy it
from `/etc/skel/.pi/agent/extensions/`. The model metadata the
extension registers is a placeholder:
128k context, 8192 max output, plain text chat, zero cost.
The proxy's OpenAI-shaped model list carries no limits, so a
model that serves more is under-planned and one that serves
less can over-generate. Set per-model values in
`~/.pi/agent/models.json` when it matters.
The toolchain pins move with an image rebuild (the shared pins in
`nix/agent-toolchain.nix`; the Debian Node tarball pin in
`nix/guest-debian.nix`); a workspace that already booted keeps
what it booted with.

One more create-time fact travels the seed: the port. The planted
`MSKSWS_BASE_URL` carries the port the daemon served when the
workspace was created. Moving `MSKSD_LLM_PORT` later does not
rebind a live listener: each workspace's listener and firewall
admission move at its next stop/start, and until then that
workspace's LLM surface stays on the old port (fail-closed:
refused). A workspace created before a port change needs the
same hand update for its planted environment (or a recreate).

One upstream-side behavior worth knowing in multi-provider mode:
providers that want images inline make the daemon fetch remote
URLs a request's message parts carry; litellm's own URL
validation guards those fetches.

## What a request may carry

The proxy forwards the fields an OpenAI chat-completion request
carries (`model`, `messages`, `stream`, sampling and tool
parameters) and drops everything else before the router sees
the body. Provider credentials and endpoints are deployment
configuration: a request that gives `api_base`, `api_key`, or any
other routing parameter gets those fields dropped, never
honored. A body larger than 16 MiB answers 413; a body that is
not a JSON object answers 400.

## SIGHUP

The proxy reads settings live. A `SIGHUP` that changes the
model list re-routes the very next request wherever a listener
already serves, in either direction: models added begin
serving, and models removed answer 503 on the open port. The
closed-port posture of an unconfigured daemon arrives with
that workspace's next stop/start. A workspace that booted while
no model list was configured has no listener and no firewall
admission; its next stop/start brings the surface up once the
daemon is configured. (The first request after a swap pays the
reconfigure: the lazily-imported litellm tree, a `cmd:` secret's
subprocess, off the serving loop, so no other workspace's
traffic waits on it.)
