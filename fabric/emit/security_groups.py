"""Security groups: resolving references, and their rules as firewall primitives."""

from __future__ import annotations

from fabric.addresses import Addresses
from fabric.primitives.host import NetnsPrimitive
from fabric.primitives.net import FirewallRule
from protocol import generated as pt

from .emitter import Emitter


# One flag-taking function per object, create/delete branches adjacent
# (2026-08-19 — the "write_interface(action)" schema): adding a create
# command forces you to look at its delete right next to it.
def _selectors(values: list[str | pt.AddrRef] | None, addrs: Addresses) -> list[str]:
    """A filter rule's sources/destinations as text: a prefix as written, an
    address of the fabric (a reference, ADR 0011 §3b) as its host prefix."""
    out = []
    for value in values or []:
        if isinstance(value, str):
            out.append(value)
        else:
            width = 32 if value.family == pt.Family.ipv4 else 128
            out.append(f"{addrs.ref(value)}/{width}")
    return out


# A `security.group` node (implementation-abstract — src/ir.ts's
# securityGroupToIR), by name. Found by the name a kernel.router side
# node carries in `data.securityGroup`: the side node is the ATTACHMENT
# point, the group node carries the rules.
def _security_group_by_name(name: str, nodes: list[pt.Model]) -> pt.SecurityGroupNode | None:
    return next(
        (n for n in nodes if n.kind == "security.group" and n.key.name == name),
        None,
    )


def _security_groups_from_refs(
    refs: list[pt.SecurityGroupRef],
    nodes: list[pt.Model],
    render: pt.Render,
    where: str,
) -> list[pt.SecurityGroupNode]:
    groups: list[pt.SecurityGroupNode] = []
    for ref in refs:
        if ref.render != render:
            raise ValueError(
                f"{where}: security group {ref.name!r} renders for "
                f"{ref.render.value}, not {render.value}"
            )
        group = _security_group_by_name(ref.name, nodes)
        if group is None:
            raise ValueError(
                f'{where} references security group "{ref.name}", but no such security.group node'
            )
        groups.append(group)
    return groups


# A security group's rules, applied INSIDE a kernel router's netns on a
# specific real interface (the `right` side's WAN iface, by
# construction). This is the services emit function for `security.group`
# (see src/ir.ts's securityGroupToIR): today it only knows how to turn
# the two masq rule shapes into iptables/ip6tables commands, and
# anything else is skipped loudly with a comment rather than silently
# dropped — future rule kinds (docker containers, wireguard, ...) extend
# the same if-chain without touching anything else.
#
# Plain `-A` add, same as every other create emitter in this module:
# iptables has no --may-exist, but a `-C ... || -A` conditional must NOT
# be emitted here — the generated Python deployer executes each line via
# shlex.split() as argv, so `2>/dev/null`/`||` would be handed to
# iptables as literal arguments (confirmed live, 2026-08-23: "Bad
# argument `2>/dev/null'"). Diffing against live state is the
# reconciler's job, not a shell conditional's.
#
# No delete branch: a `security.group` is pure netfilter state INSIDE
# the netns, and `ip netns delete` (the kernel router's own delete path
# in the kernel router job) destroys the whole netns, its rules included —
# nothing left to tear down separately.
def _emit_rule(
    emit: Emitter,
    netns_primitive: NetnsPrimitive | None,
    netns_var: str | None,
    rule: FirewallRule,
) -> None:
    if netns_primitive is not None:
        emit.ovn(rule, parent=netns_var)
        return
    for command in rule.as_primitive().create_cmds:
        emit.sh(list(command.argv))


_FAMILY = {pt.Family.ipv4: "ipv4", pt.Family.ipv6: "ipv6"}


def _emit_security_group_rules(
    group: pt.SecurityGroupNode,
    netns: str,
    dev: str,
    emit: Emitter,
    addrs: Addresses,
    netns_primitive: NetnsPrimitive | None = None,
    netns_var: str | None = None,
) -> None:
    group_unit = emit.unit("security group", f"{group.key.name} in {netns} on {dev}")
    # Builds the rules (its host only names their commands).
    rules = netns_primitive or NetnsPrimitive(host="", name=netns)
    # Every filter rule is stateful: allow replies first, per family for
    # every direction the group actually uses.
    stateful = sorted(
        {
            (_FAMILY[rule.family], rule.direction.value)
            for rule in group.data.rules
            if rule.kind == "filter"
        }
    )
    for family, direction in stateful:
        _emit_rule(
            group_unit, netns_primitive, netns_var, rules.add_stateful_accept(family, direction)
        )
    for rule in group.data.rules:
        if rule.kind == "masq":
            _emit_rule(
                group_unit, netns_primitive, netns_var, rules.add_masq(_FAMILY[rule.family], dev)
            )
            continue
        if rule.kind != "filter":
            raise ValueError(
                f"{group.key.name}: rule kind {rule.kind!r} is not a kernel "
                "rule; a kernel reference may not carry it"
            )
        _emit_rule(
            group_unit,
            netns_primitive,
            netns_var,
            rules.add_filter(
                _FAMILY[rule.family],
                rule.direction.value,
                rule.action.value,
                sources=tuple(_selectors(rule.sources, addrs)),
                exclude_sources=tuple(_selectors(rule.excludeSources, addrs)),
                destinations=tuple(_selectors(rule.destinations, addrs)),
                protocol=rule.protocol.value if rule.protocol is not None else None,
                ports=[(port.from_, port.to) for port in rule.ports or ()],
            ),
        )
    default = group.data.default
    if default is not None:
        for direction in ("input", "output", "forward"):
            action = getattr(default, direction, None)
            if action is not None:
                _emit_rule(
                    group_unit,
                    netns_primitive,
                    netns_var,
                    rules.add_default(direction, action.value),
                )


def _masq_families(security_groups: list[pt.SecurityGroupRef], nodes: list[pt.Model]) -> list[str]:
    """Resolve security-group references to the address families their
    `masq` rules cover — the app/nat consumers carry refs, not families."""
    families: set[str] = set()
    for ref in security_groups:
        group = _security_group_by_name(ref.name, nodes)
        if group is None:
            raise ValueError(
                f'kernel app references security group "{ref.name}", which is not present in the IR'
            )
        for rule in group.data.rules:
            if rule.kind == "masq":
                families.add("ipv4" if rule.family == pt.Family.ipv4 else "ipv6")
    return sorted(families)
