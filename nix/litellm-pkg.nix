# The LLM router library for the msksd closure (#607).
#
# Built here from the exact PyPI wheel uv.lock pins (hash and
# all), against nixpkgs' own aiohttp/openai/pydantic/… so the
# daemon closure stays on the platform's package set wherever
# the ranges allow it. The pinned nixpkgs carries its own
# litellm (1.108.0, below this pin); retiring the hand pin for
# the channel's copy is a deliberate release decision, not a
# pin-side effect — at this raw-master rev the channel's
# litellm closure builds git locally (the installcheck suite
# fails in-sandbox; a channel-cut rev fetches those paths from
# the binary cache instead).
#
# #1062 is the bump's reason: the unstable pin's filelock 4.2.4,
# openai 3.14.1, and huggingface-hub 2.4.0 sat outside 1.106.2's
# ceilings (filelock<4.0, openai<3.0.0, huggingface-hub<2.0) and
# pythonRuntimeDepsCheckHook refused the build. 1.110.2 widened
# those ranges — filelock<5, openai<4.0.0, huggingface-hub<3.0 —
# so the pinned set fits without a relax for any of the three.
# The wheel also grew two base deps (anyio, certifi), mirrored
# below.
#
# The wheel carries one compiled extension (litellm/rust_bridge/
# _native.abi3.so, the rust bridge nixpkgs' own expression
# builds with maturin) — it links glibc alone, so autoPatchelfHook
# lands it. x86_64 only, like the flake that consumes it (system
# is pinned there).
#
# The relax list, with what each carries on the msks surface:
#
# - boto3 (bedrock) is never imported on the msks path — and the
#   pinned set's 1.43.79 sits below the >=1.43.117 floor.
# - importlib-metadata: the pinned set's 9.0.0 sits at the <9.0
#   ceiling; litellm reads it for entry points, nothing more.
# - aiohttp and pydantic-settings sit INSIDE the pinned set's
#   ranges now (3.14.5 over the >=3.14.2 floor, 2.14.2 over the
#   >=2.14.1 floor) — the relaxes stay for nix/module.nix, which
#   builds this package against a consuming host's nixpkgs, where
#   an older set can sit below either floor. aiohttp is litellm's
#   DEFAULT async transport — but msks.llm's build_litellm_router
#   sets litellm.disable_aiohttp_transport, so the daemon's
#   completions ride plain httpx and a below-floor aiohttp never
#   carries a request (a transport it did carry would matter:
#   aiohttp <= 3.14.2 has CVE-2026-69244, a malformed-response
#   OOB heap read, and a configured upstream is exactly the
#   server a workspace's completions hit). pydantic-settings
#   loads at `from litellm import Router` (the OTEL integration's
#   config models) — verified working on 2.12.0; it never takes
#   a proxy-server settings tree here.
#
# The precedent is nixpkgs' own litellm expression (its
# pythonRelaxDeps relaxes aiohttp, boto3, certifi, click,
# filelock, huggingface-hub, importlib-metadata, jsonschema,
# openai, pydantic, and python-dotenv).
#
# Bump workflow: bump `version` and swap `src.url`/`hash` to the
# new wheel uv.lock pins (nix prints the real hash on mismatch),
# then diff the wheel's METADATA Requires-Dist against
# propagatedBuildInputs and pythonRelaxDeps — a base entry the
# new wheel adds needs a hand-edit here, and a range the
# building set sits outside needs a relax with its msks-surface
# justification (#203's blast radius: the check fails only at
# package build time). The relax list is calibrated to the PINNED
# nixpkgs rev plus the module path's host sets: a set that serves
# a bound outside litellm's ranges (a newer openai than the
# <4.0.0 ceiling, say) fails the runtime check until the list
# grows.
{
  lib,
  autoPatchelfHook,
  buildPythonPackage,
  fetchurl,

  # Runtime dependencies, mirroring litellm 1.110.2's wheel
  # metadata (base set — the proxy/cli/… extras stay off):
  aiohttp,
  anyio,
  boto3,
  certifi,
  click,
  fastuuid,
  filelock,
  # httpx[http2]: the extra's payload listed directly so the
  # runtime check resolves it either way it looks it up.
  h2,
  httpx,
  huggingface-hub,
  importlib-metadata,
  jinja2,
  jsonschema,
  openai,
  packaging,
  pydantic,
  pydantic-settings,
  python-dotenv,
  pyyaml,
  tiktoken,
  tokenizers,
  typing-extensions,
}:

buildPythonPackage {
  pname = "litellm";
  version = "1.110.2";
  format = "wheel";

  # The exact wheel uv.lock pins (hash and all), fetched directly.
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/d3/51/825f430f07bf864ade5f58462816437bbfaa22c229f5bb474073352bffc4/litellm-1.110.2-cp310-abi3-manylinux_2_28_x86_64.whl";
    hash = "sha256-nZTRx9Uktv0QwU+cjGxPCsMXBfmWSsWdz9gzfARtiRc=";
  };

  propagatedBuildInputs = [
    aiohttp
    anyio
    boto3
    certifi
    click
    fastuuid
    filelock
    h2
    httpx
    huggingface-hub
    importlib-metadata
    jinja2
    jsonschema
    openai
    packaging
    pydantic
    pydantic-settings
    python-dotenv
    pyyaml
    tiktoken
    tokenizers
    typing-extensions
  ];

  # The pinned nixpkgs serves boto3 below litellm's floor and
  # importlib-metadata above its ceiling (see the header
  # comment); the bounds drop, the presence stays. aiohttp's and
  # pydantic-settings's bounds stay dropped only for the module
  # path's older host sets — the pinned set clears both floors —
  # and aiohttp's bound stays dropped only because msks disables
  # that transport: if llm.py ever lets litellm use it again, pin
  # aiohttp at the CVE-2026-69244 fix (3.14.3, above litellm's
  # own >=3.14.2 floor) or later instead.
  pythonRelaxDeps = [
    "aiohttp"
    "boto3"
    "importlib-metadata"
    "pydantic-settings"
  ];

  pythonImportsCheck = [ "litellm" ];

  meta = {
    description = "LLM router used by msks' multi-provider proxy mode";
    homepage = "https://github.com/BerriAI/litellm";
    license = lib.licenses.mit;
  };
}
