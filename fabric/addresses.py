"""The address metadata of an IR (ADR 0011 §3b) — built on the runtime side,
never in TypeScript, never written back into the IR.

The IR holds no number for an address the topology does not pin: a
`LogicalIP` is a name (`owner`) on a collision domain's net (an `ip.net`
node), with a `host` only when pinned; an `AddrRef` names one family of
another carrier's address. `Addresses.resolve(nodes)` places every name and
answers what services ask:

    addrs.list(logical_ips)   ["172.21.0.3/24", "2600:…::3/112"]  (with prefix)
    addrs.one(logical_ip)     "10.200.7.2/24"
    addrs.ref(ref)            "172.21.0.3"                         (bare)
    addrs.net(net_id, fam)    "172.21.0.0/24"
    addrs.mac(ips, name)      "00:00:ac:15:00:03"

Allocation (ADR 0011 §3, §4a, §5) is a pure function, `allocate(request)`:
per collision domain, pinned numbers first, then the live numbers of names
still in the IR, then new names — sorted — by the domain's strategy. When
every family of the domain uses the same strategy, a name gets one host
number for all its families (`.10` and `::a`). `create` passes an empty live
view; `run` (later) the live one.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from ip_address import IPAddress

from protocol import generated as pt

FAMILIES = ("ipv4", "ipv6")


class AddressError(ValueError):
    """An address the IR asks for cannot be placed or found."""


def _parse(value: str, where: str) -> IPAddress:
    parsed = IPAddress.parse(value)
    if parsed is None:
        raise AddressError(f"{where}: not an address: {value!r}")
    return parsed


def _family(addr: IPAddress) -> str:
    return "ipv4" if addr.is_ipv4() else "ipv6"


def _usable(prefix: IPAddress) -> range:
    """The host numbers a name may get: IPv4 without network and broadcast
    (a /31 both, a /32 its one address, as the kernel treats them); IPv6
    without host 0, the subnet-router anycast."""
    size = int(prefix.size())
    if prefix.is_ipv4():
        if prefix.prefix.num >= 31:
            return range(0, size)
        return range(1, size - 1)
    if prefix.prefix.num == 128:
        return range(0, 1)
    return range(1, size)


def _host_number(prefix: IPAddress, ref: int | str) -> int:
    """A pinned HostRef as a number: `first` / `last` per family."""
    if ref == "first":
        return int(prefix.first().host_number())
    if ref == "last":
        return int(prefix.last().host_number())
    if not isinstance(ref, int):
        raise AddressError(f"{prefix.to_string()}: bad host {ref!r}")
    return ref


# -- the allocator: request in, assignment out (ADR 0011 §4a) ---------------


@dataclasses.dataclass(frozen=True)
class AllocationRequest:
    """One collision domain. Plain data, so the same call can later go to a
    central allocator unchanged."""

    domain: str
    prefixes: Mapping[str, str]  # family -> "172.21.0.0/24"
    strategy: Mapping[str, str]  # family -> "low-high" | "high-low" | "random"
    pinned: Mapping[str, Mapping[str, int]]  # owner -> family -> number
    names: Sequence[tuple[str, tuple[str, ...]]]  # (owner, families) to place
    live: Mapping[str, Mapping[str, int]] = dataclasses.field(default_factory=dict)
    held: Mapping[str, Sequence[int]] = dataclasses.field(default_factory=dict)  # family -> taken
    # What an earlier run placed (the allocator state file, ADR 0012 §6):
    # owner -> family -> number, kept unless something holds it now.
    remembered: Mapping[str, Mapping[str, int]] = dataclasses.field(default_factory=dict)


def allocate(request: AllocationRequest) -> dict[str, dict[str, int]]:
    """owner -> family -> host number, for every pinned, live and new name.
    Deterministic: the same request gives the same answer on any host."""
    prefixes = {f: _parse(p, request.domain) for f, p in request.prefixes.items()}
    taken: dict[str, set[int]] = {f: set(request.held.get(f, ())) for f in prefixes}
    out: dict[str, dict[str, int]] = {}

    def claim(owner: str, numbers: Mapping[str, int], why: str) -> None:
        for family, n in numbers.items():
            if family not in prefixes:
                raise AddressError(f"{request.domain}: {owner} has no {family} net")
            if n not in _usable(prefixes[family]) and not (
                why == "pinned" and n in range(int(prefixes[family].size()))
            ):
                raise AddressError(
                    f"{request.domain}: {why} host {n} of {owner} is outside "
                    f"{prefixes[family].to_string()}"
                )
            taken[family].add(n)
        out[owner] = dict(numbers)

    # 1. pinned — two names on one pinned number are the topology's error
    seen: dict[tuple[str, int], str] = {}
    for owner, numbers in sorted(request.pinned.items()):
        for family, n in numbers.items():
            other = seen.get((family, n))
            if other is not None:
                raise AddressError(
                    f"{request.domain}: {family} host {n} is pinned by both {other} and {owner}"
                )
            seen[(family, n)] = owner
        claim(owner, numbers, "pinned")
    # 2. live numbers of names still in the IR keep them
    wanted = dict(request.names)
    for owner, numbers in sorted(request.live.items()):
        if owner in out or owner not in wanted:
            continue
        clash = [f for f, n in numbers.items() if (f, n) in seen]
        if clash:
            raise AddressError(
                f"{request.domain}: a pinned host takes the live number of {owner} "
                f"({', '.join(clash)}) — an address written into the topology "
                "that the fabric already uses"
            )
        claim(owner, {f: n for f, n in numbers.items() if f in wanted[owner]}, "live")
    # 2b. what an earlier run placed (lost from the live view by a reboot)
    #     goes back to its name — unless taken meanwhile, or no longer usable
    for owner, numbers in sorted(request.remembered.items()):
        if owner in out or owner not in wanted:
            continue
        keep = {
            f: n
            for f, n in numbers.items()
            if f in wanted[owner]
            and f in prefixes
            and n not in taken[f]
            and n in _usable(prefixes[f])
        }
        if set(keep) == set(wanted[owner]):
            claim(owner, keep, "remembered")
    # 3. new names, sorted, by strategy
    for owner, families in sorted(request.names):
        if owner in out:
            continue
        out[owner] = _place(request, prefixes, taken, owner, families)
    return out


def _place(
    request: AllocationRequest,
    prefixes: Mapping[str, IPAddress],
    taken: dict[str, set[int]],
    owner: str,
    families: Sequence[str],
) -> dict[str, int]:
    strategies = {request.strategy.get(f, "low-high") for f in families}
    groups: list[tuple[str, ...]] = (
        [tuple(families)] if len(strategies) == 1 else [(f,) for f in families]
    )
    placed: dict[str, int] = {}
    for group in groups:
        free = set.intersection(*(set(_usable(prefixes[f])) - taken[f] for f in group))
        if not free:
            raise AddressError(
                f"{request.domain}: no free {'/'.join(group)} address left for {owner}"
            )
        strategy = request.strategy.get(group[0], "low-high")
        n = _choose(free, strategy, f"{request.domain}|{owner}")
        for f in group:
            placed[f] = n
            taken[f].add(n)
    return placed


def _choose(free: set[int], strategy: str, seed: str) -> int:
    if strategy == "high-low":
        return max(free)
    if strategy == "random":
        # A deterministic order seeded by domain and name: it spreads
        # numbers, it never draws from a random source.
        return min(
            free,
            key=lambda n: hashlib.sha256(f"{seed}|{n}".encode()).digest(),
        )
    return min(free)


# -- the metadata ------------------------------------------------------------


def _walk(value: object) -> Iterator[object]:
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        yield value
        for field in dataclasses.fields(value):
            yield from _walk(getattr(value, field.name))


def _strategy(value: object) -> str:
    return str(getattr(value, "value", value))


class Addresses:
    """Every LogicalIP of one IR, placed. Built by `resolve`."""

    def __init__(
        self,
        prefixes: Mapping[str, Mapping[str, IPAddress]],
        numbers: Mapping[str, Mapping[str, Mapping[str, int]]],
        origin: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self._prefixes = prefixes  # net id -> family -> prefix
        self._numbers = numbers  # net id -> owner -> family -> host number
        self._origin = origin or {}  # net id -> owner -> "live" | "remembered" | "new"

    @classmethod
    def resolve(
        cls,
        nodes: Iterable[pt.Model],
        live: Mapping[str, Mapping[str, Mapping[str, int]]] | None = None,
        held: Mapping[str, Mapping[str, Sequence[int]]] | None = None,
        remembered: Mapping[str, Mapping[str, Mapping[str, int]]] | None = None,
    ) -> Addresses:
        """Place every name of `nodes`. `live` (net id -> owner -> family ->
        number) and `held` (net id -> family -> every live number) are the
        live view; empty for `create`. `remembered` (as `live`): what an
        earlier run placed — the allocator state file (ADR 0012 §6)."""
        nodes = list(nodes)
        nets = [n for n in nodes if isinstance(n, pt.IpNetNode)]
        prefixes: dict[str, dict[str, IPAddress]] = {}
        strategy: dict[str, dict[str, str]] = {}
        for net in nets:
            prefixes[net.id] = {
                f: _parse(p, net.id)
                for f, p in (("ipv4", net.data.ipv4), ("ipv6", net.data.ipv6))
                if p is not None
            }
            strategy[net.id] = {
                "ipv4": _strategy(net.data.allocator.ipv4),
                "ipv6": _strategy(net.data.allocator.ipv6),
            }
        pinned: dict[str, dict[str, dict[str, int]]] = {}
        names: dict[str, dict[str, tuple[str, ...]]] = {}
        for node in nodes:
            for value in _walk(getattr(node, "data", None)):
                if not isinstance(value, pt.LogicalIP):
                    continue
                if value.net not in prefixes:
                    raise AddressError(f"{value.owner}: no ip.net node {value.net}")
                families = _families(value, prefixes[value.net])
                hosts = {f: _host_ref(value, f) for f in families}
                if all(h is None for h in hosts.values()):
                    names.setdefault(value.net, {})[value.owner] = families
                else:
                    pinned.setdefault(value.net, {})[value.owner] = {
                        f: _host_number(prefixes[value.net][f], h)
                        for f, h in hosts.items()
                        if h is not None
                    }
        numbers: dict[str, dict[str, dict[str, int]]] = {}
        remembered = remembered or {}
        for net_id, fams in prefixes.items():
            numbers[net_id] = allocate(
                AllocationRequest(
                    domain=net_id,
                    prefixes={f: p.to_string() for f, p in fams.items()},
                    strategy=strategy[net_id],
                    pinned=pinned.get(net_id, {}),
                    names=sorted(names.get(net_id, {}).items()),
                    live=(live or {}).get(net_id, {}),
                    held=(held or {}).get(net_id, {}),
                    remembered=remembered.get(net_id, {}),
                )
            )

        def how(net_id: str, owner: str) -> str:
            if owner in (live or {}).get(net_id, {}):
                return "live"
            before = remembered.get(net_id, {}).get(owner)
            return "remembered" if before == numbers[net_id].get(owner) else "new"

        origin = {
            net_id: {owner: how(net_id, owner) for owner in owners}
            for net_id, owners in names.items()
        }
        return cls(prefixes, numbers, origin)

    def placed(self) -> LiveNumbers:
        """net id -> owner -> family -> number, for every name the runtime
        placed (not the pinned ones): what the allocator state file keeps."""
        return {
            net_id: {owner: dict(self._numbers[net_id][owner]) for owner in owners}
            for net_id, owners in self._origin.items()
        }

    def allocated(self) -> list[tuple[str, str, str, str]]:
        """(net id, owner, addresses, "live" | "remembered" | "new") for every address the
        runtime placed — the resolved view a run reports (ADR 0011 §3)."""
        out = []
        for net_id, owners in sorted(self._origin.items()):
            for owner, how in sorted(owners.items()):
                fams = self._numbers[net_id][owner]
                text = " ".join(self.address(net_id, owner, f).to_string() for f in sorted(fams))
                out.append((net_id, owner, text, how))
        return out

    # -- what services ask --

    def address(self, net_id: str, owner: str, family: str) -> IPAddress:
        prefix = self._prefixes.get(net_id, {}).get(family)
        if prefix is None:
            raise AddressError(f"{owner}: {net_id} has no {family}")
        n = self._numbers.get(net_id, {}).get(owner, {}).get(family)
        if n is None:
            raise AddressError(f"{owner}: no {family} address on {net_id}")
        addr = prefix.host(n)
        if addr is None:
            raise AddressError(f"{owner}: host {n} is outside {prefix.to_string()}")
        return addr

    def list(self, ips: Sequence[pt.LogicalIP] | None) -> list[str]:
        """With prefix, each name's IPv4 before its IPv6."""
        return [
            self.address(ip.net, ip.owner, f).to_string()
            for ip in ips or []
            for f in _families(ip, self._prefixes[ip.net])
        ]

    def one(self, ip: pt.LogicalIP) -> str:
        found = self.list([ip])
        if len(found) != 1:
            raise AddressError(f"{ip.owner}: expected one address, got {found}")
        return found[0]

    def ref(self, ref: pt.AddrRef | str) -> str:
        """Bare: a next hop, a nameserver, an SNAT's external address. A
        string is an address that is not the fabric's, as written."""
        if isinstance(ref, str):
            return ref
        family = _strategy(ref.family)
        if ref.host is not None:  # carried by nothing of the fabric: pinned here
            prefix = self._prefixes[ref.net][family]
            addr = prefix.host(_host_number(prefix, ref.host))
            if addr is None:
                raise AddressError(f"{ref.owner}: host {ref.host} outside {prefix.to_string()}")
            return str(addr.to_s())
        return str(self.address(ref.net, ref.owner, family).to_s())

    def net(self, net_id: str, family: str) -> str:
        return str(self._prefixes[net_id][family].to_string())

    def mac(self, ips: Sequence[pt.LogicalIP] | None, name: str) -> str:
        """The fold of the first resolved IPv4 (`00:00:<ipv4 hex>`), else a
        locally-administered MAC hashed from `name` (ADR 0011 §5)."""
        for ip in ips or []:
            if "ipv4" in _families(ip, self._prefixes[ip.net]):
                return mac_from_v4(str(self.address(ip.net, ip.owner, "ipv4").to_s()))
        return mac_from_name(name)


