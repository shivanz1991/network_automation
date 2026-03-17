"""Unit tests for netbox.addressing -- pure derivation, no mocking needed."""

import ipaddress
import unittest

from netbox.addressing import (
    derive_site_addressing,
    derive_wan_p2p,
    get_region,
    parse_device_name,
)


class TestGetRegion(unittest.TestCase):

    def test_amer(self):
        name, cfg = get_region(0)
        self.assertEqual(name, "AMER")
        self.assertEqual(cfg["start"], 0)

    def test_emea(self):
        name, _ = get_region(64)
        self.assertEqual(name, "EMEA")

    def test_apac(self):
        name, _ = get_region(128)
        self.assertEqual(name, "APAC")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            get_region(192)


class TestParseDeviceName(unittest.TestCase):

    def test_mgtsw1a(self):
        dev = parse_device_name("MGTSW1A")
        self.assertEqual(dev["prefix"], "MGTSW")
        self.assertEqual(dev["role"], "Management")
        self.assertEqual(dev["cabinet"], 1)
        self.assertEqual(dev["side"], "A")
        self.assertEqual(dev["side_offset"], 0)

    def test_trdsw2b(self):
        dev = parse_device_name("TRDSW2B")
        self.assertEqual(dev["prefix"], "TRDSW")
        self.assertEqual(dev["role"], "Trading")
        self.assertEqual(dev["cabinet"], 2)
        self.assertEqual(dev["side"], "B")
        self.assertEqual(dev["side_offset"], 1)

    def test_timeserver1a(self):
        dev = parse_device_name("TIMESERVER1A")
        self.assertEqual(dev["prefix"], "TIMESERVER")
        self.assertEqual(dev["role"], "PTP")
        self.assertEqual(dev["type"], "Server")

    def test_console1b(self):
        dev = parse_device_name("CONSOLE1B")
        self.assertEqual(dev["role"], "OOB")

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            parse_device_name("FOOBAR1A")


class TestDeriveAddressing(unittest.TestCase):

    def test_dcamer_site_id_0(self):
        _, region_cfg = get_region(0)
        addr = derive_site_addressing(0, region_cfg)
        self.assertEqual(addr["asn"], 65000)
        self.assertEqual(addr["htcolo_prefix"], ipaddress.IPv4Network("10.64.0.0/21"))
        self.assertEqual(addr["netinfra_prefix"], ipaddress.IPv4Network("10.16.0.0/24"))
        self.assertEqual(addr["ibgp_a"], ipaddress.IPv4Address("10.64.4.1"))
        self.assertEqual(addr["ibgp_b"], ipaddress.IPv4Address("10.64.4.2"))

    def test_dcemea_site_id_64(self):
        _, region_cfg = get_region(64)
        addr = derive_site_addressing(64, region_cfg)
        self.assertEqual(addr["asn"], 65064)
        self.assertEqual(addr["htcolo_prefix"], ipaddress.IPv4Network("10.65.0.0/21"))
        self.assertEqual(addr["netinfra_prefix"], ipaddress.IPv4Network("10.16.64.0/24"))

    def test_second_amer_site(self):
        _, region_cfg = get_region(2)
        addr = derive_site_addressing(2, region_cfg)
        self.assertEqual(addr["asn"], 65002)
        self.assertEqual(addr["htcolo_prefix"], ipaddress.IPv4Network("10.64.8.0/21"))

    def test_vlan_prefixes_count(self):
        _, region_cfg = get_region(0)
        addr = derive_site_addressing(0, region_cfg)
        self.assertEqual(len(addr["vlan_prefixes"]), 4)

    def test_local_supernets_present(self):
        _, region_cfg = get_region(0)
        addr = derive_site_addressing(0, region_cfg)
        self.assertIn("esx_vsan", addr["local_prefixes"])
        self.assertIn("ptp", addr["local_prefixes"])


class TestDeriveWanP2P(unittest.TestCase):

    def test_link_count(self):
        _, region_cfg = get_region(0)
        links = derive_wan_p2p(0, region_cfg)
        self.assertEqual(len(links), 6)

    def test_links_are_slash30(self):
        _, region_cfg = get_region(0)
        links = derive_wan_p2p(0, region_cfg)
        for link in links:
            self.assertEqual(link["prefix"].prefixlen, 30)

    def test_hub_and_colo_in_same_prefix(self):
        _, region_cfg = get_region(0)
        links = derive_wan_p2p(0, region_cfg)
        for link in links:
            self.assertIn(link["hub_ip"], link["prefix"])
            self.assertIn(link["colo_ip"], link["prefix"])


if __name__ == "__main__":
    unittest.main()
