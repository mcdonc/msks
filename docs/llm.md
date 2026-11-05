# The workspace LLM proxy

Every workspace can consume LLM endpoints through msksd. The daemon holds the
provider credentials and the routing configuration; each workspace holds only
its own proxy credential and talks to endpoints served on its own tap.

Provider keys never enter a workspace, and a workspace uses LLMs with its
egress consent untouched. This holds even for a workspace with no egress at all
(once one is attached), or a `static` workspace whose allowlist carries no
providers: the daemon makes the upstream connection itself.

`msks` uses **LiteLLM** under the hood for this capability.

## Routing modes

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
  - model_name: claude-sonnet-4
    params:
      model: anthropic/claude-sonnet-4
      api_key: cmd:pass show anthropic
      rpm: 10
```

A workspace addresses a model by its logical name: the part after the provider
slash for a string entry, or the explicit `model_name` for a dict one. A
request that gives no model, or a name the list does not carry, uses the first
configured model. Known providers (`openai`, `anthropic`, `cohere`, `mistral`,
`groq`, `together_ai`, `deepseek`, `fireworks_ai`) fill their default base when
an entry gives none.

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

An empty list (the default) presents no LLM surface at all:
nothing binds on any tap, and the per-workspace input chain admits
no proxy port.

## The Anthropic Messages route (for Claude API access)

`claude` is preinstalled in every msks VM.

The LLM proxy serves `/v1/messages`, the Anthropic Messages API claude-code
speaks.

Configuring the proxy exclusively for claude-code API access is one `msks.yaml`
block. The passthrough form names the endpoint, and every model it
serves is available:

```yaml
llm_models:
  - model_name: "*"
    params:
      api_base: https://api.anthropic.com/v1
      api_key: file:/etc/msksd/anthropic.key
```

The environment form is equivalent:
`MSKSD_LLM_MODELS='*:https://api.anthropic.com/v1:file:/etc/msksd/anthropic.key'`.

An Anthropic-compatible gateway works the same way: point
`api_base` at the gateway's own base.

A multi-provider form serves claude-code beside other clients.
An entry whose logical name is the model name claude-code sends
answers that model's requests directly; a name the list does not
carry falls to the first entry:

```yaml
llm_models:
  - model_name: claude-sonnet-4-5
    params:
      model: anthropic/claude-sonnet-4-5
      api_key: file:/etc/msksd/anthropic.key
  - model_name: gpt-4o
    params:
      model: openai/gpt-4o
      api_key: file:/etc/msksd/openai.key
```

With this list, a claude-code request for `claude-sonnet-4-5`
takes the first entry, and an OpenAI-shaped client asking for
`gpt-4o` takes the second.

In passthrough mode the request forwards verbatim to the
upstream's own `/v1/messages` endpoint. The upstream must serve
the Messages API itself for this route: Anthropic's API, or any
Anthropic-compatible gateway. The entry's key rides as
`x-api-key`, and the `anthropic-version` and `anthropic-beta`
headers forward with `2023-06-01` as the default version. A
passthrough entry pointed at an OpenAI-shaped upstream still
serves the OpenAI pair; the Messages route answers that upstream's
own error.

A workspace that wants claude-code on the proxy must export a few envvars by
hand. The base URL derives from the seeded `MSKSWS_BASE_URL`:

```console
export ANTHROPIC_BASE_URL="${MSKSWS_BASE_URL%/v1}"
export ANTHROPIC_AUTH_TOKEN="msks-local-proxy"
```

The token is a placeholder; it exists because claude-code refuses a base URL
with no credential beside it. Set in a shell profile, the pair reaches every
Anthropic SDK client in the workspace, not claude-code alone.

## Subscription claude-code with no stored credentials

The LLM proxy cannot successfully be used to access a subscription Anthropic
account. Instead, you should almost certainly use the normal Anthropic
subscription login process to connect to Claude Code.

But that process does leave Claude credentials on the VM.

If you want to keep Claude credentials off the VM even with an Anthropic
subscription instead of an API key, an operator with a
claude.ai subscription and no API billing can still run
claude-code inside a workspace, with nothing credential-shaped
left on the guest's disk: subscription login with RAM-only
tokens. The tokens live in a tmpfs for the session and vanish
at shutdown.

This is only useful if you use more than one coding agent on the same VM, and
you distrust the other to not steal your Claude credentials. Otherwise, it's
total overkill. It's better to just silo claude into its own workspace.

Subscription authentication is OAuth, and claude-code holds
its tokens wherever it runs (`~/.claude` on Linux). No msks
machinery can hold them for the guest. The proxy and the
placeholder swap both present a static credential, and the
OAuth token rotates on refresh. What the workspace can do is
keep the token off the home volume, so a powered-off workspace
holds nothing.

**The threat this recipe answers.** An agent sharing the
workspace (another coding agent, a script a repository asked
to run) reads the home volume as the same user. A disk-backed
`~/.claude` hands it the refresh token, which works from
anywhere until you notice. The tmpfs narrows the exposure to
sessions where claude-code itself is running. The home volume
never carries the token at all. The full answer for untrusted
agents is a separate workspace: another workspace's VM reads
nothing of this one's, live or at rest.

**Keep `~/.claude` in RAM.** Mount a tmpfs on it before the
first login. Tokens, settings, and session history then live
in memory alone, and a reboot drops them all:

```console
mkdir -p ~/.claude
sudo mount -t tmpfs -o mode=700,uid="$USER" tmpfs ~/.claude
```

A workspace where mounting does not fit runs
`claude auth logout` at the end of each session instead.
Either way the home volume stays clean. A copied workspace or
a home-volume backup carries no credential.

**Log in with the paste-code flow.** `claude auth login`
prints an authorize URL. Open it on your own machine; any
browser works, and the guest is never dialed. Authorize, copy
the code the page shows, and paste it at the guest CLI's
prompt. A reboot drops the tokens (the tmpfs again), and the
next session logs in the same way.

**The flow.** The token exchange dials
`platform.claude.com`. The session dials `api.anthropic.com`.
claude-code's telemetry hosts (statsig, sentry) and its other
non-essential calls dial too; exporting
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` keeps them from
asking at all. A `static` workspace allowlists the two that
matter:

```console
msks egress mode myws static --allow api.anthropic.com \
    --allow platform.claude.com
```

The flags replace the whole list; repeat them to keep entries
the workspace already had. An `interactive` workspace answers
the two prompts instead.

## Integration with the Pi coding agent

Each msks VM ships with the `pi` coding agent installed. Each image bakes
pinned Node and the pinned **pi** coding agent on every account's default
`PATH`: the Debian/Fedora image under `/usr/local`, the NixOS image through its
system profile.

The first-boot VM seed configures the clients: `/etc/profile.d/msks-llm.sh`
exports `MSKSWS_BASE_URL`, built from the DHCP lease's gateway and the daemon's
port. It also exports `MSKSWS_API_KEY`, a placeholder the proxy never reads:
it exists for OpenAI-shaped clients that refuse an empty key.

A pi extension consumes these envvars so that `/models` works.

The pi extension is named `llm-models.ts`. It exists in `/etc/skel` and root's
home. Every account the identity seed provisions copies the skeleton, so each
login user ends up with the extension in `~/.pi/agent/extensions/`, where pi
discovers it. At every pi startup the extension makes one fetch of
`${MSKSWS_BASE_URL}/models`, aborted at 1.5 seconds, and registers the catalog
under the `msks` provider, so `/model` always shows the daemon's live model
list.

## SIGHUP

A restart or `SIGHUP` of msksd is required in order to register changes to the
LLM proxy settings.
