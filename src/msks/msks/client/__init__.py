"""The msks client package: speaks the daemon's API from a dev box.

Server-side env is ``MSKSD_*``; client-side env is ``MSKSC_*`` (the
client/server split keeps the namespaces apart on purpose), and the
client's YAML file — ``~/.config/msks/msks.yaml`` (#314) — carries
the same settings durably beneath the variables.
"""
