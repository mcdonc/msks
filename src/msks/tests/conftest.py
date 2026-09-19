import os

from msks import guestassets

# Use the sysmon coverage engine — on Python 3.14 sys.monitoring measures
# branches and tracks greenlet-executed code natively, which is why CI
# runs with `-n auto` (a single-process run under-counts and shows a
# false low total; see AGENTS.md). No `concurrency` option anywhere:
# sysmon does not support it.
os.environ.setdefault("COVERAGE_CORE", "sysmon")

# Self-provisioned smoke-test assets (#5): when the msks-build-guest
# script has built the guest assets (.devenv/state/guest by default;
# MSKS_GUEST_DIR relocates it) and /dev/kvm is usable, point the
# MSKSD_TEST_* variables at the built artifacts. Explicitly exported
# variables win; when nothing was built the smoke tests keep
# skipping themselves.
for _name, _value in guestassets.smoke_env_defaults(
    guestassets.load_guest_assets(),
).items():
    os.environ.setdefault(_name, _value)
_image = guestassets.load_runner_image()
if _image is not None:
    os.environ.setdefault(guestassets.RUNNER_IMAGE_ENV, _image)