def _families(ip: pt.LogicalIP, prefixes: Mapping[str, IPAddress]) -> tuple[str, ...]:
    if ip.families:
        return tuple(_strategy(f) for f in ip.families)
    if isinstance(ip.host, pt.HostNumbers):
        return tuple(f for f in FAMILIES if getattr(ip.host, f) is not None)
    return tuple(f for f in FAMILIES if f in prefixes)


def _host_ref(ip: pt.LogicalIP, family: str) -> int | str | None:
    if isinstance(ip.host, pt.HostNumbers):
        value: int | str | None = getattr(ip.host, family)
        return value
    return ip.host


def mac_from_v4(address: str) -> str:
    """192.168.128.2 -> 00:00:c0:a8:80:02 — unique per collision domain,
    since two names never hold one number there at once."""
    octets = [int(o) for o in address.split("/")[0].split(".")]
    return "00:00:" + ":".join(f"{o:02x}" for o in octets)


def mac_from_name(name: str) -> str:
    """02:… from the 64-bit FNV-1a of `name` — the same as the TypeScript
    side's macFromName (src/addressing.ts)."""
    h = 0xCBF29CE484222325
    for byte in name.encode():
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    hexed = f"{h:016x}"[:10]
    return "02:" + ":".join(hexed[i : i + 2] for i in range(0, 10, 2))


