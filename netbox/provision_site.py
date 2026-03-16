#!/usr/bin/env python3
"""
Provision a new site in NetBox from a site_code and site_id.

All addressing is deterministically derived from the site_id using the
formulas defined in network_standards/. This script is the bridge between
the standards repo and NetBox.

Usage:
    python3 provision_site.py --site EQ4LON --site-id 64 --url http://192.168.0.36 --token <API_TOKEN>

Environment variables (alternative to CLI flags):
    NETBOX_URL    - NetBox base URL
    NETBOX_TOKEN  - API authentication token
"""

import argparse
import ipaddress
import os
import sys

import pynetbox


# ---------------------------------------------------------------------------
# Constants derived from network_standards/
# ---------------------------------------------------------------------------

REGIONS = {
    "AMER": {"start": 0,   "htcolo": "10.64.0.0/16",  "netinfra": "10.16.0.0/18"},
    "EMEA": {"start": 64,  "htcolo": "10.65.0.0/16",  "netinfra": "10.16.64.0/18"},
    "APAC": {"start": 128, "htcolo": "10.66.0.0/16",  "netinfra": "10.16.128.0/18"},
}

LOCAL_SUPERNETS = {
    "esx_vsan":       {"base": "10.204.0.0/16", "prefix_len": 24, "vlan": None},
    "esx_vmkernel":   {"base": "10.200.0.0/16", "prefix_len": 24, "vlan": None},
    "ptp":            {"base": "10.205.0.0/16", "prefix_len": 24, "vlan": 205},
    "tickpublisher":  {"base": "10.10.0.0/16",  "prefix_len": 24, "vlan": 600},
    "orderentry_nat": {"base": "10.112.0.0/16", "prefix_len": 26, "vlan": 700},
}

HTCOLO_VLANS = [
    {"offset": 0, "vid": 100, "name": "INFRA",  "prefix_len": 24},
    {"offset": 1, "vid": 110, "name": "MGMT",   "prefix_len": 24},
    {"offset": 2, "vid": 120, "name": "APP",    "prefix_len": 24},
    {"offset": 3, "vid": 130, "name": "ESX",    "prefix_len": 24},
]

INTRA_SITE_OFFSET = 4

WAN_P2P_BASE = ipaddress.IPv4Network("10.0.0.0/16")
WAN_HUBS_PER_REGION = 3

CORE_DEVICES = [
    # (hostname_prefix, role, type, cabinet, side, offset)
    ("MGTSW1A",      "MGT", "SW", 1, "A", 57),
    ("MGTSW1B",      "MGT", "SW", 1, "B", 58),
    ("INFSW1A",      "INF", "SW", 1, "A", 59),
    ("INFSW1B",      "INF", "SW", 1, "B", 60),
    ("TRDSW1A",      "TRD", "SW", 1, "A", 63),
    ("TRDSW1B",      "TRD", "SW", 1, "B", 64),
    ("TIMESERVER1A", "PTP", "SV", 1, "A", 30),
    ("TIMESERVER1B", "PTP", "SV", 1, "B", 31),
    ("PTPSW1A",      "PTP", "SW", 1, "A", 32),
    ("PTPSW1B",      "PTP", "SW", 1, "B", 33),
    ("CONSOLE1A",    "OOB", "SV", 1, "A", 34),
    ("CONSOLE1B",    "OOB", "SV", 1, "B", 35),
]

ASN_BASE = 65000


# ---------------------------------------------------------------------------
# Address derivation — mirrors the formulas in network_standards/
# ---------------------------------------------------------------------------

def get_region(site_id: int) -> tuple[str, dict]:
    for name, cfg in REGIONS.items():
        end = cfg["start"] + 63
        if cfg["start"] <= site_id <= end:
            return name, cfg
    raise ValueError(f"site_id {site_id} is outside all region ranges (0-63, 64-127, 128-191)")


