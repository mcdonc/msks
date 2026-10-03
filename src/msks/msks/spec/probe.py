"""The workspace probe's shared vocabulary (#424).

The probe — the emulated external HTTPS service a workspace
operator verifies secret interception with — is described by one
set of well-known facts, shared by every half that must agree on
them: the resolver that answers the name locally
(``msks.net.dns``), the interceptor that serves the endpoint
(``msks.interceptor.probe``), the secret routes that seed and serve
its placeholder (``msks.server.api.secrets``). The facts are
vocabulary, not configuration: every deployment serves them, and
an operator's placeholder allowlist spells the host exactly.

The credential is fixed by design: username ``msks``, password
``12345`` — a probe credential shared by every deployment, not a
secret (the endpoint's whole power is the ``ok`` page it already
gave). The minted placeholder secret is the base64 of the whole
credential, so the interceptor's byte-level swap of the raw Basic
blob produces a well-formed header. Code scanning reads a
credential-shaped literal either way, so each line naming the
value carries the inline suppression for the
hardcoded-credentials query, and this module says why.
"""

import base64

#: The well-known probe host (#424). The per-tap resolver answers
#: it with the tap's own address, and the interceptor answers
#: requests aimed at it locally — no upstream exists.
PROBE_HOST = "probe.msks"

#: The port the service answers on: the guest dials HTTPS (443),
#: the nft redirect preserves the original destination's port, and
#: the interceptor's upstream dial lands here — so 443 is the only
#: port the service can serve.
PROBE_PORT = 443

#: The seeded probe placeholder's label (#424): the daemon-wide row
#: the daemon mints at startup, so the probe works with zero
#: operator minting.
PROBE_NAME = "probe"

# The fixed probe credential (#424): every deployment serves the
# same pair, because the endpoint's response is the same
# everywhere — the value gates nothing an attacker wants.
# lgtm[py/hardcoded-credentials]
PROBE_USERNAME = "msks"
# lgtm[py/hardcoded-credentials]
PROBE_PASSWORD = "12345"

#: What the seeded placeholder carries as its secret (#424): the
#: base64 of the whole credential — the value the swap writes into
#: the raw Basic blob, and the value the service then validates.
PROBE_SECRET_B64 = base64.b64encode(
    f"{PROBE_USERNAME}:{PROBE_PASSWORD}".encode()
).decode()
