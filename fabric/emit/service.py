"""Services (workloads): their NICs and container."""

from __future__ import annotations

import hashlib

from fabric.addresses import Addresses
from fabric.decide import Decide
from fabric.primitives.docker import files_hash
from fabric.primitives.host import HostPrimitive
from protocol import generated as pt

from .container import _security_flags
from .emitter import Emitter, Ref, _network_of
from .images import _docker_dockerfile, _emit_image


# The OVS bridge (`br-<shortname>`) a segment is bound to ON this host — the
# ovn.ls node's interface shortName (same bridge the segment's localnet iface
# binds into via _emit_iface_bindings). A container face's veth root leg
# attaches there, putting that NIC on the segment's L2 (2026-09-08).
def _segment_bridge(host_name: str, l2_segment: str, nodes: list[pt.Model]) -> str:
    # ovn.ls interface `host` is the bare host NAME (not the `host:` id) —
    # same convention _host_bindings/_emit_iface_bindings use.
    for ls in nodes:
        if ls.kind != "ovn.ls" or ls.id != l2_segment:
            continue
        for binding in ls.data.interfaces:
            if binding.host == host_name and binding.iface.bridge:
                return binding.iface.bridge
    raise ValueError(
        f"no ovn.ls interface on {host_name} for {l2_segment} to bind a container veth to"
    )


# One NIC of a workload: (index, serviceRef, bridge, root-side leg, the
# in-container NIC name) — see _service_faces.
Face = tuple[int, pt.ServiceRef, str, str, str]


def _service_faces(service_name: str, host_name: str, nodes: list[pt.Model]) -> list[Face]:
    """One tuple per NIC of a workload: (index, serviceRef, bridge, dev,
    nic) — `dev` is the root-side leg; `nic` is the IN-CONTAINER interface
    name, already RESOLVED by the generator (ServiceRef.name — the deployer
    never shortens). Derived from every ovn.lrp that REFERENCES this
    service (`kernel.app.container:<name>`) — the endpoint carries the attachment."""
    service_id = f"kernel.app.container:{service_name}"
    base = hashlib.md5(service_name.encode()).hexdigest()[:8]
    out: list[Face] = []
    i = 0
    for lrp in nodes:
        if lrp.kind != "ovn.lrp":
            continue
        for ref in lrp.data.serviceRefs or []:
            if ref.service != service_id:
                continue
            bridge = _segment_bridge(host_name, lrp.data.l2Segment, nodes)
            out.append((i, ref, bridge, f"sc-{base}-{i}", ref.name))
            i += 1
    return out


def _emit_service_up(
    node: pt.KernelAppContainerNode | pt.KernelAppServiceNode,
    faces: list[Face],
    addrs: Addresses,
    emit: Emitter,
) -> None:
    """A workload: its image, then its container, NICs and addresses."""
    # Its image: a unit of its own, first in the scope (ADR 0012 §4).
    if node.kind == "kernel.app.container":
        _emit_image(
            HostPrimitive(emit.label.split(":", 1)[0]),
            node.data.image or "",
            _docker_dockerfile(node.data.build) if node.data.build is not None else None,
            f"/var/lib/ovn-fabric/build/{node.key.name}",
            emit.unit("image", node.data.image or node.key.name),
        )
    workload_unit = emit.unit("workload", node.key.name, key=node.id)
    _emit_service_body(node, faces, addrs, workload_unit)


