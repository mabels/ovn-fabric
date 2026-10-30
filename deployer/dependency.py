from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

INSTALL_INTERVAL = 24 * 60 * 60
APT_UPDATE_STAMP = "/var/lib/ovn-fabric/last-apt-update"


def _read_stamp(path: str) -> float:
    try:
        with open(path, encoding="utf-8") as handle:
            return float(handle.read().strip())
    except (OSError, ValueError):
        return 0.0


def _write_stamp(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(time.time()))


async def docker_image_exists(image: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/docker",
            "image",
            "inspect",
            image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0
    except OSError:
        return False


async def _apt_retry(
    job: Any, label: str, cmd: list[str], attempts: int = 6, timeout: float | None = 600
) -> bool:
    delay = 5.0
    for attempt in range(1, attempts + 1):
        if await job.run_cmd(label, cmd, False, timeout=timeout):
            return True
        if attempt < attempts:
            job.warn(f"{label}: attempt {attempt}/{attempts} failed; retrying in {delay:g}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)
    job.error(f"{label}: gave up after {attempts} attempts")
    return False


async def _dpkg_version(job: Any, name: str) -> str | None:
    status = await job.capture_cmd(
        "dpkg", ["dpkg-query", "-W", "-f=${Status} ${Version}", name], False
    )
    if not status.startswith("install ok installed"):
        return None
    version = status.rsplit(" ", 1)[-1]
    return version if isinstance(version, str) else None


async def install_packages(
    job: Any, deps: dict[str, dict[str, Any]], *, dry_run: bool = False
) -> None:
    if dry_run:
        for entry in deps.values():
            for dep in entry["concrete"]:
                if dep["kind"] == "apt-dep":
                    job.info(f"dry-run: apt-dep {dep['package']} installed")
                    job.emit(
                        {"kind": "apt-dep", "package": dep["package"], "state": "installed"},
                        queue=True,
                    )
                elif dep["kind"] == "sys-cmd-dep":
                    job.info(f"dry-run: sys-cmd-dep {dep['name']} installed")
                    job.emit(
                        {"kind": "sys-cmd-dep", "name": dep["name"], "state": "installed"},
                        queue=True,
                    )
        return
    label = "install-packages"
    apt_packages: list[str] = []
    sys_cmds: list[dict[str, Any]] = []
    for entry in deps.values():
        for dep in entry["concrete"]:
            if dep["kind"] == "apt-dep" and dep["package"] not in apt_packages:
                apt_packages.append(dep["package"])
            elif dep["kind"] == "sys-cmd-dep":
                sys_cmds.append(dep)

    installed = [name for name in apt_packages if await _dpkg_version(job, name) is not None]
    missing = [name for name in apt_packages if name not in installed]
    if missing:
        job.info(f"apt-get install: {' '.join(sorted(missing))}")

    update_ok = True
    if apt_packages:
        now = time.time()
        last = await asyncio.to_thread(_read_stamp, APT_UPDATE_STAMP)
        if now - last < INSTALL_INTERVAL:
            job.debug(f"apt update is fresh ({(now - last) / 3600:.1f}h old); skipping")
        else:
            update_ok = await _apt_retry(
                job,
                label,
                [
                    "apt-get",
                    "-o",
                    "Dpkg::Use-Pty=0",
                    "-o",
                    "Dpkg::Progress-Fancy=0",
                    "update",
                ],
            )
            if update_ok and not dry_run:
                try:
                    await asyncio.to_thread(_write_stamp, APT_UPDATE_STAMP)
                except OSError as exc:
                    job.warn(f"cannot save apt update timestamp: {exc}")
            elif not update_ok:
                job.error(f"{label}: apt-get update failed; skipping install")

    if update_ok and installed:
        await _apt_retry(
            job,
            label,
            [
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "APT_LISTCHANGES_FRONTEND=none",
                "LC_ALL=C",
                "TERM=dumb",
                "apt-get",
                "-o",
                "Dpkg::Use-Pty=0",
                "-o",
                "Dpkg::Progress-Fancy=0",
                "install",
                "--only-upgrade",
                "-y",
                *sorted(installed),
            ],
        )

    apt_ok = update_ok
    if apt_ok and missing:
        apt_ok = await _apt_retry(
            job,
            label,
            [
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "APT_LISTCHANGES_FRONTEND=none",
                "LC_ALL=C",
                "TERM=dumb",
                "apt-get",
                "-o",
                "Dpkg::Use-Pty=0",
                "-o",
                "Dpkg::Progress-Fancy=0",
                "install",
                "-y",
                *sorted(missing),
            ],
        )

    installed_sys: set[str] = set()
    for dep in sorted(sys_cmds, key=lambda entry: entry.get("order", 0)):
        if not apt_ok:
            job.warn(f"cannot install {dep['name']}: apt dependencies failed")
            continue
        if await _apt_retry(job, label, dep["cmd"]):
            installed_sys.add(dep["name"])

    for name in apt_packages:
        version = await _dpkg_version(job, name)
        if version is not None:
            job.info(f"apt-dep {name} resolved ({version})")
            job.emit(
                {"kind": "apt-dep", "package": name, "state": "installed"},
                queue=True,
            )
    for name in installed_sys:
        job.info(f"sys-cmd-dep {name} resolved")
        job.emit(
            {"kind": "sys-cmd-dep", "name": name, "state": "installed"},
            queue=True,
        )


async def install_image(job: Any, entry: dict[str, Any], *, dry_run: bool = False) -> None:
    label = f"image:{entry['image']}"
    if dry_run:
        job.info(f"dry-run: docker-image {entry['image']} installed")
        job.emit(
            {
                "kind": "dependency",
                "dep": {"kind": "docker-image", "image": entry["image"]},
                "state": "installed",
            },
            queue=True,
        )
        return
    if await docker_image_exists(entry["image"]):
        job.info(f"docker-image {entry['image']} resolved (already present)")
        job.emit(
            {
                "kind": "dependency",
                "dep": {"kind": "docker-image", "image": entry["image"]},
                "state": "installed",
            },
            queue=True,
        )
        return
    # A pull or build runs to completion — no timeout: a big image on a
    # slow uplink takes as long as it takes, and the jobs gated on it
    # wait for this event. A real failure is retried with backoff.
    if entry.get("dockerfile") is None:
        job.info(f"pulling {entry['image']}")
        ok = await _apt_retry(job, label, ["/usr/bin/docker", "pull", entry["image"]], timeout=None)
        if not ok:
            job.warn(f"could not pull {entry['image']}")
            return
        job.info(f"pulled {entry['image']}")
    else:
        build_dir = tempfile.mkdtemp()
        try:
            Path(build_dir, "Dockerfile").write_text(entry["dockerfile"])
            job.info(f"building {entry['image']}")
            ok = await _apt_retry(
                job,
                label,
                ["/usr/bin/docker", "build", "-t", entry["image"], build_dir],
                timeout=None,
            )
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)
        if not ok:
            job.warn(f"could not build {entry['image']}")
            return
        job.info(f"built {entry['image']}")
    job.emit(
        {
            "kind": "dependency",
            "dep": {"kind": "docker-image", "image": entry["image"]},
            "state": "installed",
        },
        queue=True,
    )
