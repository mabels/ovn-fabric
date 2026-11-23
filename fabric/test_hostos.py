from __future__ import annotations

import unittest
from collections.abc import Callable
from unittest import mock

from protocol import generated as pt

from .hostos import HostOsError, discover_host_os, resolve_host_os

Reader = Callable[[str], str | None]


def _reader(files: dict[str, str]) -> Reader:
    def read(path: str) -> str | None:
        return files.get(path)

    return read


class DiscoverHostOsTest(unittest.TestCase):
    def test_ubuntu(self) -> None:
        os = discover_host_os(_reader({"/etc/os-release": 'ID=ubuntu\nVERSION_ID="24.04"\n'}))
        self.assertIs(os.distro, pt.Distro.ubuntu)
        self.assertEqual(os.version, "24.04")
        self.assertIs(os.platform, pt.HostPlatform.generic)

    def test_rocky(self) -> None:
        os = discover_host_os(_reader({"/etc/os-release": "ID=rocky\nVERSION_ID=10.2\n"}))
        self.assertIs(os.distro, pt.Distro.rocky)
        self.assertEqual(os.version, "10.2")

    def test_raspbian_is_debian(self) -> None:
        os = discover_host_os(_reader({"/etc/os-release": "ID=raspbian\nVERSION_ID=12\n"}))
        self.assertIs(os.distro, pt.Distro.debian)

    def test_quoted_values(self) -> None:
        os = discover_host_os(_reader({"/etc/os-release": "ID='fedora'\nVERSION_ID=\"44\"\n"}))
        self.assertIs(os.distro, pt.Distro.fedora)
        self.assertEqual(os.version, "44")

    def test_usr_lib_os_release_is_the_fallback(self) -> None:
        os = discover_host_os(_reader({"/usr/lib/os-release": "ID=debian\nVERSION_ID=13.7\n"}))
        self.assertIs(os.distro, pt.Distro.debian)
        self.assertEqual(os.version, "13.7")

    def test_raspberry_pi_model(self) -> None:
        os = discover_host_os(
            _reader(
                {
                    "/etc/os-release": "ID=ubuntu\nVERSION_ID=24.04\n",
                    "/proc/device-tree/model": "Raspberry Pi 5 Model B Rev 1.0\x00",
                }
            )
        )
        self.assertIs(os.platform, pt.HostPlatform.rpi)

    def test_raspberry_pi_compatible(self) -> None:
        os = discover_host_os(
            _reader(
                {
                    "/etc/os-release": "ID=debian\nVERSION_ID=12\n",
                    "/proc/device-tree/compatible": "raspberrypi,5-model-b\x00brcm,bcm2712\x00",
                }
            )
        )
        self.assertIs(os.platform, pt.HostPlatform.rpi)

    def test_missing_os_release_is_refused(self) -> None:
        with self.assertRaises(HostOsError):
            discover_host_os(_reader({}))

    def test_unknown_distro_is_refused(self) -> None:
        with self.assertRaises(HostOsError):
            discover_host_os(_reader({"/etc/os-release": "ID=centos\nVERSION_ID=10\n"}))


def _declared(
    distro: pt.Distro,
    version: str = "1",
    platform: pt.HostPlatform = pt.HostPlatform.generic,
) -> pt.HostOs:
    return pt.HostOs(distro=distro, version=version, platform=platform)


