# The dhcpcd runtime image for the kernel-netns dhcp-client that runs
# INSIDE a docker container owning the real interface (2026-08-31).
#
# dhcpcd must be in the image (not apk-add'd at container start): the
# container runs `--network none` and owns the interfaces moved in from
# the router netns, so it has no network path to a package repo. Build
# once on the host, then reference `ovn-fabric-dhcpcd`:
#
#   docker build -t ovn-fabric-dhcpcd containers/dhcpcd
#
# The container's own network is irrelevant (it's always `--network
# none`); only the dhcpcd binary and its runtime deps are needed.
FROM alpine:latest
# Only dhcpcd is baked in: the interface/static-IP/masq config is applied
# from the host via `ip netns exec <container>` (host ip/iptables targeting
# the container netns), not inside the container.
RUN apk add --no-cache dhcpcd
