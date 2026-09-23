"""The unprivileged real-VM suite: boots a workspace through
cloud-hypervisor against whichever guest the ``MSKSD_TEST_*``
variables point at (the conftest discovers the built Debian assets;
the CI workflow exports the NixOS ones).

These tests need /dev/kvm but not root and not the host's network —
they self-skip (``needs_local``) when the assets or the device are
missing. The root-and-egress suite lives beside this as
``test_smoke.egress``.
"""
