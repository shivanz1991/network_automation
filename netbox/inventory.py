"""
Inventory loader -- reads desired state from YAML files.

Reads:
  - inventory/sites/sites.yml          Site registry (site codes + site_ids)
  - inventory/sites/<SITE>/hosts.yml   Device list per site
"""

import os

import yaml


def load_sites(inventory_dir: str) -> dict:
    """Load all sites and their devices from the inventory directory.

    Returns a dict keyed by site code:
        {"DCAMER": {"site_id": 0, "devices": ["MGTSW1A-DCAMER", ...]}, ...}
    """
    sites_dir = os.path.join(inventory_dir, "sites")
    sites_file = os.path.join(sites_dir, "sites.yml")
    with open(sites_file) as f:
        data = yaml.safe_load(f)

    sites = {}
    for region_name, region_cfg in data.get("regions", {}).items():
        for site_code, site_id in (region_cfg.get("sites") or {}).items():
            if site_id is None:
                continue
            if site_id < 0 or site_id > 190:
                raise ValueError(
                    f"Site {site_code}: site_id must be 0-190, got {site_id}"
                )
            if site_id % 2 != 0:
                raise ValueError(
                    f"Site {site_code}: site_id must be even, got {site_id}"
                )

            hosts_file = os.path.join(sites_dir, site_code, "hosts.yml")
            if not os.path.exists(hosts_file):
                raise FileNotFoundError(
                    f"Site {site_code} defined in sites.yml but "
                    f"missing {hosts_file}"
                )

            with open(hosts_file) as f:
                hosts_data = yaml.safe_load(f)

            sites[site_code] = {
                "site_id": site_id,
                "devices": hosts_data.get("devices", []),
            }

    return sites