def derive_addressing(site_id: int, region_cfg: dict) -> dict:
    pair_index = (site_id - region_cfg["start"]) // 2

    htcolo_net = ipaddress.IPv4Network(region_cfg["htcolo"])
    htcolo_base_int = int(htcolo_net.network_address) + (pair_index * 8 * 256)
    htcolo_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(htcolo_base_int)}/21")

    netinfra_net = ipaddress.IPv4Network(region_cfg["netinfra"])
    netinfra_base_int = int(netinfra_net.network_address) + (pair_index * 256)
    netinfra_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(netinfra_base_int)}/24")

    htcolo_vlans = []
    for v in HTCOLO_VLANS:
        vlan_base_int = htcolo_base_int + (v["offset"] * 256)
        vlan_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(vlan_base_int)}/{v['prefix_len']}")
        htcolo_vlans.append({**v, "prefix": vlan_prefix})

    intra_base_int = htcolo_base_int + (INTRA_SITE_OFFSET * 256)
    intra_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(intra_base_int)}/24")
    ibgp_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(intra_base_int)}/30")
    sw1a_ibgp = ipaddress.IPv4Address(intra_base_int + 1)
    sw1b_ibgp = ipaddress.IPv4Address(intra_base_int + 2)

    local_prefixes = {}
    for name, cfg in LOCAL_SUPERNETS.items():
        base_net = ipaddress.IPv4Network(cfg["base"])
        local_base_int = int(base_net.network_address) + (site_id * 256)
        local_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(local_base_int)}/{cfg['prefix_len']}")
        local_prefixes[name] = {"prefix": local_prefix, "vlan": cfg["vlan"]}

    return {
        "pair_index": pair_index,
        "asn": ASN_BASE + site_id,
        "htcolo_prefix": htcolo_prefix,
        "netinfra_prefix": netinfra_prefix,
        "htcolo_vlans": htcolo_vlans,
        "intra_prefix": intra_prefix,
        "ibgp_prefix": ibgp_prefix,
        "sw1a_ibgp": sw1a_ibgp,
        "sw1b_ibgp": sw1b_ibgp,
        "local_prefixes": local_prefixes,
    }


def derive_wan_p2p(site_id: int, region_cfg: dict) -> list[dict]:
    pair_index = (site_id - region_cfg["start"]) // 2
    region_offset = region_cfg["start"]
    links = []

    for hub_index in range(WAN_HUBS_PER_REGION):
        for side, side_offset in [("A", 0), ("B", 32)]:
            third_octet = region_offset + side_offset + hub_index
            fourth_octet = pair_index * 4
            base_int = int(WAN_P2P_BASE.network_address) + (third_octet * 256) + fourth_octet
            p2p_prefix = ipaddress.IPv4Network(f"{ipaddress.IPv4Address(base_int)}/30")
            hub_ip = ipaddress.IPv4Address(base_int + 1)
            colo_ip = ipaddress.IPv4Address(base_int + 2)
            links.append({
                "hub_index": hub_index,
                "side": side,
                "prefix": p2p_prefix,
                "hub_ip": hub_ip,
                "colo_ip": colo_ip,
            })

    return links


def compute_device_ips(addressing: dict) -> dict:
    """Compute management IPs for each core device on the INFRA /24."""
    infra_prefix = addressing["htcolo_vlans"][0]["prefix"]  # offset +0 = INFRA
    broadcast_int = int(infra_prefix.broadcast_address)
    device_ips = {}
    for dev in CORE_DEVICES:
        hostname_prefix, _, _, _, _, offset = dev
        ip = ipaddress.IPv4Address(broadcast_int - offset)
        device_ips[hostname_prefix] = ip
    return device_ips


# ---------------------------------------------------------------------------
# NetBox provisioning
# ---------------------------------------------------------------------------

def get_or_create_rir(nb):
    rir = nb.ipam.rirs.get(slug="rfc1918")
    if not rir:
        rir = nb.ipam.rirs.create(name="RFC1918", slug="rfc1918", is_private=True)
        print(f"  Created RIR: {rir}")
    return rir


def get_or_create_role(nb, name, slug):
    role = nb.dcim.device_roles.get(slug=slug)
    if not role:
        role = nb.dcim.device_roles.create(name=name, slug=slug, color="607d8b")
        print(f"  Created device role: {role}")
    return role


def get_or_create_device_type(nb, model, slug):
    dt = nb.dcim.device_types.get(slug=slug)
    if not dt:
        manufacturer = nb.dcim.manufacturers.get(slug="generic")
        if not manufacturer:
            manufacturer = nb.dcim.manufacturers.create(name="Generic", slug="generic")
        dt = nb.dcim.device_types.create(
            manufacturer=manufacturer.id,
            model=model,
            slug=slug,
        )
        print(f"  Created device type: {dt}")
    return dt


