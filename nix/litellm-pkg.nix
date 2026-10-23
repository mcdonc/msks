# The LLM router library for the msksd closure (#607).
#
# nixpkgs (the pinned rev) carries 1.86.0, below the >=1.106.2
# floor pyproject declares: msks's own runtime deps check refuses
# the wheel against it. Built here from the PyPI wheel uv.lock
# pins, against nixpkgs' own aiohttp/openai/pydantic/… so the
# daemon closure stays on the platform's package set wherever the
# floors allow it.
#
# The wheel carries one compiled extension (litellm/rust_bridge/
# _native.abi3.so, the rust bridge nixpkgs' own expression builds
# with maturin) — it links glibc alone, so autoPatchelfHook lands
# it. x86_64 only, like the flake that consumes it (system is
# pinned there).
#
# Three floors the pinned nixpkgs sits below — aiohttp (3.13.5
# vs >=3.14.2), pydantic-settings (2.12.0 vs >=2.14.1), boto3
# (1.42.31 vs >=1.43.1) — relax here, on the precedent of
# nixpkgs' own litellm expression (its pythonRelaxDeps relaxes
# aiohttp, click, importlib-metadata, jsonschema, openai, and
# python-dotenv). What each carries on the msks surface:
#
# - aiohttp is litellm's DEFAULT async transport — but
#   msks.msks.llm's build_litellm_router sets
#   litellm.disable_aiohttp_transport, so the daemon's
#   completions ride plain httpx and the closure's below-floor
#   aiohttp never carries a request (a transport it did carry
#   would matter: aiohttp <= 3.14.2 has CVE-2026-69244, a
#   malformed-response OOB heap read, and a configured upstream
#   is exactly the server a workspace's completions hit).
# - pydantic-settings loads at `from litellm import Router` (the
#   OTEL integration's config models) — verified working on
#   2.12.0; it never takes a proxy-server settings tree here.
# - boto3 (bedrock) is never imported on the msks path.
#
# Bump workflow: bump `version` and swap `src.url`/`hash` to the
# new wheel uv.lock pins (nix prints the real hash on mismatch),
# then diff the wheel's METADATA Requires-Dist against
# propagatedBuildInputs and pythonRelaxDeps — a base entry the new
# wheel adds needs a hand-edit here, or the runtime check fails
# only at package build time (#203's blast radius). The relax list
# is calibrated to the PINNED nixpkgs rev: nix/module.nix builds
# against the consuming host's nixpkgs, so a host set that serves
# any bound outside litellm's ranges (a newer filelock than the
# <4.0 ceiling, say) fails the runtime check until the list grows.
{
  lib,
  autoPatchelfHook,
  buildPythonPackage,
  fetchurl,

  # Runtime dependencies, mirroring litellm 1.106.2's wheel
  # metadata (base set — the proxy/cli/… extras stay off):
  aiohttp,
  boto3,
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
  version = "1.106.2";
  format = "wheel";

  # The exact wheel uv.lock pins (hash and all), fetched directly.
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/6b/30/4a38c22181ef7169b6c044b8294f2e5b06236aeeccfbe57c2f0e85a51bfe/litellm-1.106.2-cp310-abi3-manylinux_2_28_x86_64.whl";
    hash = "sha256-yxoqHlWkqDUK4hZ1JdwNBvWbB4zWhfs+D1QZ8C2lQg4=";
  };

  propagatedBuildInputs = [
    aiohttp
    boto3
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

  # The pinned nixpkgs serves aiohttp/pydantic-settings/boto3
  # below litellm's floors and importlib-metadata above its
  # ceiling (see the header comment); the bounds drop, the
  # presence stays. aiohttp's bound stays dropped only because
  # msks disables that transport — if llm.py ever lets litellm
  # use it again, pin aiohttp at litellm's floor instead.
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