# -- the live view (ADR 0011 §3) --------------------------------------------

LiveNumbers = dict[str, dict[str, dict[str, int]]]  # net id -> owner -> family -> n
HeldNumbers = dict[str, dict[str, list[int]]]  # net id -> family -> every live n


def live_view(
    nodes: Iterable[pt.Model], live: Mapping[str, Mapping[str, Any]]
) -> tuple[LiveNumbers, HeldNumbers]:
    """From the live RawIR (node id -> node): the numbers the names of the
    IR hold now (`live`), and every number taken on each collision domain,
    ours or not (`held`) — what `Addresses.resolve` keeps and avoids.

    A workload NIC is found by its container and link name. An OVN router
    port is found by its name: its owner is the IR id of the
    same port (`ovnrouter:<router>|lrp:<port>`); its collision domain is the switch
    its peer port binds it to (`options:router-port`). A port of ours with
    an address outside the IR's prefix means the prefix was changed — an
    error: renumbering is a reinstall (ADR 0011 §3)."""
    nodes = list(nodes)
    prefixes: dict[str, dict[str, IPAddress]] = {}
    for net in (n for n in nodes if isinstance(n, pt.IpNetNode)):
        prefixes[net.id] = {
            f: _parse(p, net.id)
            for f, p in (("ipv4", net.data.ipv4), ("ipv6", net.data.ipv6))
            if p is not None
        }
    owners: dict[str, set[str]] = {}  # net id -> owners in the IR
    net_of: dict[str, str] = {}  # owner -> its net id
    for node in nodes:
        for value in _walk(getattr(node, "data", None)):
            if isinstance(value, pt.LogicalIP):
                owners.setdefault(value.net, set()).add(value.owner)
                net_of.setdefault(value.owner, value.net)
    switch_of_port: dict[str, str] = {}
    for raw in live.values():
        if raw.get("kind") == "ovn.lsp":
            port = (raw.get("options") or {}).get("router-port")
            if port:
                switch_of_port[port] = str(raw.get("switchRef", "")).removeprefix("ovn.ls:")
    out_live: LiveNumbers = {}
    held: dict[str, dict[str, set[int]]] = {}

    def hold(net_id: str, value: str, owner: str | None, who: str, strict: bool = True) -> None:
        addr = IPAddress.parse(value)
        if addr is None:
            return
        family = _family(addr)
        prefix = prefixes[net_id].get(family)
        if prefix is None:
            return
        inside = addr.change_prefix(prefix.prefix.num)
        if inside is None or not prefix.includes(inside):
            if owner is not None and strict:
                raise AddressError(
                    f"{net_id}: {who} holds {value}, outside {prefix.to_string()} — the "
                    "prefix was changed; renumbering a collision domain is a reinstall"
                )
            return
        n = int(inside.host_number())
        held.setdefault(net_id, {}).setdefault(family, set()).add(n)
        if owner is not None:
            out_live.setdefault(net_id, {}).setdefault(owner, {})[family] = n

    for node_id, raw in live.items():
        kind = raw.get("kind")
        if kind == "ovn.lrp":
            switch = switch_of_port.get(str(raw.get("name")))
            net_id = f"ipnet:ls:{switch}"
            if switch is None or net_id not in prefixes:
                continue
            # its owner: the IR id of the same port (ovn.lrp:<r>|<p> in RawIR)
            router = str(raw.get("routerRef", "")).removeprefix("ovn.lr:")
            ir_id = f"ovnrouter:{router}|lrp:{raw.get('name')}"
            owner = ir_id if ir_id in owners.get(net_id, set()) else None
            for value in raw.get("networks") or []:
                hold(net_id, str(value), owner, node_id)
        elif kind == "addr":
            # A workload NIC: its container's netns (`ovn-fabric-<service>`,
            # also seen as `docker:…`), its link the NIC's name — owned by
            # `kernel.app.container:<service>|nic:<nic>` (src/ir.ts).
            netns = str(raw.get("netnsRef", "")).removeprefix("netns:")
            container = netns.removeprefix("docker:")
            if not container.startswith("ovn-fabric-"):
                continue
            nic = str(raw.get("linkRef", "")).rpartition("|")[2]
            owner = f"kernel.app.container:{container.removeprefix('ovn-fabric-')}|nic:{nic}"
            # Its net: the owner's in the IR; for a NIC whose workload left
            # the IR, the switch it is named after (src/ir.ts shortIfaceName)
            # — its address is still held until the cleanup removes it.
            nic_net = net_of.get(owner, f"ipnet:ls:{nic}")
            if nic_net not in prefixes:
                continue
            value = f"{raw.get('address')}/{raw.get('prefixlen')}"
            # a link-local address is the kernel's, never the net's
            hold(
                nic_net,
                value,
                owner if owner in owners.get(nic_net, set()) else None,
                node_id,
                strict=False,
            )
        elif kind == "ovn.lsp":
            net_id = f"ipnet:ls:{str(raw.get('switchRef', '')).removeprefix('ovn.ls:')}"
            if net_id not in prefixes:
                continue
            for entry in raw.get("addresses") or []:  # "<mac> <ip> <ip>…", or "router"
                for value in str(entry).split()[1:]:
                    hold(net_id, value, None, node_id)
    return out_live, {
        net_id: {f: sorted(ns) for f, ns in fams.items()} for net_id, fams in held.items()
    }