class ResolveHostOsTest(unittest.TestCase):
    def test_ubuntu_packages(self) -> None:
        target = resolve_host_os(_declared(pt.Distro.ubuntu))
        self.assertEqual(target.package_manager, "apt")
        self.assertEqual(target.system_deps("ovs"), ["openvswitch-switch"])
        self.assertEqual(target.system_deps("docker"), ["docker.io"])

    def test_rocky_packages(self) -> None:
        target = resolve_host_os(_declared(pt.Distro.rocky, "10.2"))
        self.assertEqual(target.package_manager, "dnf")
        self.assertEqual(target.system_deps("ovs"), ["openvswitch3.5"])
        self.assertEqual(target.system_deps("ovn"), ["ovn25.09-host"])
        self.assertEqual(target.system_deps("ovn-central"), ["ovn25.09-central"])
        self.assertEqual(target.system_deps("docker"), ["docker-ce"])

    def test_rocky_repositories(self) -> None:
        target = resolve_host_os(
            _declared(pt.Distro.rocky, "10.2"),
            abstract_names=["ovn", "ovs", "docker"],
        )
        self.assertEqual(
            {repo.id for repo in target.repositories()},
            {"centos-nfv-openvswitch", "docker-ce-stable"},
        )
        # A repository on its own does not replace the distro's packages.
        self.assertEqual(target.system_deps("ovn"), ["ovn25.09-host"])
        self.assertEqual(target.system_deps("docker"), ["docker-ce"])

    def test_rpi_defaults(self) -> None:
        target = resolve_host_os(_declared(pt.Distro.ubuntu, platform=pt.HostPlatform.rpi))
        self.assertEqual(target.system_deps("platform"), ["raspi-config", "linux-firmware"])

    def test_rpi_defaults_debian(self) -> None:
        target = resolve_host_os(_declared(pt.Distro.debian, platform=pt.HostPlatform.rpi))
        self.assertEqual(target.system_deps("platform"), ["raspi-config", "raspi-firmware"])

    def test_platform_defaults_merge_with_user_overrides(self) -> None:
        target = resolve_host_os(
            _declared(pt.Distro.ubuntu, platform=pt.HostPlatform.rpi),
            {"platform": [pt.DepPkg(kind="dep.pkg", name="ifstat")]},
        )
        self.assertEqual(
            target.system_deps("platform"),
            ["ifstat", "raspi-config", "linux-firmware"],
        )

    def test_unknown_name_without_override_is_none(self) -> None:
        target = resolve_host_os(_declared(pt.Distro.ubuntu))
        self.assertIsNone(target.system_deps("quantum"))

    def test_version_and_file_rendering(self) -> None:
        target = resolve_host_os(
            _declared(pt.Distro.ubuntu),
            {
                "traceroute": [pt.DepPkg(kind="dep.pkg", name="traceroute", version="1:2.1.6-1")],
                "ovn": [
                    pt.DepPkgFile(
                        kind="dep.pkgFile",
                        path="/root/ovn-host_26.03.0-2+ravlocal11_amd64.deb",
                    )
                ],
            },
        )
        self.assertEqual(target.system_deps("traceroute"), ["traceroute=1:2.1.6-1"])
        self.assertEqual(
            target.system_deps("ovn"),
            ["/root/ovn-host_26.03.0-2+ravlocal11_amd64.deb"],
        )

    def test_dnf_version_rendering(self) -> None:
        target = resolve_host_os(
            _declared(pt.Distro.fedora),
            {"traceroute": [pt.DepPkg(kind="dep.pkg", name="traceroute", version="2.1.6-3.el10")]},
        )
        self.assertEqual(target.system_deps("traceroute"), ["traceroute-2.1.6-3.el10"])

    def test_discovery_is_the_fallback(self) -> None:
        discovered = pt.HostOs(distro=pt.Distro.debian, version="12")
        with mock.patch("fabric.hostos.discover_host_os", return_value=discovered):
            target = resolve_host_os(None)
        self.assertIs(target.os.distro, pt.Distro.debian)
        self.assertIs(target.os.platform, pt.HostPlatform.generic)
        self.assertEqual(target.package_manager, "apt")

    def test_platform_is_discovered_when_omitted(self) -> None:
        discovered = pt.HostOs(
            distro=pt.Distro.ubuntu,
            version="24.04",
            platform=pt.HostPlatform.rpi,
        )
        with mock.patch("fabric.hostos.discover_host_os", return_value=discovered):
            target = resolve_host_os(pt.HostOs(distro=pt.Distro.ubuntu, version="24.04"))
        self.assertIs(target.os.platform, pt.HostPlatform.rpi)
        self.assertEqual(target.system_deps("platform"), ["raspi-config", "linux-firmware"])

    def test_declared_platform_wins_over_discovery(self) -> None:
        discovered = pt.HostOs(
            distro=pt.Distro.ubuntu,
            version="24.04",
            platform=pt.HostPlatform.rpi,
        )
        with mock.patch("fabric.hostos.discover_host_os", return_value=discovered):
            target = resolve_host_os(_declared(pt.Distro.ubuntu, "24.04"))
        self.assertIs(target.os.platform, pt.HostPlatform.generic)
        self.assertIsNone(target.system_deps("platform"))

    def test_version_is_not_taken_from_another_distro(self) -> None:
        discovered = pt.HostOs(
            distro=pt.Distro.ubuntu,
            version="24.04",
            platform=pt.HostPlatform.generic,
        )
        with mock.patch("fabric.hostos.discover_host_os", return_value=discovered):
            target = resolve_host_os(pt.HostOs(distro=pt.Distro.rocky))
        self.assertIsNone(target.os.version)
        self.assertIs(target.os.platform, pt.HostPlatform.generic)

    def test_repo_entries_are_kept(self) -> None:
        repo = pt.DepRepo(
            kind="dep.repo",
            id="centos-nfv-openvswitch",
            baseurl="https://example.invalid/nfv",
            gpgkey="https://example.invalid/key",
        )
        target = resolve_host_os(_declared(pt.Distro.rocky, "10.2"), {"ovn": [repo]})
        self.assertEqual(target.dependencies["ovn"], (repo,))

    def test_repo_entries_are_separate_from_packages(self) -> None:
        repo = pt.DepRepo(
            kind="dep.repo",
            id="centos-nfv-openvswitch",
            baseurl="https://example.invalid/nfv",
        )
        target = resolve_host_os(_declared(pt.Distro.rocky, "10.2"), {"ovn": [repo]})
        self.assertEqual(target.system_deps("ovn"), ["ovn25.09-host"])
        self.assertEqual(target.repositories(), (repo,))

    def test_discovery_failure_keeps_the_declared_os(self) -> None:
        with mock.patch("fabric.hostos.discover_host_os", side_effect=HostOsError("no os-release")):
            target = resolve_host_os(pt.HostOs(distro=pt.Distro.ubuntu, version="24.04"))
        self.assertIs(target.os.distro, pt.Distro.ubuntu)
        self.assertEqual(target.os.version, "24.04")
        self.assertIs(target.os.platform, pt.HostPlatform.generic)


if __name__ == "__main__":
    unittest.main()