def _emit_service_body(
    node: pt.KernelAppContainerNode | pt.KernelAppServiceNode,
    faces: list[Face],
    addrs: Addresses,
    emit: Emitter,
) -> None:
    """The `up` body: build/run the container, then for EACH NIC (from a
    referencing endpoint) move a veth into its netns, name it after the
    segment, apply that reference's ipaddrs/routes, and bind the root leg
    into the segment's OVS bridge."""
    if node.kind != "kernel.app.container":
        raise NotImplementedError(
            f"'{node.kind}' service '{node.key.name}': rendering this "
            f"workload is not implemented yet"
        )
    name = node.key.name
    host = HostPrimitive(emit.label.split(":", 1)[0])
    container = f"ovn-fabric-{name}"
    # The workload's in-memory FILESYSTEM (Service.files -> IR): written on
    # the host and bind-mounted back READ-ONLY, one mount per file — not a
    # directory mount, so whatever else lives in that container directory
    # isn't shadowed. Host state lives under /var/lib/ovn-fabric/<service>/
    # and is removed on `down`.
    files = list(node.data.files or [])
    file_mounts = [(f"/var/lib/ovn-fabric/{name}{f.path}", f.path, f.content) for f in files]
    emit.py(f"container = {container!r}")
    emit.py(f"image = {node.data.image!r}")
    for (host_path, _container_path, content), file in zip(file_mounts, files, strict=True):
        parent = host_path.rsplit("/", 1)[0]
        body = "\n".join(content) if isinstance(content, list) else content
        emit.ovn(host.make_dir(parent))
        if file.user is None and file.group is None and file.mode is None:
            emit.append(host_path, body)
        else:
            # Its owner and mode as the container's user needs them (the
            # mount is the host's file: same uid/gid, same permissions).
            mode = int(file.mode, 8) if file.mode is not None else None
            emit.write_file(host_path, body, mode, file.user, file.group)
    # (Its image: a unit of its own before this one, _emit_service_up.)
    # The service IS the container's command — `docker run -d` runs it
    # in the background (no sleep + docker exec dance).
    env = tuple(sorted((node.data.env or {}).items()))
    mounts = tuple(
        (host_path, f"{container_path}:ro") for host_path, container_path, _ in file_mounts
    )
    # DNS resolver (ServiceRef.resolver, resolved by the generator:
    # attachment > control-plane global > CoreDNS). Docker writes
    # /etc/resolv.conf from --dns; we never bind-mount over it. A container
    # has ONE resolv.conf, so the FIRST NIC that carries a resolver wins.
    run_flags: list[str] = []
    for _i, ref, _bridge, _dev, _nic in faces:
        resolver = ref.resolver
        if resolver and resolver.nameservers:
            for server in resolver.nameservers:
                run_flags += ["--dns", addrs.ref(server)]
            break
    docker = host.docker(
        container,
        node.data.image or "",
        command=tuple(node.data.cmd or ()),
        environment=env,
        volumes=mounts,
        run_flags=(*_security_flags(node.data.securityEnable), *run_flags),
        # A changed file needs a restart: the service read it (its owner
        # and mode too: whether it may).
        files=files_hash(
            *(
                "\n".join(content) if isinstance(content, list) else content
                for _path, _container_path, content in file_mounts
            ),
            *(
                f"{f.path}:{f.user}:{f.group}:{f.mode}"
                for f in files
                if (f.user, f.group, f.mode) != (None, None, None)
            ),
        ),
    )
    docker_var = emit.local(container)
    emit.ovn(docker, bind=docker_var, decide=Decide.docker_container)
    # The capture name is per container: the store is shared by every
    # controller of the run, and services run concurrently.
    pid_var = emit.local(f"{container}-pid")
    emit.capture(pid_var, ["/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}", Ref("container")])
    emit.ovn(host.make_dir("/var/run/netns"))
    # `container` is a Python local; the PID is a queued capture ref.
    emit.netns_symlink("container", pid_var)
    container_ns = docker.get_netns()
    container_ns_var = emit.local(f"{container}-netns")
    emit.ovn(container_ns, bind=container_ns_var)
    for i, ref, bridge, dev, nic in faces:
        peer = f"{dev}c"
        # Per-NIC routing table + a source rule: source-based policy
        # routing. Two NICs of the same workload may legitimately reach the
        # SAME destination; a separate table per NIC, entered by a rule
        # matching that NIC's own source prefix, keeps every path intact.
        table = 1000 + i
        nic_addrs = addrs.list(ref.ipaddrs)
        networks = [_network_of(a) for a in nic_addrs]
        nic_unit = emit.unit("NIC", f"{nic} on {bridge}")
        nic_unit.ovn(
            container_ns.add_link(
                peer,
                "veth",
                rename_to=nic,
                mac=ref.mac or addrs.mac(ref.ipaddrs, f"{ref.service}|{nic}"),
                root_leg=dev,
                bridge=bridge,
            ),
            parent=container_ns_var,
        )
        for address in nic_addrs:
            nic_unit.ovn(
                container_ns.add_addr(nic, address),
                parent=container_ns_var,
            )
        if ref.routes:
            for route in ref.routes:
                family = 6 if ":" in route.dst else 4
                nic_unit.ovn(
                    container_ns.add_route(
                        route.dst,
                        nic,
                        via=addrs.ref(route.via) if route.via is not None else None,
                        metric=route.metric,
                        table=table,
                        family=family,
                    ),
                    parent=container_ns_var,
                )
                if route.dst in ("0.0.0.0/0", "::/0"):
                    nic_unit.ovn(
                        container_ns.add_route(
                            route.dst,
                            nic,
                            via=addrs.ref(route.via) if route.via is not None else None,
                            metric=route.metric,
                            family=family,
                        ),
                        parent=container_ns_var,
                    )
            for network in networks:
                family = 6 if ":" in network else 4
                nic_unit.ovn(
                    container_ns.add_ip_rule(network, table, family),
                    parent=container_ns_var,
                )