def get_or_create_prefix_role(nb, name, slug):
    role = nb.ipam.roles.get(slug=slug)
    if not role:
        role = nb.ipam.roles.create(name=name, slug=slug)
        print(f"  Created prefix role: {role}")
    return role


def provision(nb, site_code: str, site_id: int, dry_run: bool = False):
    region_name, region_cfg = get_region(site_id)
    addr = derive_addressing(site_id, region_cfg)
    wan_links = derive_wan_p2p(site_id, region_cfg)
    device_ips = compute_device_ips(addr)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Provisioning site: {site_code}")
    print(f"  Region:      {region_name}")
    print(f"  site_id:     {site_id}")
    print(f"  ASN:         {addr['asn']}")
    print(f"  htcolo /21:  {addr['htcolo_prefix']}")
    print(f"  netinfra:    {addr['netinfra_prefix']}")
    print(f"  iBGP SW1A:   {addr['sw1a_ibgp']}")
    print(f"  iBGP SW1B:   {addr['sw1b_ibgp']}")
    print()

    for name, lp in addr["local_prefixes"].items():
        print(f"  {name:20s} {lp['prefix']}")
    print()

    print(f"  WAN P2P links ({len(wan_links)}):")
    for link in wan_links:
        print(f"    Hub{link['hub_index']}-{link['side']}: {link['prefix']}  hub={link['hub_ip']}  colo={link['colo_ip']}")
    print()

    print(f"  Core devices ({len(CORE_DEVICES)}):")
    for dev in CORE_DEVICES:
        hostname = f"{dev[0]}-{site_code}"
        ip = device_ips[dev[0]]
        print(f"    {hostname:25s} {ip}")
    print()

    if dry_run:
        print("[DRY RUN] No changes made to NetBox.")
        return

    # --- Create region if needed ---
    nb_region = nb.dcim.regions.get(slug=region_name.lower())
    if not nb_region:
        nb_region = nb.dcim.regions.create(name=region_name, slug=region_name.lower())
        print(f"  Created region: {nb_region}")

    # --- Create site ---
    site_slug = site_code.lower()
    nb_site = nb.dcim.sites.get(slug=site_slug)
    if nb_site:
        print(f"  Site already exists: {nb_site}")
    else:
        nb_site = nb.dcim.sites.create(
            name=site_code,
            slug=site_slug,
            region=nb_region.id,
            status="planned",
            custom_fields={"site_id": site_id} if _has_custom_field(nb, "site_id") else {},
        )
        print(f"  Created site: {nb_site}")

    # --- Create ASN ---
    rir = get_or_create_rir(nb)
    existing_asn = nb.ipam.asns.get(asn=addr["asn"])
    if existing_asn:
        print(f"  ASN already exists: {existing_asn}")
    else:
        nb_asn = nb.ipam.asns.create(asn=addr["asn"], rir=rir.id)
        nb_asn.sites = [nb_site.id]
        nb_asn.save()
        print(f"  Created ASN: {nb_asn}")

    # --- VLAN group ---
    vg_slug = f"{site_slug}-vlans"
    vlan_group = nb.ipam.vlan_groups.get(slug=vg_slug)
    if not vlan_group:
        vlan_group = nb.ipam.vlan_groups.create(
            name=f"{site_code} VLANs",
            slug=vg_slug,
            scope_type="dcim.site",
            scope_id=nb_site.id,
        )
        print(f"  Created VLAN group: {vlan_group}")

    # --- Prefix roles ---
    role_htcolo = get_or_create_prefix_role(nb, "htcolo", "htcolo")
    role_netinfra = get_or_create_prefix_role(nb, "netinfra", "netinfra")
    role_local = get_or_create_prefix_role(nb, "local", "local")
    role_p2p = get_or_create_prefix_role(nb, "wan-p2p", "wan-p2p")
    role_intra = get_or_create_prefix_role(nb, "intra-site", "intra-site")

    # --- htcolo /21 parent ---
    _create_prefix(nb, str(addr["htcolo_prefix"]), nb_site, role_htcolo,
                   f"{site_code} htcolo")

    # --- htcolo VLAN /24s ---
    for v in addr["htcolo_vlans"]:
        nb_vlan = _get_or_create_vlan(nb, vlan_group, v["vid"], v["name"])
        _create_prefix(nb, str(v["prefix"]), nb_site, role_htcolo,
                       f"{site_code} {v['name']}", vlan=nb_vlan)

    # --- Intra-site /24 ---
    _create_prefix(nb, str(addr["intra_prefix"]), nb_site, role_intra,
                   f"{site_code} INTRA-SITE")

    # --- iBGP /30 and IPs ---
    _create_prefix(nb, str(addr["ibgp_prefix"]), nb_site, role_intra,
                   f"{site_code} iBGP SW1A-SW1B")
    _create_ip(nb, f"{addr['sw1a_ibgp']}/30", f"MGTSW1A-{site_code} iBGP")
    _create_ip(nb, f"{addr['sw1b_ibgp']}/30", f"MGTSW1B-{site_code} iBGP")

    # --- netinfra /24 ---
    _create_prefix(nb, str(addr["netinfra_prefix"]), nb_site, role_netinfra,
                   f"{site_code} netinfra")

    # --- Local supernets ---
    for name, lp in addr["local_prefixes"].items():
        nb_vlan = None
        if lp["vlan"]:
            nb_vlan = _get_or_create_vlan(nb, vlan_group, lp["vlan"], name.upper())
        _create_prefix(nb, str(lp["prefix"]), nb_site, role_local,
                       f"{site_code} {name}", vlan=nb_vlan)

    # --- WAN P2P /30s ---
    for link in wan_links:
        desc = f"{site_code} Hub{link['hub_index']}-{link['side']}"
        _create_prefix(nb, str(link["prefix"]), nb_site, role_p2p, desc)
        _create_ip(nb, f"{link['hub_ip']}/30", f"Hub{link['hub_index']}-{link['side']} → {site_code}")
        _create_ip(nb, f"{link['colo_ip']}/30", f"{site_code} → Hub{link['hub_index']}-{link['side']}")

    # --- VLANs without htcolo prefixes (trading switch VLANs) ---
    for vid, name in [(800, "FEED_A"), (801, "FEED_B"), (3000, "MGTSW_IBGP"), (3050, "TRDSW_INTERLINK"), (3100, "INFRA_AB_INTERLINK")]:
        _get_or_create_vlan(nb, vlan_group, vid, name)

    # --- Device roles ---
    roles = {
        "MGT": get_or_create_role(nb, "Management", "management"),
        "TRD": get_or_create_role(nb, "Trading", "trading"),
        "INF": get_or_create_role(nb, "Infrastructure", "infrastructure"),
        "PTP": get_or_create_role(nb, "PTP", "ptp"),
        "OOB": get_or_create_role(nb, "OOB", "oob"),
    }

    # --- Device types ---
    types = {
        "SW": get_or_create_device_type(nb, "Switch", "switch"),
        "SV": get_or_create_device_type(nb, "Server", "server"),
    }

    # --- Core devices ---
    for dev in CORE_DEVICES:
        hostname_prefix, role_code, type_code, cab, side, offset = dev
        hostname = f"{hostname_prefix}-{site_code}"
        nb_dev = nb.dcim.devices.get(name=hostname)
        if nb_dev:
            print(f"  Device already exists: {nb_dev}")
            continue

        nb_dev = nb.dcim.devices.create(
            name=hostname,
            device_role=roles[role_code].id,
            device_type=types[type_code].id,
            site=nb_site.id,
            status="planned",
        )
        print(f"  Created device: {nb_dev}")

        mgmt_iface = nb.dcim.interfaces.create(
            device=nb_dev.id,
            name="Management1",
            type="1000base-t",
        )

        mgmt_ip = device_ips[hostname_prefix]
        nb_ip = _create_ip(nb, f"{mgmt_ip}/24", hostname,
                           interface_id=mgmt_iface.id, device_id=nb_dev.id)
        if nb_ip:
            nb_dev.primary_ip4 = nb_ip.id
            nb_dev.save()

    print(f"\nSite {site_code} provisioned successfully.")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _has_custom_field(nb, field_name):
    try:
        cfs = nb.extras.custom_fields.filter(name=field_name)
        return len(list(cfs)) > 0
    except Exception:
        return False


