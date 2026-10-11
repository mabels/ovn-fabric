# protocol/__init__.py — the protocol package's public surface: the
# generated dataclasses (protocol/generated.py, from src/protocol.ts's
# ArkType schema) plus the names the deployer has always used for the
# kernel apps. Every generated class carries the name it has in
# src/protocol.ts (buildJsonSchema()'s named $refs), so these are plain
# aliases — no positional Apps/Apps1/... names to guess at anymore.
from . import generated

AppDhcpClient = generated.DhcpClientApp
AppWireguard = generated.WireguardApp
AppZerotier = generated.ZerotierApp
AppDocker = generated.DockerApp

# The KernelApp union itself — what KernelRouterData.apps is a list of.
KernelApp = generated.KernelApp
