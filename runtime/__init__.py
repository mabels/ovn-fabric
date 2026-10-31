# runtime — the event runtime (ADR 0007): messages, peers, transports,
# services. Its own message parts are runtime/schema/ -> runtime/generated.py.
# It never imports application code (fabric, deployer, ladops, protocol).

__version__ = "0.1.0"

# The wire protocol's version, answered by VersionReq. Bumped on an
# incompatible change of the message format.
PROTOCOL = 1