def _create_prefix(nb, prefix_str, site, role, description, vlan=None):
    existing = nb.ipam.prefixes.get(prefix=prefix_str, site_id=site.id)
    if existing:
        print(f"  Prefix exists: {prefix_str}")
        return existing

    data = {
        "prefix": prefix_str,
        "site": site.id,
        "role": role.id,
        "status": "active",
        "description": description,
    }
    if vlan:
        data["vlan"] = vlan.id

    prefix = nb.ipam.prefixes.create(**data)
    print(f"  Created prefix: {prefix_str:20s}  {description}")
    return prefix


def _create_ip(nb, address_str, description, interface_id=None, device_id=None):
    existing = list(nb.ipam.ip_addresses.filter(address=address_str))
    if existing:
        print(f"  IP exists: {address_str}")
        return existing[0]

    data = {
        "address": address_str,
        "status": "active",
        "description": description,
    }
    if interface_id:
        data["assigned_object_type"] = "dcim.interface"
        data["assigned_object_id"] = interface_id

    ip = nb.ipam.ip_addresses.create(**data)
    print(f"  Created IP: {address_str:20s}  {description}")
    return ip


def _get_or_create_vlan(nb, vlan_group, vid, name):
    existing = nb.ipam.vlans.get(group_id=vlan_group.id, vid=vid)
    if existing:
        return existing
    vlan = nb.ipam.vlans.create(
        group=vlan_group.id,
        vid=vid,
        name=name,
        status="active",
    )
    print(f"  Created VLAN: {vid} {name}")
    return vlan


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def validate_site_id(site_id: int):
    if site_id < 0 or site_id > 191:
        raise ValueError(f"site_id must be 0-191, got {site_id}")
    if site_id % 2 != 0:
        raise ValueError(f"site_id must be even (odd IDs are reserved), got {site_id}")


