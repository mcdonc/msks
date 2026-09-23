"""The root-and-egress real-VM suite: every test here boots a VM
through the daemon's own net stack (taps, DHCP, DNS, nftables), so
they need ``MSKSD_TEST_EGRESS=1``, root, and a host whose FORWARD
chain the daemon's tables own — the workflow sets all three.

The unprivileged boots (no root, no host network) live beside this
as ``test_smoke.core``; both suites share the harness in
``test_smoke/__init__.py``.
"""
