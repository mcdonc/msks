import importlib.metadata

import msks


def test_package_imports() -> None:
    assert msks.__version__


def test_installed_version_matches_package() -> None:
    assert importlib.metadata.version("msks") == msks.__version__