def main():
    parser = argparse.ArgumentParser(
        description="Provision a new site in NetBox with all derived addressing."
    )
    parser.add_argument("--site", required=True, help="Site code, e.g. EQ4LON")
    parser.add_argument("--site-id", type=int, required=True, help="Even site_id (0-190)")
    parser.add_argument("--url", default=os.environ.get("NETBOX_URL", "http://192.168.0.36"),
                        help="NetBox URL (default: $NETBOX_URL or http://192.168.0.36)")
    parser.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"),
                        help="NetBox API token (default: $NETBOX_TOKEN)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be created without making changes")
    args = parser.parse_args()

    try:
        validate_site_id(args.site_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if len(args.site) != 6:
        print(f"Warning: site code '{args.site}' is not 6 characters (expected format: EQ4LON)", file=sys.stderr)

    if args.dry_run:
        nb = None
        region_name, region_cfg = get_region(args.site_id)
        addr = derive_addressing(args.site_id, region_cfg)
        wan_links = derive_wan_p2p(args.site_id, region_cfg)
        device_ips = compute_device_ips(addr)

        print(f"\n[DRY RUN] Provisioning site: {args.site}")
        print(f"  Region:      {region_name}")
        print(f"  site_id:     {args.site_id}")
        print(f"  ASN:         {addr['asn']}")
        print(f"  htcolo /21:  {addr['htcolo_prefix']}")
        print(f"  netinfra:    {addr['netinfra_prefix']}")
        print(f"  iBGP SW1A:   {addr['sw1a_ibgp']}")
        print(f"  iBGP SW1B:   {addr['sw1b_ibgp']}")
        print()
        for name, lp in addr["local_prefixes"].items():
            print(f"  {name:20s} {lp['prefix']}")
        print()
        print(f"  WAN P2P links ({len(wan_links)}):")
        for link in wan_links:
            print(f"    Hub{link['hub_index']}-{link['side']}: {link['prefix']}  hub={link['hub_ip']}  colo={link['colo_ip']}")
        print()
        print(f"  Core devices ({len(CORE_DEVICES)}):")
        for dev in CORE_DEVICES:
            hostname = f"{dev[0]}-{args.site}"
            ip = device_ips[dev[0]]
            print(f"    {hostname:25s} {ip}")
        print()
        print("[DRY RUN] No changes made to NetBox.")
        return

    if not args.token:
        print("Error: --token or NETBOX_TOKEN environment variable required", file=sys.stderr)
        sys.exit(1)

    nb = pynetbox.api(args.url, token=args.token)
    provision(nb, args.site, args.site_id)


if __name__ == "__main__":
    main()
