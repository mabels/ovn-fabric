"""The host's OS target: discovery, the resolved translation input, and the
package translation itself.

The runtime runs on the host itself, so discovery is local: `/etc/os-release`
names the distro and its release, and the device tree names a Raspberry Pi.
A declared distro wins; a missing os, version, or platform comes from
discovery. The resolved `HostTarget` carries the derived package manager and
the effective overrides — the IR's `dependencyOverrides` with the platform's
defaults merged into the same RHS arrays.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence

from protocol import generated as pt

Reader = Callable[[str], str | None]

OS_RELEASE_PATHS = ("/etc/os-release", "/usr/lib/os-release")
DEVICE_TREE_MODEL_PATHS = (
    "/proc/device-tree/model",
    "/sys/firmware/devicetree/base/model",
)
DEVICE_TREE_COMPATIBLE_PATHS = (
    "/proc/device-tree/compatible",
    "/sys/firmware/devicetree/base/compatible",
)

_DISTROS = {
    "debian": pt.Distro.debian,
    "fedora": pt.Distro.fedora,
    "raspbian": pt.Distro.debian,
    "rocky": pt.Distro.rocky,
    "ubuntu": pt.Distro.ubuntu,
}

MANAGER_BY_DISTRO: dict[pt.Distro, str] = {
    pt.Distro.ubuntu: "apt",
    pt.Distro.debian: "apt",
    pt.Distro.fedora: "dnf",
    pt.Distro.rocky: "dnf",
}

DISTRO_DEPS: dict[pt.Distro, dict[str, tuple[str, ...]]] = {
    pt.Distro.ubuntu: {
        "arp": ("net-tools",),
        "dhclient": ("isc-dhcp-client",),
        "dhcpcd": ("dhcpcd",),
        "docker": ("docker.io",),
        "ip": ("iproute2",),
        "iptables": ("iptables", "nftables"),
        "nftables": ("nftables",),
        "ovn": ("ovn-host",),
        "ovn-central": ("ovn-central",),
        "ovs": ("openvswitch-switch",),
        "ping": ("iputils-ping",),
        "python": ("python-is-python3",),
        "traceroute": ("traceroute",),
        "wireguard": ("wireguard-tools",),
    },
    pt.Distro.debian: {
        "arp": ("net-tools",),
        "dhclient": ("isc-dhcp-client",),
        "dhcpcd": ("dhcpcd",),
        "docker": ("docker.io",),
        "ip": ("iproute2",),
        "iptables": ("iptables", "nftables"),
        "nftables": ("nftables",),
        "ovn": ("ovn-host",),
        "ovn-central": ("ovn-central",),
        "ovs": ("openvswitch-switch",),
        "ping": ("iputils-ping",),
        "python": ("python-is-python3",),
        "traceroute": ("traceroute",),
        "wireguard": ("wireguard-tools",),
    },
    pt.Distro.fedora: {
        "arp": ("net-tools",),
        "dhclient": ("dhcp-client",),
        "dhcpcd": ("dhcpcd",),
        "docker": ("moby-engine",),
        "ip": ("iproute",),
        "iptables": ("iptables-nft", "nftables"),
        "nftables": ("nftables",),
        "ovn": ("ovn-host",),
        "ovn-central": ("ovn-central",),
        "ovs": ("openvswitch",),
        "ping": ("iputils",),
        "python": ("python-unversioned-command",),
        "traceroute": ("traceroute",),
        "wireguard": ("wireguard-tools",),
    },
    pt.Distro.rocky: {
        "arp": ("net-tools",),
        "dhclient": ("dhcp-client",),
        "dhcpcd": ("dhcpcd",),
        "docker": ("docker-ce",),
        "ip": ("iproute",),
        "iptables": ("iptables-nft", "nftables"),
        "nftables": ("nftables",),
        "ovn": ("ovn25.09-host",),
        "ovn-central": ("ovn25.09-central",),
        "ovs": ("openvswitch3.5",),
        "ping": ("iputils",),
        "python": ("python-unversioned-command",),
        "traceroute": ("traceroute",),
        "wireguard": ("wireguard-tools",),
    },
}

ABSTRACT_DEPS = tuple(sorted({name for deps in DISTRO_DEPS.values() for name in deps}))

_NFV_REPO = pt.DepRepo(
    kind="dep.repo",
    id="centos-nfv-openvswitch",
    baseurl=(
        "https://mirror.stream.centos.org/SIGs/$releasever-stream/nfv/" "$basearch/openvswitch-2/"
    ),
    gpgkey="https://www.centos.org/keys/RPM-GPG-KEY-CentOS-SIG-NFV",
)

_DOCKER_CE_REPO = pt.DepRepo(
    kind="dep.repo",
    id="docker-ce-stable",
    baseurl=("https://download.docker.com/linux/centos/$releasever/$basearch/stable"),
    gpgkey="https://download.docker.com/linux/centos/gpg",
)

DISTRO_REPOS: dict[pt.Distro, dict[str, tuple[pt.DepRepo, ...]]] = {
    pt.Distro.rocky: {
        "ovn": (_NFV_REPO,),
        "ovs": (_NFV_REPO,),
        "docker": (_DOCKER_CE_REPO,),
    },
}

PLATFORM_DEPS: dict[
    tuple[pt.Distro, pt.HostPlatform],
    dict[str, tuple[pt.DependencyEntry, ...]],
] = {
    (pt.Distro.ubuntu, pt.HostPlatform.rpi): {
        "platform": (
            pt.DepPkg(kind="dep.pkg", name="raspi-config"),
            pt.DepPkg(kind="dep.pkg", name="linux-firmware"),
        ),
    },
    # Debian / Raspberry Pi OS has no `linux-firmware` (Ubuntu's name); the
    # Pi's GPU firmware and bootloaders ship as `raspi-firmware`.
    (pt.Distro.debian, pt.HostPlatform.rpi): {
        "platform": (
            pt.DepPkg(kind="dep.pkg", name="raspi-config"),
            pt.DepPkg(kind="dep.pkg", name="raspi-firmware"),
        ),
    },
}


class HostOsError(ValueError):
    """The host's OS cannot be discovered reliably."""


