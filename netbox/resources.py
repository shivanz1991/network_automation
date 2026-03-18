"""
Idempotent NetBox resource helpers (ensure_* pattern).

Every function follows the same contract:
  1. Check if the object already exists (GET with filters).
  2. If yes, return it unchanged.
  3. If no, create it (POST) and return the new object.

This makes every call safe to re-run without side-effects.
"""


def ensure_region(nb, region_name):
    slug = region_name.lower()
    existing = nb.get_or_none("dcim/regions/", slug=slug)
    if existing:
        return existing
    result = nb.post("dcim/regions/", {"name": region_name, "slug": slug})
    print(f"  + Region: {region_name}")
    return result


def ensure_site_group(nb, name):
    slug = name.lower()
    existing = nb.get_or_none("dcim/site-groups/", slug=slug)
    if existing:
        return existing
    result = nb.post("dcim/site-groups/", {"name": name, "slug": slug})
    print(f"  + Site Group: {name}")
    return result


def ensure_site(nb, site_code, region_id, group_id=None):
    slug = site_code.lower()
    existing = nb.get_or_none("dcim/sites/", slug=slug)
    if existing:
        return existing
    data = {
        "name": site_code, "slug": slug, "region": region_id, "status": "planned",
    }
    if group_id:
        data["group"] = group_id
    result = nb.post("dcim/sites/", data)
    print(f"  + Site: {site_code}")
    return result


def ensure_rir(nb):
    existing = nb.get_or_none("ipam/rirs/", slug="rfc1918")
    if existing:
        return existing
    return nb.post("ipam/rirs/", {
        "name": "RFC1918", "slug": "rfc1918", "is_private": True,
    })


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
    existing = nb.get_or_none(
        "ipam/prefixes/", prefix=prefix_str, site_id=site_id,
    )
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
    return nb.post("dcim/device-roles/", {
        "name": name, "slug": slug, "color": "607d8b",
    })


def ensure_device_type(nb, model, slug):
    existing = nb.get_or_none("dcim/device-types/", slug=slug)
    if existing:
        return existing
    manufacturer = nb.get_or_none("dcim/manufacturers/", slug="generic")
    if not manufacturer:
        manufacturer = nb.post("dcim/manufacturers/", {
            "name": "Generic", "slug": "generic",
        })
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
