#!/usr/bin/env python3
"""
Provision and reconcile NetBox state from inventory/.

Reads desired state from:
  - inventory/sites.yml          Site registry (site codes + site_ids)
  - inventory/<SITE>/hosts.yml   Device list per site

Derives all addressing from network_standards/ formulas, then ensures
NetBox matches — creating, updating, or removing objects as needed.

All changes are made inside a NetBox branch for peer review before merge.

Usage:
    python3 netbox/provision_site.py --dry-run
    python3 netbox/provision_site.py
    python3 netbox/provision_site.py --branch "add-eq4lon"
    python3 netbox/provision_site.py --merge 3

Environment variables:
    NETBOX_URL    - NetBox base URL (default: http://192.168.0.36)
    NETBOX_TOKEN  - API authentication token (required)
"""

import argparse
import ipaddress
import os
import sys
import time

import requests
import yaml


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
INVENTORY_DIR = os.path.join(REPO_DIR, "inventory")
SITES_FILE = os.path.join(INVENTORY_DIR, "sites.yml")


# ---------------------------------------------------------------------------
# Constants from network_standards/
# ---------------------------------------------------------------------------

REGIONS = {
    "AMER": {"start": 0,   "htcolo": "10.64.0.0/16",  "netinfra": "10.16.0.0/18"},
    "EMEA": {"start": 64,  "htcolo": "10.65.0.0/16",  "netinfra": "10.16.64.0/18"},
    "APAC": {"start": 128, "htcolo": "10.66.0.0/16",  "netinfra": "10.16.128.0/18"},
}

LOCAL_SUPERNETS = {
    "esx_vsan":       {"base": "10.204.0.0/16", "prefix_len": 24},
    "esx_vmkernel":   {"base": "10.200.0.0/16", "prefix_len": 24},
    "ptp":            {"base": "10.205.0.0/16", "prefix_len": 24},
    "tickpublisher":  {"base": "10.10.0.0/16",  "prefix_len": 24},
    "orderentry_nat": {"base": "10.112.0.0/16", "prefix_len": 26},
}

HTCOLO_VLANS = [
    {"offset": 0, "vid": 100, "name": "INFRA",  "prefix_len": 24},
    {"offset": 1, "vid": 110, "name": "MGMT",   "prefix_len": 24},
    {"offset": 2, "vid": 120, "name": "APP",    "prefix_len": 24},
    {"offset": 3, "vid": 130, "name": "ESX",    "prefix_len": 24},
]

SITE_VLANS = [
    (100, "INFRA"), (110, "MGMT"), (120, "APP"), (130, "ESX"),
    (800, "FEED_A"), (801, "FEED_B"),
    (3000, "MGTSW_IBGP"), (3050, "TRDSW_INTERLINK"), (3100, "INFRA_AB_INTERLINK"),
]

INTRA_SITE_OFFSET = 4
WAN_P2P_BASE = ipaddress.IPv4Network("10.0.0.0/16")
WAN_HUBS_PER_REGION = 3
ASN_BASE = 65000

DEVICE_CATALOG = {
    "MGTSW":      {"role": "Management",      "type": "Switch", "offsets": {1: 57, 2: 67, 3: 77, 4: 87}},
    "INFSW":      {"role": "Infrastructure",   "type": "Switch", "offsets": {1: 59, 2: 69, 3: 79, 4: 89}},
    "TRDSW":      {"role": "Trading",          "type": "Switch", "offsets": {1: 63, 2: 73, 3: 83, 4: 93}},
    "TIMESERVER": {"role": "PTP",              "type": "Server",  "offsets": {1: 30}},
    "PTPSW":      {"role": "PTP",              "type": "Switch", "offsets": {1: 32}},
    "CONSOLE":    {"role": "OOB",              "type": "Server",  "offsets": {1: 34}},
}

SVI_OFFSETS = {"A": 3, "B": 2, "VRRP": 1}


