"""
Reconciliation engine -- compares desired state against NetBox and applies diffs.

Handles three operations:
  - Provision: create sites/prefixes/devices that should exist
  - Decommission: remove sites that are no longer in the inventory
  - Dry-run: preview what would change without touching NetBox
"""

from netbox.addressing import (
    derive_device_ips,
    derive_site_addressing,
    derive_wan_p2p,
    get_region,
    parse_device_name,
)
from netbox.constants import SITE_VLANS
from netbox.resources import (
    ensure_asn,
    ensure_device,
    ensure_device_role,
    ensure_device_type,
    ensure_ip,
    ensure_prefix,
    ensure_prefix_role,
    ensure_region,
    ensure_rir,
    ensure_site,
    ensure_vlan,
    ensure_vlan_group,
)


def provision_site(nb, site_code: str, site_id: int, device_names: list):
    """Create or verify all NetBox objects for a single site."""
    region_name, region_cfg = get_region(site_id)
    addr = derive_site_addressing(site_id, region_cfg)
    wan_links = derive_wan_p2p(site_id, region_name, region_cfg)

    print(
        f"\n--- {site_code} "
        f"(site_id={site_id}, {region_name}, ASN {addr['asn']}) ---"
    )

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

    ensure_prefix(
        nb, str(addr["htcolo_prefix"]), nb_site["id"],
        role_htcolo["id"], f"{site_code} htcolo",
    )

    for vp in addr["vlan_prefixes"]:
        ensure_prefix(
            nb, str(vp["prefix"]), nb_site["id"], role_htcolo["id"],
            f"{site_code} {vp['name']}", vlan_id=vlan_map[vp["vid"]]["id"],
        )

    ensure_prefix(
        nb, str(addr["intra_prefix"]), nb_site["id"],
        role_intra["id"], f"{site_code} INTRA-SITE",
    )
    ensure_prefix(
        nb, str(addr["ibgp_prefix"]), nb_site["id"],
        role_intra["id"], f"{site_code} iBGP INFSW1A-INFSW1B",
    )
    ensure_ip(nb, f"{addr['ibgp_a']}/30", f"INFSW1A-{site_code} iBGP")
    ensure_ip(nb, f"{addr['ibgp_b']}/30", f"INFSW1B-{site_code} iBGP")

    ensure_prefix(
        nb, str(addr["netinfra_prefix"]), nb_site["id"],
        role_netinfra["id"], f"{site_code} netinfra",
    )

    for name, prefix in addr["local_prefixes"].items():
        ensure_prefix(
            nb, str(prefix), nb_site["id"],
            role_local["id"], f"{site_code} {name}",
        )

    for link in wan_links:
        wan_vlan = ensure_vlan(
            nb, vlan_group["id"], link["vlan_id"],
            f"WAN_{site_code}_{link['side']}",
        )
        desc = f"{site_code} Hub-{link['side']} VLAN {link['vlan_id']}"
        ensure_prefix(
            nb, str(link["prefix"]), nb_site["id"], role_p2p["id"], desc,
            vlan_id=wan_vlan["id"],
        )
        ensure_ip(
            nb, f"{link['hub_ip']}/30",
            f"Hub-{link['side']} -> {site_code}",
        )
        ensure_ip(
            nb, f"{link['colo_ip']}/30",
            f"{site_code} -> Hub-{link['side']}",
        )

    roles_cache = {}
    types_cache = {}

    for device_name in device_names:
        if "-" in device_name:
            hostname = device_name
            device_prefix = device_name.rsplit("-", 1)[0]
        else:
            hostname = f"{device_name}-{site_code}"
            device_prefix = device_name
        dev = parse_device_name(device_prefix)

        role_slug = dev["role"].lower()
        if role_slug not in roles_cache:
            roles_cache[role_slug] = ensure_device_role(
                nb, dev["role"], role_slug,
            )

        type_slug = dev["type"].lower()
        if type_slug not in types_cache:
            types_cache[type_slug] = ensure_device_type(
                nb, dev["type"], type_slug,
            )

        nb_dev, created = ensure_device(
            nb, hostname, roles_cache[role_slug]["id"],
            types_cache[type_slug]["id"], nb_site["id"],
        )

        if created:
            nb.post("dcim/interfaces/", {
                "device": nb_dev["id"],
                "name": "Management1",
                "type": "1000base-t",
            })
            device_ips = derive_device_ips(device_prefix, addr["vlan_prefixes"])
            mgmt_vlan = device_ips.get(110)
            if mgmt_vlan:
                ensure_ip(
                    nb, f"{mgmt_vlan['svi_ip']}/24", f"{hostname} MGMT SVI",
                )


