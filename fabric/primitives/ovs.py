from __future__ import annotations

import dataclasses

from ladops import ovs as ovs_ops

from ..lifecycle import (
    Command,
    CommandPrimitive,
    Primitive,
    RawNodeTemplate,
)


@dataclasses.dataclass(frozen=True)
class OVSBridge(CommandPrimitive):
    name: str
    datapath: str | None = None
    fail_mode: str = "standalone"

    @property
    def key(self) -> str:
        return f"ovs.bridge:{self.name}"

    def as_primitive(self) -> Primitive:
        create: list[Command] = [
            Command(f"bridge:{self.name} (create)", tuple(ovs_ops.add_br_argv(self.name)))
        ]
        if self.datapath is not None:
            create.append(
                Command(
                    f"bridge:{self.name} (datapath)",
                    tuple(ovs_ops.set_bridge_datapath_argv(self.name, self.datapath)),
                )
            )
        create.append(
            Command(
                f"bridge:{self.name} (fail-mode)",
                tuple(ovs_ops.set_bridge_fail_mode_argv(self.name, self.fail_mode)),
            )
        )
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovs.bridge",
                    fields={
                        "name": self.name,
                        "portRefs": [],
                        "externalIds": {},
                    },
                ),
            ),
            create_cmds=tuple(create),
            delete_cmds=(
                Command(f"bridge:{self.name} (delete)", tuple(ovs_ops.del_br_argv(self.name))),
            ),
        )


@dataclasses.dataclass(frozen=True)
class OVSPort(CommandPrimitive):
    bridge: str
    port: str
    tag: int | None = None
    trunks: tuple[int, ...] = ()

    @property
    def key(self) -> str:
        return f"ovs.port:{self.port}"

    def as_primitive(self) -> Primitive:
        create: list[Command] = [
            Command(
                f"port:{self.port} (create)",
                tuple(ovs_ops.add_port_argv(self.bridge, self.port)),
            )
        ]
        if self.tag is not None:
            create.append(
                Command(
                    f"port:{self.port} (tag)",
                    tuple(ovs_ops.set_port_tag_argv(self.port, self.tag)),
                )
            )
        if self.trunks:
            create.append(
                Command(
                    f"port:{self.port} (trunks)",
                    tuple(ovs_ops.set_port_trunks_argv(self.port, list(self.trunks))),
                )
            )
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovs.port",
                    fields={
                        "bridgeRef": f"ovs.bridge:{self.bridge}",
                        "name": self.port,
                        "trunks": list(self.trunks),
                        "ifaceRefs": [],
                        "externalIds": {},
                    },
                ),
            ),
            create_cmds=tuple(create),
            delete_cmds=(
                Command(
                    f"port:{self.port} (delete)",
                    tuple(ovs_ops.del_port_argv(self.bridge, self.port)),
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class OVSAfxdpPort(CommandPrimitive):
    bridge: str
    port: str
    mode: str
    n_rxq: int
    trunks: tuple[int, ...] = ()

    @property
    def key(self) -> str:
        return f"ovs.port:{self.port}"

    def as_primitive(self) -> Primitive:
        create: list[Command] = [
            Command(
                f"port:{self.port} (create)",
                tuple(ovs_ops.add_afxdp_port_argv(self.bridge, self.port, self.mode, self.n_rxq)),
            )
        ]
        if self.trunks:
            create.append(
                Command(
                    f"port:{self.port} (trunks)",
                    tuple(ovs_ops.set_port_trunks_argv(self.port, list(self.trunks))),
                )
            )
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovs.port",
                    fields={
                        "bridgeRef": f"ovs.bridge:{self.bridge}",
                        "name": self.port,
                        "trunks": list(self.trunks),
                        "ifaceRefs": [],
                        "externalIds": {},
                        "type": "afxdp",
                        "options": {"xdp-mode": self.mode, "n_rxq": self.n_rxq},
                    },
                ),
            ),
            create_cmds=tuple(create),
            delete_cmds=(
                Command(
                    f"port:{self.port} (delete)",
                    tuple(ovs_ops.del_port_argv(self.bridge, self.port)),
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class OVSMapping(CommandPrimitive):
    """Open_vSwitch.external_ids:ovn-bridge-mappings, a single global map
    rendered as `network:bridge` entries."""

    mappings: tuple[str, ...]

    @property
    def key(self) -> str:
        return "ovs.mapping:ovn-bridge-mappings"

    @property
    def value(self) -> str:
        return ",".join(self.mappings)

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovs.mapping",
                    fields={
                        "name": "ovn-bridge-mappings",
                        "mappings": list(self.mappings),
                        "externalIds": {"ovn-bridge-mappings": self.value},
                    },
                ),
            ),
            create_cmds=(
                Command(
                    "mapping:ovn-bridge-mappings (set)",
                    tuple(ovs_ops.set_external_id_argv("ovn-bridge-mappings", self.value)),
                ),
            ),
            delete_cmds=(
                Command(
                    "mapping:ovn-bridge-mappings (remove)",
                    tuple(ovs_ops.remove_external_id_argv("ovn-bridge-mappings")),
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class OVSExternalId(CommandPrimitive):
    """One Open_vSwitch external_ids key/value."""

    key_name: str
    value: str

    @property
    def key(self) -> str:
        return f"ovs.external-id:{self.key_name}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovs.external-id",
                    fields={"key": self.key_name, "value": self.value},
                ),
            ),
            create_cmds=(
                Command(
                    f"ovs external-id:{self.key_name} (set)",
                    tuple(ovs_ops.set_external_id_argv(self.key_name, self.value)),
                ),
            ),
            delete_cmds=(
                Command(
                    f"ovs external-id:{self.key_name} (remove)",
                    tuple(ovs_ops.remove_external_id_argv(self.key_name)),
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class OVSIpfix(CommandPrimitive):
    """One bridge's IPFIX sampler. CREATE attaches it with `script` (a
    `sh -c` body, see ir_common._ipfix_enable_script); DELETE clears the
    weak ref, which lets OVSDB GC the orphaned IPFIX row."""

    bridge: str
    script: str

    @property
    def key(self) -> str:
        return f"ovs.ipfix:{self.bridge}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovs.ipfix",
                    fields={"bridgeRef": f"ovs.bridge:{self.bridge}"},
                ),
            ),
            create_cmds=(Command(f"ipfix:{self.bridge}", ("sh", "-c", self.script)),),
            delete_cmds=(
                Command(
                    f"ipfix:{self.bridge} (clear)",
                    ("ovs-vsctl", "--if-exists", "clear", "Bridge", self.bridge, "ipfix"),
                ),
            ),
        )
