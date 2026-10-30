# rt — the runtime (placeholder name, ADR 0007): the job tree and the bus.
# It never imports ovn-fabric code (deployer, ladops, protocol).

__version__ = "0.1.0"

# The wire protocol's version, answered by VersionReq. Bumped on an
# incompatible change of the message format.
PROTOCOL = 1