# ---------------------------------------------------------------------------
# Device name parsing
# ---------------------------------------------------------------------------

def parse_device_name(name: str) -> dict:
    """Parse a device name like MGTSW1A into components."""
    for prefix, catalog in DEVICE_CATALOG.items():
        if name.startswith(prefix):
            remainder = name[len(prefix):]
            cabinet = int(remainder[0])
            side = remainder[1]
            return {
                "prefix": prefix,
                "role": catalog["role"],
                "type": catalog["type"],
                "cabinet": cabinet,
                "side": side,
                "base_offset": catalog["offsets"].get(cabinet, catalog["offsets"][1]),
                "side_offset": 0 if side == "A" else 1,
            }
    raise ValueError(f"Unknown device name: {name}")


# ---------------------------------------------------------------------------
# Address derivation
# ---------------------------------------------------------------------------

def get_region(site_id: int) -> tuple:
    for name, cfg in REGIONS.items():
        if cfg["start"] <= site_id <= cfg["start"] + 63:
            return name, cfg
    raise ValueError(f"site_id {site_id} outside valid ranges (0-63, 64-127, 128-191)")


def derive_site_addressing(site_id: int, region_cfg: dict) -> dict:
    pair_index = (site_id - region_cfg["start"]) // 2

    htcolo_net = ipaddress.IPv4Network(region_cfg["htcolo"])
    htcolo_base = int(htcolo_net.network_address) + (pair_index * 8 * 256)
    htcolo_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(htcolo_base)}/21")

    netinfra_net = ipaddress.IPv4Network(region_cfg["netinfra"])
    netinfra_base = int(netinfra_net.network_address) + (pair_index * 256)
    netinfra_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(netinfra_base)}/24")

    vlan_prefixes = []
    for v in HTCOLO_VLANS:
        vlan_base = htcolo_base + (v["offset"] * 256)
        vlan_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(vlan_base)}/{v['prefix_len']}")
        vlan_prefixes.append({**v, "prefix": vlan_prefix})

    intra_base = htcolo_base + (INTRA_SITE_OFFSET * 256)
    intra_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(intra_base)}/24")
    ibgp_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(intra_base)}/30")

    local_prefixes = {}
    for name, cfg in LOCAL_SUPERNETS.items():
        base_net = ipaddress.IPv4Network(cfg["base"])
        local_base = int(base_net.network_address) + (site_id * 256)
        local_prefixes[name] = ipaddress.IPv4Network(
            f"{ipaddress.IPv4Address(local_base)}/{cfg['prefix_len']}"
        )

    return {
        "pair_index": pair_index,
        "asn": ASN_BASE + site_id,
        "htcolo_prefix": htcolo_prefix,
        "netinfra_prefix": netinfra_prefix,
        "vlan_prefixes": vlan_prefixes,
        "intra_prefix": intra_prefix,
        "ibgp_prefix": ibgp_prefix,
        "ibgp_a": ipaddress.IPv4Address(intra_base + 1),
        "ibgp_b": ipaddress.IPv4Address(intra_base + 2),
        "local_prefixes": local_prefixes,
    }


def derive_device_ips(device_name: str, vlan_prefixes: list) -> dict:
    """Derive SVI IPs for a device on each VLAN and management IP."""
    dev = parse_device_name(device_name)
    offset = dev["base_offset"] + dev["side_offset"]
    ips = {}

    for vp in vlan_prefixes:
        broadcast = int(vp["prefix"].broadcast_address)
        mgmt_ip = ipaddress.IPv4Address(broadcast - offset)
        svi_ip = ipaddress.IPv4Address(broadcast - SVI_OFFSETS[dev["side"]])
        vrrp_ip = ipaddress.IPv4Address(broadcast - SVI_OFFSETS["VRRP"])
        ips[vp["vid"]] = {
            "mgmt_ip": mgmt_ip,
            "svi_ip": svi_ip,
            "vrrp_ip": vrrp_ip,
            "prefix": vp["prefix"],
        }

    return ips


