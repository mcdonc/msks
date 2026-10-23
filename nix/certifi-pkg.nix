# The platform CA bundle for the msksd closure (#424, #606).
#
# nixpkgs (nixos-26.05) carries 2026.01.04, below the >=2026.10.23
# floor pyproject declares: that release drops the distrusted
# emSign root certificates (GHSA-6q48-6c7w-9gw2) from the Mozilla
# root store the interceptor's upstream verification
# (ssl_verify_upstream_trusted_ca) and the image download's
# fallback context both load. Built here from the PyPI wheel —
# pure python, no compiled extensions, no dependencies — because
# certifi is a transitive dependency in the closure (httpx and
# mitmproxy propagate it), so a callPackage argument alone would
# collide with the packages that still carry nixpkgs' own: the
# overlay (nix/certifi-overlay.nix) replaces it across the whole
# python package set, every dependent resolves the one build, and
# catch-conflicts stays clean.
#
# Bump workflow: bump `version` and swap `src.url`/`hash` to the
# new wheel uv.lock pins (nix prints the real hash on mismatch).
# test_pkg_mirror.py fails when pyproject's floor moves past this
# pin.
{
  lib,
  buildPythonPackage,
  fetchurl,
}:

buildPythonPackage rec {
  pname = "certifi";
  version = "2026.10.23";
  format = "wheel";

  # The exact wheel uv.lock pins (hash and all), fetched directly.
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/32/d1/a3652fe2c19134d0d15922177821d863cbd7e842d53a21bbe7e8e3a1b5ac/certifi-2026.10.23-py3-none-any.whl";
    hash = "sha256-6bVN525TC7bPDM0DZ4cQOstY/UK6kw6ov5SN26ovYdE=";
  };

  pythonImportsCheck = [ "certifi" ];

  meta = {
    description = "Mozilla's root certificates, the platform CA bundle msks' TLS verification loads";
    homepage = "https://github.com/certifi/python-certifi";
    license = lib.licenses.mpl20;
  };
}
