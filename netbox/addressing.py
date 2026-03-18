"""
Pure IP / ASN derivation functions.

Every function here is deterministic: given a site_id and region config,
it returns derived addresses with zero side-effects and zero network calls.
This makes the module trivially unit-testable.
"""

import ipaddress

from netbox.constants import (
    ASN_BASE,
    DEVICE_CATALOG,
    HTCOLO_VLANS,
    INTRA_SITE_OFFSET,
    LOCAL_SUPERNETS,
    REGION_INDEX,
    REGIONS,
    SVI_OFFSETS,
    WAN_P2P_BASE,
    WAN_VLAN_BASE,
)


def get_region(site_id: int) -> tuple:
    """Return (region_name, region_config) for a given site_id."""
    for name, cfg in REGIONS.items():
        if cfg["start"] <= site_id <= cfg["start"] + 63:
            return name, cfg
    raise ValueError(
        f"site_id {site_id} outside valid ranges (0-63, 64-127, 128-191)"
    )


def derive_site_addressing(site_id: int, region_cfg: dict) -> dict:
    """Derive all IP prefixes and addresses for a site."""
    pair_index = (site_id - region_cfg["start"]) // 2

    htcolo_net = ipaddress.IPv4Network(region_cfg["htcolo"])
    htcolo_base = int(htcolo_net.network_address) + (pair_index * 8 * 256)
    htcolo_prefix = ipaddress.IPv4Network(
        f"{ipaddress.IPv4Address(htcolo_base)}/21"
    )

    netinfra_net = ipaddress.IPv4Network(region_cfg["netinfra"])
    netinfra_base = int(netinfra_net.network_address) + (pair_index * 256)
    netinfra_prefix = ipaddress.IPv4Network(
        f"{ipaddress.IPv4Address(netinfra_base)}/24"
    )

    vlan_prefixes = []
    for v in HTCOLO_VLANS:
        vlan_base = htcolo_base + (v["offset"] * 256)
        vlan_prefix = ipaddress.IPv4Network(
            f"{ipaddress.IPv4Address(vlan_base)}/{v['prefix_len']}"
        )
        vlan_prefixes.append({**v, "prefix": vlan_prefix})

    intra_base = htcolo_base + (INTRA_SITE_OFFSET * 256)
    intra_prefix = ipaddress.IPv4Network(
        f"{ipaddress.IPv4Address(intra_base)}/24"
    )
    ibgp_prefix = ipaddress.IPv4Network(
        f"{ipaddress.IPv4Address(intra_base)}/30"
    )

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
    """Derive SVI and management IPs for a device on each VLAN."""
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


def derive_wan_p2p(site_id: int, region_name: str, region_cfg: dict) -> list:
    """Derive WAN point-to-point /30 links for a site (1 hub per region)."""
    pair_index = (site_id - region_cfg["start"]) // 2
    if pair_index == 0:
        return []
    region_idx = REGION_INDEX[region_name]
    base_net = int(WAN_P2P_BASE.network_address)
    links = []
    for side, side_offset in [("A", 0), ("B", 1)]:
        third_octet = (region_idx * 2) + side_offset
        fourth_octet = pair_index * 4
        base = base_net + (third_octet * 256) + fourth_octet
        vlan_id = WAN_VLAN_BASE + site_id + side_offset
        links.append({
            "side": side,
            "vlan_id": vlan_id,
            "prefix": ipaddress.IPv4Network(
                f"{ipaddress.IPv4Address(base)}/30"
            ),
            "hub_ip": ipaddress.IPv4Address(base + 1),
            "colo_ip": ipaddress.IPv4Address(base + 2),
        })
    return links


def parse_device_name(name: str) -> dict:
    """Parse a device prefix like INFSW1A into structured components."""
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
                "base_offset": catalog["offsets"].get(
                    cabinet, catalog["offsets"][1]
                ),
                "side_offset": 0 if side == "A" else 1,
            }
    raise ValueError(f"Unknown device name: {name}")