@dataclasses.dataclass(frozen=True)
class HostTarget:
    """What the lower layers need: the resolved OS, its package manager, and
    the effective overrides (platform defaults merged with the IR's)."""

    os: pt.HostOs
    package_manager: str
    dependencies: Mapping[str, tuple[pt.DependencyEntry, ...]]

    def system_deps(self, name: str) -> list[str] | None:
        """The concrete packages for an abstract dependency, or None when
        neither the distro table nor an override knows it. Repositories are
        excluded: they are host-global and have their own phase."""
        entries = self.dependencies.get(name)
        if entries is not None:
            packages = [
                _render_entry(entry, self.package_manager)
                for entry in entries
                if not isinstance(entry, pt.DepRepo)
            ]
            if packages:
                return packages
        base = DISTRO_DEPS.get(self.os.distro, {}).get(name)
        return None if base is None else list(base)

    def repositories(self) -> tuple[pt.DepRepo, ...]:
        """Every repository named by the effective dependencies, by id."""
        repos: dict[str, pt.DepRepo] = {}
        for entries in self.dependencies.values():
            for entry in entries:
                if isinstance(entry, pt.DepRepo) and entry.id not in repos:
                    repos[entry.id] = entry
        return tuple(repos.values())


def resolve_host_os(
    declared: pt.HostOs | None,
    overrides: Mapping[str, Sequence[pt.DependencyEntry]] | None = None,
    abstract_names: Sequence[str] = (),
) -> HostTarget:
    """The declared OS, else the discovered one, plus manager and overrides.

    A declared distro is authoritative. Missing version/platform are filled
    from discovery; a version is only taken when discovery sees the same
    distro (so a declared Rocky host never inherits an Ubuntu version)."""
    os = _fill_from_discovery(declared) if declared is not None else discover_host_os()
    platform = os.platform if os.platform is not None else pt.HostPlatform.generic
    resolved = pt.HostOs(distro=os.distro, version=os.version, platform=platform)
    manager = MANAGER_BY_DISTRO.get(resolved.distro)
    if manager is None:
        raise HostOsError(f"unsupported distro {resolved.distro!r}")
    defaults = _distro_entries(resolved.distro, platform, abstract_names)
    return HostTarget(
        os=resolved,
        package_manager=manager,
        dependencies=_merge(defaults, overrides or {}),
    )


