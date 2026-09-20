# The netfilterqueue python binding for the appliance closure (#69).
#
# nixpkgs does not carry it, so it is built here from the PyPI sdist
# against nixpkgs' libnetfilter_queue + libnfnetlink — the same pair
# the devenv shell points uv's wheel build at. Egress consent is the
# normal posture for workspaces, so the binding rides the daemon
# closure like every other dependency.
{
  lib,
  buildPythonPackage,
  fetchPypi,
  cython,
  setuptools,
  wheel,
  libnfnetlink,
  libnetfilter_queue,
}:

buildPythonPackage rec {
  pname = "netfilterqueue";
  version = "1.1.0";
  pyproject = true;

  src = fetchPypi {
    pname = "NetfilterQueue"; # the sdist filename's case
    inherit version;
    hash = "sha256-4w7/mZMlYX9UvZWz2NM1MhTMd7XjGI+Q7DpvYoiIjXI=";
  };

  # The sdist's pyproject declares a cython build requirement (it
  # regenerates the C from the .pyx even though the C ships too).
  build-system = [
    setuptools
    wheel
    cython
  ];

  buildInputs = [
    libnfnetlink
    libnetfilter_queue
  ];

  pythonImportsCheck = [ "netfilterqueue" ];

  meta = {
    description = "Python binding for libnetfilter_queue";
    homepage = "https://github.com/oremanj/python-netfilterqueue";
    license = lib.licenses.mit;
  };
}