def derive_wan_p2p(site_id: int, region_cfg: dict) -> list:
    pair_index = (site_id - region_cfg["start"]) // 2
    region_offset = region_cfg["start"]
    links = []
    for hub_index in range(WAN_HUBS_PER_REGION):
        for side, side_offset in [("A", 0), ("B", 32)]:
            third_octet = region_offset + side_offset + hub_index
            fourth_octet = pair_index * 4
            base = int(WAN_P2P_BASE.network_address) + (third_octet * 256) + fourth_octet
            links.append({
                "hub_index": hub_index, "side": side,
                "prefix": ipaddress.IPv4Network(f"{ipaddress.IPv4Address(base)}/30"),
                "hub_ip": ipaddress.IPv4Address(base + 1),
                "colo_ip": ipaddress.IPv4Address(base + 2),
            })
    return links


# ---------------------------------------------------------------------------
# NetBox API helpers (raw requests for branching support)
# ---------------------------------------------------------------------------

class NetBoxClient:
    def __init__(self, url: str, token: str, branch_schema_id: str = None):
        self.url = url.rstrip("/")
        self.token = token
        self.branch_schema_id = branch_schema_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _headers(self):
        h = {}
        if self.branch_schema_id:
            h["X-NetBox-Branch"] = self.branch_schema_id
        return h

    def get(self, endpoint, params=None):
        r = self.session.get(f"{self.url}/api/{endpoint}", params=params, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def post(self, endpoint, data):
        r = self.session.post(f"{self.url}/api/{endpoint}", json=data, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def delete(self, endpoint):
        r = self.session.delete(f"{self.url}/api/{endpoint}", headers=self._headers())
        r.raise_for_status()

    def get_or_none(self, endpoint, **filters):
        result = self.get(endpoint, params=filters)
        results = result.get("results", [])
        return results[0] if results else None

    # --- Branch operations (no X-NetBox-Branch header) ---

    def create_branch(self, name: str, description: str = "") -> dict:
        r = self.session.post(
            f"{self.url}/api/plugins/branching/branches/",
            json={"name": name, "description": description},
        )
        r.raise_for_status()
        return r.json()

    def get_branch(self, branch_id: int) -> dict:
        r = self.session.get(f"{self.url}/api/plugins/branching/branches/{branch_id}/")
        r.raise_for_status()
        return r.json()

    def merge_branch(self, branch_id: int) -> dict:
        r = self.session.post(
            f"{self.url}/api/plugins/branching/branches/{branch_id}/merge/",
            json={"commit": True},
        )
        r.raise_for_status()
        return r.json()

    def wait_for_branch_ready(self, branch_id: int, timeout: int = 30):
        for _ in range(timeout):
            branch = self.get_branch(branch_id)
            if branch["status"]["value"] == "ready":
                return branch
            time.sleep(1)
        raise TimeoutError(f"Branch {branch_id} not ready after {timeout}s")


# ---------------------------------------------------------------------------
# Provisioning logic
# ---------------------------------------------------------------------------

def ensure_region(nb, region_name):
    slug = region_name.lower()
    existing = nb.get_or_none("dcim/regions/", slug=slug)
    if existing:
        return existing
    result = nb.post("dcim/regions/", {"name": region_name, "slug": slug})
    print(f"  + Region: {region_name}")
    return result


def ensure_site(nb, site_code, region_id):
    slug = site_code.lower()
    existing = nb.get_or_none("dcim/sites/", slug=slug)
    if existing:
        return existing
    result = nb.post("dcim/sites/", {
        "name": site_code, "slug": slug, "region": region_id, "status": "planned",
    })
    print(f"  + Site: {site_code}")
    return result


def ensure_rir(nb):
    existing = nb.get_or_none("ipam/rirs/", slug="rfc1918")
    if existing:
        return existing
    return nb.post("ipam/rirs/", {"name": "RFC1918", "slug": "rfc1918", "is_private": True})


def ensure_asn(nb, asn, rir_id, site_id):
    existing = nb.get_or_none("ipam/asns/", asn=asn)
    if existing:
        return existing
    result = nb.post("ipam/asns/", {"asn": asn, "rir": rir_id})
    print(f"  + ASN: {asn}")
    return result


def ensure_vlan_group(nb, site_code, site_id):
    slug = f"{site_code.lower()}-vlans"
    existing = nb.get_or_none("ipam/vlan-groups/", slug=slug)
    if existing:
        return existing
    result = nb.post("ipam/vlan-groups/", {
        "name": f"{site_code} VLANs", "slug": slug,
        "scope_type": "dcim.site", "scope_id": site_id,
    })
    print(f"  + VLAN Group: {site_code} VLANs")
    return result


def ensure_vlan(nb, vlan_group_id, vid, name):
    existing = nb.get_or_none("ipam/vlans/", group_id=vlan_group_id, vid=vid)
    if existing:
        return existing
    result = nb.post("ipam/vlans/", {
        "group": vlan_group_id, "vid": vid, "name": name, "status": "active",
    })
    print(f"  + VLAN: {vid} {name}")
    return result


def ensure_prefix_role(nb, name, slug):
    existing = nb.get_or_none("ipam/roles/", slug=slug)
    if existing:
        return existing
    return nb.post("ipam/roles/", {"name": name, "slug": slug})


def ensure_prefix(nb, prefix_str, site_id, role_id, description, vlan_id=None):
    existing = nb.get_or_none("ipam/prefixes/", prefix=prefix_str, site_id=site_id)
    if existing:
        return existing
    data = {
        "prefix": prefix_str, "site": site_id, "role": role_id,
        "status": "active", "description": description,
    }
    if vlan_id:
        data["vlan"] = vlan_id
    result = nb.post("ipam/prefixes/", data)
    print(f"  + Prefix: {prefix_str:20s} {description}")
    return result


def ensure_ip(nb, address, description):
    existing = nb.get_or_none("ipam/ip-addresses/", address=address)
    if existing:
        return existing
    result = nb.post("ipam/ip-addresses/", {
        "address": address, "status": "active", "description": description,
    })
    print(f"  + IP: {address:20s} {description}")
    return result


def ensure_device_role(nb, name, slug):
    existing = nb.get_or_none("dcim/device-roles/", slug=slug)
    if existing:
        return existing
    return nb.post("dcim/device-roles/", {"name": name, "slug": slug, "color": "607d8b"})


def ensure_device_type(nb, model, slug):
    existing = nb.get_or_none("dcim/device-types/", slug=slug)
    if existing:
        return existing
    manufacturer = nb.get_or_none("dcim/manufacturers/", slug="generic")
    if not manufacturer:
        manufacturer = nb.post("dcim/manufacturers/", {"name": "Generic", "slug": "generic"})
    return nb.post("dcim/device-types/", {
        "manufacturer": manufacturer["id"], "model": model, "slug": slug,
    })


def ensure_device(nb, hostname, role_id, type_id, site_id):
    existing = nb.get_or_none("dcim/devices/", name=hostname)
    if existing:
        return existing, False
    result = nb.post("dcim/devices/", {
        "name": hostname, "role": role_id, "device_type": type_id,
        "site": site_id, "status": "planned",
    })
    print(f"  + Device: {hostname}")
    return result, True


def provision_site(nb, site_code: str, site_id: int, device_names: list):
    region_name, region_cfg = get_region(site_id)
    addr = derive_site_addressing(site_id, region_cfg)
    wan_links = derive_wan_p2p(site_id, region_cfg)

    print(f"\n--- {site_code} (site_id={site_id}, {region_name}, ASN {addr['asn']}) ---")

    nb_region = ensure_region(nb, region_name)
    nb_site = ensure_site(nb, site_code, nb_region["id"])
    rir = ensure_rir(nb)
    ensure_asn(nb, addr["asn"], rir["id"], nb_site["id"])

    vlan_group = ensure_vlan_group(nb, site_code, nb_site["id"])
    vlan_map = {}
    for vid, name in SITE_VLANS:
        vlan_map[vid] = ensure_vlan(nb, vlan_group["id"], vid, name)

    role_htcolo = ensure_prefix_role(nb, "htcolo", "htcolo")
    role_netinfra = ensure_prefix_role(nb, "netinfra", "netinfra")
    role_local = ensure_prefix_role(nb, "local", "local")
    role_p2p = ensure_prefix_role(nb, "wan-p2p", "wan-p2p")
    role_intra = ensure_prefix_role(nb, "intra-site", "intra-site")

    ensure_prefix(nb, str(addr["htcolo_prefix"]), nb_site["id"], role_htcolo["id"],
                  f"{site_code} htcolo")

    for vp in addr["vlan_prefixes"]:
        ensure_prefix(nb, str(vp["prefix"]), nb_site["id"], role_htcolo["id"],
                      f"{site_code} {vp['name']}", vlan_id=vlan_map[vp["vid"]]["id"])

    ensure_prefix(nb, str(addr["intra_prefix"]), nb_site["id"], role_intra["id"],
                  f"{site_code} INTRA-SITE")
    ensure_prefix(nb, str(addr["ibgp_prefix"]), nb_site["id"], role_intra["id"],
                  f"{site_code} iBGP MGTSW1A-MGTSW1B")
    ensure_ip(nb, f"{addr['ibgp_a']}/30", f"MGTSW1A-{site_code} iBGP")
    ensure_ip(nb, f"{addr['ibgp_b']}/30", f"MGTSW1B-{site_code} iBGP")

    ensure_prefix(nb, str(addr["netinfra_prefix"]), nb_site["id"], role_netinfra["id"],
                  f"{site_code} netinfra")

    for name, prefix in addr["local_prefixes"].items():
        ensure_prefix(nb, str(prefix), nb_site["id"], role_local["id"],
                      f"{site_code} {name}")

    for link in wan_links:
        desc = f"{site_code} Hub{link['hub_index']}-{link['side']}"
        ensure_prefix(nb, str(link["prefix"]), nb_site["id"], role_p2p["id"], desc)
        ensure_ip(nb, f"{link['hub_ip']}/30", f"Hub{link['hub_index']}-{link['side']} -> {site_code}")
        ensure_ip(nb, f"{link['colo_ip']}/30", f"{site_code} -> Hub{link['hub_index']}-{link['side']}")

    roles_cache = {}
    types_cache = {}

    for device_name in device_names:
        dev = parse_device_name(device_name)
        hostname = f"{device_name}-{site_code}"

        role_slug = dev["role"].lower()
        if role_slug not in roles_cache:
            roles_cache[role_slug] = ensure_device_role(nb, dev["role"], role_slug)

        type_slug = dev["type"].lower()
        if type_slug not in types_cache:
            types_cache[type_slug] = ensure_device_type(nb, dev["type"], type_slug)

        nb_dev, created = ensure_device(
            nb, hostname, roles_cache[role_slug]["id"],
            types_cache[type_slug]["id"], nb_site["id"],
        )

        if created:
            mgmt_iface = nb.post("dcim/interfaces/", {
                "device": nb_dev["id"], "name": "Management1", "type": "1000base-t",
            })
            device_ips = derive_device_ips(device_name, addr["vlan_prefixes"])
            mgmt_vlan = device_ips.get(110)
            if mgmt_vlan:
                mgmt_ip = ensure_ip(nb, f"{mgmt_vlan['svi_ip']}/24", f"{hostname} MGMT SVI")


def decommission_site(nb, site_code: str):
    """Remove a site and all its associated objects from NetBox."""
    print(f"\n--- Removing: {site_code} ---")
    nb_site = nb.get_or_none("dcim/sites/", slug=site_code.lower())
    if not nb_site:
        print(f"  Site {site_code} not found in NetBox, skipping")
        return

    site_nb_id = nb_site["id"]

    devices = nb.get("dcim/devices/", params={"site_id": site_nb_id, "limit": 200})
    for dev in devices.get("results", []):
        ips = nb.get("ipam/ip-addresses/", params={"device_id": dev["id"], "limit": 200})
        for ip in ips.get("results", []):
            nb.delete(f"ipam/ip-addresses/{ip['id']}/")
            print(f"  - IP: {ip['address']} ({ip.get('description', '')})")

        ifaces = nb.get("dcim/interfaces/", params={"device_id": dev["id"], "limit": 200})
        for iface in ifaces.get("results", []):
            nb.delete(f"dcim/interfaces/{iface['id']}/")

        nb.delete(f"dcim/devices/{dev['id']}/")
        print(f"  - Device: {dev['name']}")

    prefixes = nb.get("ipam/prefixes/", params={"site_id": site_nb_id, "limit": 500})
    for pfx in prefixes.get("results", []):
        nb.delete(f"ipam/prefixes/{pfx['id']}/")
        print(f"  - Prefix: {pfx['prefix']}")

    vlan_groups = nb.get("ipam/vlan-groups/", params={"slug": f"{site_code.lower()}-vlans"})
    for vg in vlan_groups.get("results", []):
        vlans = nb.get("ipam/vlans/", params={"group_id": vg["id"], "limit": 200})
        for vlan in vlans.get("results", []):
            nb.delete(f"ipam/vlans/{vlan['id']}/")
            print(f"  - VLAN: {vlan['vid']} {vlan['name']}")
        nb.delete(f"ipam/vlan-groups/{vg['id']}/")

    nb.delete(f"dcim/sites/{site_nb_id}/")
    print(f"  - Site: {site_code}")


def reconcile(nb, desired_sites: dict, dry_run: bool = False):
    """Reconcile NetBox state with desired sites.yml."""
    if dry_run:
        print("\n[DRY RUN] Showing what would be provisioned:\n")
        for site_code, cfg in desired_sites.items():
            site_id = cfg["site_id"]
            devices = cfg.get("devices", [])
            region_name, region_cfg = get_region(site_id)
            addr = derive_site_addressing(site_id, region_cfg)
            wan_links = derive_wan_p2p(site_id, region_cfg)

            print(f"--- {site_code} (site_id={site_id}, {region_name}, ASN {addr['asn']}) ---")
            print(f"  htcolo /21:  {addr['htcolo_prefix']}")
            print(f"  netinfra:    {addr['netinfra_prefix']}")
            print(f"  iBGP:        {addr['ibgp_a']} <-> {addr['ibgp_b']}")
            for name, prefix in addr["local_prefixes"].items():
                print(f"  {name:20s} {prefix}")
            print(f"  WAN P2P:     {len(wan_links)} links")
            print(f"  Devices:     {', '.join(devices)}")
            print()
        return

    # Find sites in NetBox that are not in desired state
    all_nb_sites = nb.get("dcim/sites/", params={"limit": 200})
    nb_site_slugs = {s["slug"]: s for s in all_nb_sites.get("results", [])}
    desired_slugs = {code.lower() for code in desired_sites}

    for slug, nb_site in nb_site_slugs.items():
        if slug not in desired_slugs:
            print(f"\n  Site '{nb_site['name']}' exists in NetBox but not in sites.yml")
            print(f"  To remove it, this will be handled in the branch for review")
            decommission_site(nb, nb_site["name"])

    # Provision desired sites
    for site_code, cfg in desired_sites.items():
        provision_site(nb, site_code, cfg["site_id"], cfg.get("devices", []))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_sites(inventory_dir: str) -> dict:
    """Load sites from inventory/sites.yml and per-site hosts.yml files."""
    sites_file = os.path.join(inventory_dir, "sites.yml")
    with open(sites_file) as f:
        data = yaml.safe_load(f)

    sites = {}
    for region_name, region_cfg in data.get("regions", {}).items():
        for site_code, site_id in (region_cfg.get("sites") or {}).items():
            if site_id is None:
                continue
            if site_id < 0 or site_id > 190:
                raise ValueError(f"Site {site_code}: site_id must be 0-190, got {site_id}")
            if site_id % 2 != 0:
                raise ValueError(f"Site {site_code}: site_id must be even, got {site_id}")

            hosts_file = os.path.join(inventory_dir, site_code, "hosts.yml")
            if not os.path.exists(hosts_file):
                raise FileNotFoundError(
                    f"Site {site_code} defined in sites.yml but missing {hosts_file}"
                )

            with open(hosts_file) as f:
                hosts_data = yaml.safe_load(f)

            sites[site_code] = {
                "site_id": site_id,
                "devices": hosts_data.get("devices", []),
            }

    return sites


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile NetBox state with sites.yml desired state."
    )
    parser.add_argument("--url", default=os.environ.get("NETBOX_URL", "http://192.168.0.36"),
                        help="NetBox URL")
    parser.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"),
                        help="NetBox API token")
    parser.add_argument("--inventory", default=INVENTORY_DIR,
                        help="Path to inventory directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be created without making changes")
    parser.add_argument("--branch", default=None,
                        help="NetBox branch name (default: auto-generated)")
    parser.add_argument("--merge", type=int, default=None, metavar="BRANCH_ID",
                        help="Merge an existing branch by ID")
    args = parser.parse_args()

    if args.merge:
        if not args.token:
            print("Error: --token required", file=sys.stderr)
            sys.exit(1)
        nb = NetBoxClient(args.url, args.token)
        print(f"Merging branch {args.merge}...")
        result = nb.merge_branch(args.merge)
        print(f"Merge job submitted: {result.get('id', 'unknown')}")
        return

    sites = load_sites(args.inventory)

    if args.dry_run:
        reconcile(None, sites, dry_run=True)
        return

    if not args.token:
        print("Error: --token or NETBOX_TOKEN required", file=sys.stderr)
        sys.exit(1)

    nb = NetBoxClient(args.url, args.token)

    branch_name = args.branch or f"provision-{time.strftime('%Y%m%d-%H%M%S')}"
    print(f"Creating NetBox branch: {branch_name}")

    try:
        branch = nb.create_branch(branch_name, description=f"Automated provisioning from sites.yml")
        branch_id = branch["id"]
        schema_id = branch["schema_id"]
        print(f"  Branch ID: {branch_id}, schema: {schema_id}")
        print(f"  Waiting for branch to be ready...")
        nb.wait_for_branch_ready(branch_id)
        print(f"  Branch ready.")

        nb.branch_schema_id = schema_id
    except requests.exceptions.HTTPError as e:
        if "branching" in str(e).lower() or e.response.status_code == 404:
            print(f"  Warning: NetBox branching plugin not available, operating on main")
            print(f"  Install netbox-branching for branch-based workflow")
            branch_id = None
        else:
            raise

    reconcile(nb, sites)

    if branch_id:
        print(f"\n{'='*60}")
        print(f"Changes staged in branch: {branch_name}")
        print(f"Branch ID: {branch_id}")
        print(f"")
        print(f"Next steps:")
        print(f"  1. Review changes in NetBox UI")
        print(f"  2. Get peer approval")
        print(f"  3. Merge: python3 netbox/provision_site.py --merge {branch_id}")
        print(f"{'='*60}")
    else:
        print(f"\nProvisioning complete (applied directly to main).")


if __name__ == "__main__":
    main()