def _distro_entries(
    distro: pt.Distro,
    platform: pt.HostPlatform,
    names: Sequence[str],
) -> dict[str, tuple[pt.DependencyEntry, ...]]:
    entries: dict[str, tuple[pt.DependencyEntry, ...]] = dict(
        PLATFORM_DEPS.get((distro, platform), {})
    )
    for name in names:
        repos = DISTRO_REPOS.get(distro, {}).get(name)
        if repos:
            entries[name] = (*entries.get(name, ()), *repos)
    return entries


def _fill_from_discovery(declared: pt.HostOs) -> pt.HostOs:
    if declared.version is not None and declared.platform is not None:
        return declared
    try:
        discovered = discover_host_os()
    except HostOsError:
        return declared
    version = declared.version
    if version is None and discovered.distro is declared.distro:
        version = discovered.version
    platform = declared.platform if declared.platform is not None else discovered.platform
    return pt.HostOs(distro=declared.distro, version=version, platform=platform)


def fallback_system_deps(name: str) -> dict[str, list[str]]:
    """The manager-keyed view the CLI/docker paths still use: apt is Ubuntu,
    dnf is Fedora. IR apply supplies explicit packages instead."""
    deps: dict[str, list[str]] = {}
    for manager, distro in (("apt", pt.Distro.ubuntu), ("dnf", pt.Distro.fedora)):
        packages = DISTRO_DEPS[distro].get(name)
        if packages is not None:
            deps[manager] = list(packages)
    return deps


def discover_host_os(read: Reader | None = None) -> pt.HostOs:
    """The running host's distro, release, and platform."""
    reader: Reader = read if read is not None else _read_text
    fields = _os_release(reader)
    distro = _distro(fields)
    version = fields.get("VERSION_ID") or None
    return pt.HostOs(
        distro=distro,
        version=version,
        platform=_platform(reader),
    )


def _merge(
    defaults: Mapping[str, Sequence[pt.DependencyEntry]],
    overrides: Mapping[str, Sequence[pt.DependencyEntry]],
) -> dict[str, tuple[pt.DependencyEntry, ...]]:
    names = list(defaults) + [name for name in overrides if name not in defaults]
    return {name: _dedupe((*overrides.get(name, ()), *defaults.get(name, ()))) for name in names}


def _dedupe(
    entries: Sequence[pt.DependencyEntry],
) -> tuple[pt.DependencyEntry, ...]:
    seen: set[tuple[str, str]] = set()
    out: list[pt.DependencyEntry] = []
    for entry in entries:
        key = _entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return tuple(out)


def _entry_key(entry: pt.DependencyEntry) -> tuple[str, str]:
    if isinstance(entry, pt.DepPkg):
        return (entry.kind, entry.name)
    if isinstance(entry, pt.DepPkgFile):
        return (entry.kind, entry.path)
    return (entry.kind, entry.id)


def _render_entry(entry: pt.DependencyEntry, manager: str) -> str:
    if isinstance(entry, pt.DepPkg):
        if entry.version is None:
            return entry.name
        return (
            f"{entry.name}={entry.version}" if manager == "apt" else f"{entry.name}-{entry.version}"
        )
    if isinstance(entry, pt.DepPkgFile):
        return entry.path
    raise HostOsError(f"repository {entry.id!r} needs a repository phase")


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _os_release(read: Reader) -> dict[str, str]:
    for path in OS_RELEASE_PATHS:
        text = read(path)
        if text is None:
            continue
        fields = _parse_os_release(text)
        if fields:
            return fields
    raise HostOsError("no usable /etc/os-release or /usr/lib/os-release")


def _parse_os_release(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        fields[key.strip()] = _unquote(value.strip())
    return fields


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _distro(fields: dict[str, str]) -> pt.Distro:
    name = fields.get("ID", "").strip().lower()
    distro = _DISTROS.get(name)
    if distro is None:
        raise HostOsError(f"unsupported distro {name!r} in os-release")
    return distro


def _platform(read: Reader) -> pt.HostPlatform:
    for path in DEVICE_TREE_MODEL_PATHS:
        model = read(path)
        if model is not None and "raspberry pi" in _text(model).lower():
            return pt.HostPlatform.rpi
    for path in DEVICE_TREE_COMPATIBLE_PATHS:
        compatible = read(path)
        if compatible is not None and "raspberrypi" in _text(compatible).lower():
            return pt.HostPlatform.rpi
    return pt.HostPlatform.generic


def _text(value: str) -> str:
    return value.replace("\x00", " ").strip()
