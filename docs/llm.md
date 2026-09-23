# The workspace LLM proxy

Every workspace consumes LLMs through msksd. The daemon holds the
provider credentials and the routing configuration; each workspace
holds only its own proxy credential and talks to an OpenAI-shaped
endpoint served on its own tap. Provider keys never enter a
workspace, and a workspace uses LLMs with its egress consent
untouched — including a workspace with no egress at all (once one
is attached) or a `static` workspace whose allowlist names no
providers, because the daemon makes the upstream connection itself.

## The two routing modes

The model list is one setting, `llm_models`
(`MSKSD_LLM_MODELS`), carrying entries spelled
`provider/model:api_base:api_key` — comma-separated in the
environment, or a YAML list in the config file, where each entry
may also be a **LiteLLM-native dict** (klangk's YAML shape) for
the routing knobs the string grammar cannot spell:

```yaml
llm_models:
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_base: https://api.openai.com/v1
      api_key: file:/etc/msksd/openai.key
  - model-name: claude-sonnet-4
    litellm-params:
      model: anthropic/claude-sonnet-4
      api_key: cmd:pass show anthropic
      rpm: 10
```

Dict keys accept kebab- or snake-case, `params` is the
`litellm_params` shorthand, and `file:`/`cmd:` indirection works
on `api_key`/`api_base` inside the block. An environment string
overrides the file's list wholesale. The colons of the string
grammar split with two exceptions, so the common shapes all parse:

- an entry whose key is a `file:` or `cmd:` reference keeps
  everything from the marker — paths and commands carry colons
  (`openai/gpt-4o::file:/etc/msksd/openai.key`);
- a scheme-bearing base keeps its port and any key after its path
  (`openai/gpt-4o:https://api.openai.com/v1:sk-...`).

A **single** entry whose model name is `*` is _passthrough mode_:
the entry's base names one upstream, requests forward verbatim
over httpx, and `/models` is discovered dynamically from the
upstream — every model the upstream serves is available with no
further configuration. Any other list is _multi-provider mode_:
the entries become a [`litellm`](https://github.com/BerriAI/litellm)
router, workspaces address models by the name after the provider
slash, and a request that names no model — or a name the list does
not carry — uses the first configured model. Known providers
(`openai`, `anthropic`, `cohere`, `mistral`, `groq`, `together_ai`,
`deepseek`, `fireworks_ai`) fill their default base when an entry
names none.

An empty list (the default) presents no LLM surface at all:
nothing binds on any tap, and the per-workspace input chain admits
no proxy port.

## Secrets stay out of the config file

Every secret-bearing value resolves indirection at configure time:
`file:` reads a path, `cmd:` runs a shell command and takes its
stdout. It works on each entry's key and on the shared default
`llm_api_key` (`MSKSD_LLM_API_KEY`). A broken reference refuses
the configure — the daemon log carries the named error (the
entry's own text, operator-visible), and requests answer a 503
that names only the failure, never the entry — instead of sending
an empty key upstream.

## The listener, the firewall, and the credential

Each egress workspace's tap carries its host-reachable services —
DHCP (67), the resolver (53), the interceptor's listener while
armed (#199), and — when a model list is configured — the proxy at
`llm_port` (`MSKSD_LLM_PORT`, default `8770`). The listener binds the tap's own address, and that
workspace's per-VM nftables input chain admits exactly this port
from exactly that tap, pinned to the guest's source address. A
workspace cannot reach another workspace's proxy, and nothing
outside a tap can reach any of them.

The credential is per workspace, minted at create and stored on
the workspace's row. The proxy accepts one thing: that workspace's
token in the `Authorization: Bearer` header. Daemon API tokens,
anonymous requests, and any other credential answer 401 — the
proxy is usable only from inside a workspace, by that workspace.

The first-boot seed delivers it (`docs/networking.md` describes
the tap; the identity seed is the vehicle): the token lands at
`/etc/msks/llm.token` and `/etc/profile.d/msks-llm.sh` exports
`OPENAI_BASE_URL` — the DHCP lease's gateway and the daemon's port
— and `OPENAI_API_KEY`. A login shell inside the workspace is
therefore already configured for every OpenAI-shaped client:

```console
$ env | grep OPENAI
OPENAI_BASE_URL=http://172.31.0.2:8770/v1
OPENAI_API_KEY=msksllm1_...
```

Retrieve or rotate a credential with the CLI or API — a token
holder already owns the workspace's root console, so the private
half serving rule is the identity's:

```console
msks llm-token myws            # the stored credential
msks llm-token myws --remint   # a fresh one, replacing the row's
```

A reminted credential does not re-run the seed — the seed is
immutable create-time input — so export the new one inside the
workspace by hand after rotating.

One more create-time fact rides the seed: the port. The planted
`OPENAI_BASE_URL` names the port the daemon served when the
workspace was created. Moving `MSKSD_LLM_PORT` later does not
rebind a live listener — each workspace's listener and firewall
admission move at its next stop/start, and until then that
workspace's LLM surface stays on the old port (fail-closed:
refused). A workspace created before a port change needs the
same hand update for its planted environment (or a recreate).

One upstream-side behavior worth knowing in multi-provider mode:
providers that want images inline make the daemon fetch remote
URLs a request's message parts name — litellm's own URL
validation guards those fetches.

## What a request may carry

The proxy forwards the fields an OpenAI chat-completion request
carries — `model`, `messages`, `stream`, sampling and tool
parameters — and drops everything else before the router sees
the body. Provider credentials and endpoints are deployment
configuration: a request that names `api_base`, `api_key`, or any
other routing parameter gets those fields dropped, never
honored. A body larger than 16 MiB answers 413; a body that is
not a JSON object answers 400.

## SIGHUP

The proxy reads settings live: a `SIGHUP` that changes the model
list re-routes the very next request wherever a listener already
serves, in either direction (models added begin serving; models
removed answer 503 on the open port — the closed-port posture of
an unconfigured daemon arrives with that workspace's next
stop/start). A workspace that booted while no model list was
configured has no listener and no firewall admission — its next
stop/start brings the surface up once the daemon is configured.
(The first request after a swap pays the reconfigure — the
lazily-imported litellm tree, a `cmd:` secret's subprocess — off
the serving loop, so no other workspace's traffic waits on it.)