def decommission_site(nb, site_code: str):
    """Remove a site and all associated objects from NetBox."""
    print(f"\n--- Removing: {site_code} ---")
    nb_site = nb.get_or_none("dcim/sites/", slug=site_code.lower())
    if not nb_site:
        print(f"  Site {site_code} not found in NetBox, skipping")
        return

    site_nb_id = nb_site["id"]

    devices = nb.get(
        "dcim/devices/", params={"site_id": site_nb_id, "limit": 200},
    )
    for dev in devices.get("results", []):
        ips = nb.get(
            "ipam/ip-addresses/",
            params={"device_id": dev["id"], "limit": 200},
        )
        for ip in ips.get("results", []):
            nb.delete(f"ipam/ip-addresses/{ip['id']}/")
            print(f"  - IP: {ip['address']} ({ip.get('description', '')})")

        ifaces = nb.get(
            "dcim/interfaces/",
            params={"device_id": dev["id"], "limit": 200},
        )
        for iface in ifaces.get("results", []):
            nb.delete(f"dcim/interfaces/{iface['id']}/")

        nb.delete(f"dcim/devices/{dev['id']}/")
        print(f"  - Device: {dev['name']}")

    prefixes = nb.get(
        "ipam/prefixes/", params={"site_id": site_nb_id, "limit": 500},
    )
    for pfx in prefixes.get("results", []):
        nb.delete(f"ipam/prefixes/{pfx['id']}/")
        print(f"  - Prefix: {pfx['prefix']}")

    vlan_groups = nb.get(
        "ipam/vlan-groups/",
        params={"slug": f"{site_code.lower()}-vlans"},
    )
    for vg in vlan_groups.get("results", []):
        vlans = nb.get(
            "ipam/vlans/", params={"group_id": vg["id"], "limit": 200},
        )
        for vlan in vlans.get("results", []):
            nb.delete(f"ipam/vlans/{vlan['id']}/")
            print(f"  - VLAN: {vlan['vid']} {vlan['name']}")
        nb.delete(f"ipam/vlan-groups/{vg['id']}/")

    nb.delete(f"dcim/sites/{site_nb_id}/")
    print(f"  - Site: {site_code}")


def reconcile(nb, desired_sites: dict, dry_run: bool = False):
    """Reconcile NetBox state with the desired inventory.

    In dry-run mode, prints what would happen without making API calls.
    In live mode, provisions desired sites and decommissions removed ones.
    """
    if dry_run:
        _dry_run(desired_sites)
        return

    all_nb_sites = nb.get("dcim/sites/", params={"limit": 200})
    nb_site_slugs = {s["slug"]: s for s in all_nb_sites.get("results", [])}
    desired_slugs = {code.lower() for code in desired_sites}

    for slug, nb_site in nb_site_slugs.items():
        if slug not in desired_slugs:
            print(
                f"\n  Site '{nb_site['name']}' exists in NetBox "
                f"but not in sites.yml"
            )
            print("  To remove it, this will be handled in the branch for review")
            decommission_site(nb, nb_site["name"])

    for site_code, cfg in desired_sites.items():
        provision_site(nb, site_code, cfg["site_id"], cfg.get("devices", []))


def _dry_run(desired_sites: dict):
    """Print a summary of what would be provisioned."""
    print("\n[DRY RUN] Showing what would be provisioned:\n")
    for site_code, cfg in desired_sites.items():
        site_id = cfg["site_id"]
        devices = cfg.get("devices", [])
        region_name, region_cfg = get_region(site_id)
        addr = derive_site_addressing(site_id, region_cfg)
        wan_links = derive_wan_p2p(site_id, region_name, region_cfg)

        print(
            f"--- {site_code} "
            f"(site_id={site_id}, {region_name}, ASN {addr['asn']}) ---"
        )
        print(f"  htcolo /21:  {addr['htcolo_prefix']}")
        print(f"  netinfra:    {addr['netinfra_prefix']}")
        print(f"  iBGP:        {addr['ibgp_a']} <-> {addr['ibgp_b']}")
        for name, prefix in addr["local_prefixes"].items():
            print(f"  {name:20s} {prefix}")
        if wan_links:
            print(f"  WAN P2P:     {len(wan_links)} links")
            for link in wan_links:
                print(
                    f"    {link['side']}-side  VLAN {link['vlan_id']}  "
                    f"{link['prefix']}  hub {link['hub_ip']}  colo {link['colo_ip']}"
                )
        else:
            print(f"  WAN P2P:     hub site (no colo links)")
        print(f"  Devices:     {', '.join(devices)}")
        print()
