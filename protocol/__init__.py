# protocol/__init__.py — the protocol package's public surface: the
# generated dataclasses (protocol/generated.py, from src/protocol.ts's
# ArkType schema) plus readable aliases for the KernelApp discriminated
# union, whose members datamodel-codegen names from the inline anyOf
# (Apps/Apps1/Apps2/Apps3, in arbitrary order) — these mirror the TS
# names (src/protocol.ts's DhcpClientApp/WireguardApp/ZerotierApp/DockerApp).
from . import generated

AppDhcpClient = generated.Apps3
AppWireguard = generated.Apps
AppZerotier = generated.Apps2
AppDocker = generated.Apps1

# The KernelApp union itself — what KernelRouterData.apps is a list of.
KernelApp = generated.Apps | generated.Apps1 | generated.Apps2 | generated.Apps3
