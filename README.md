# Network Automation

This repository contains all automation code for provisioning, configuring,
and managing network infrastructure. It is the operational counterpart to the
[network_standards](../network_standards/) repository, which defines the
formulas and conventions — this repo implements them.

## Architecture

```
network_standards/     Defines the "what" — formulas, naming, addressing
        │
        ▼
network_automation/    Implements the "how" — scripts, templates, playbooks
        │
        ▼
    NetBox             Single source of truth for all network data
        │
        ▼
    Devices            Configs generated and pushed via Ansible
```

## Directory Structure

```
network_automation/
├── netbox/            NetBox provisioning and management scripts
│   └── provision_site.py
├── templates/         Jinja2 config templates for network devices
├── playbooks/         Ansible playbooks for config deployment
├── scripts/           Standalone utility scripts (validation, audit, etc.)
└── requirements.txt   Python dependencies
```

### netbox/

Scripts that create, update, or query data in NetBox via the API.
NetBox is the single source of truth — these scripts are the only
sanctioned way to bulk-provision network data.

| Script | Purpose |
|--------|---------|
| `provision_site.py` | Provision a new site with all derived addressing, VLANs, devices, and IPs |

**Usage:**

```bash
# Preview what will be created (no changes)
python3 netbox/provision_site.py --site EQ4LON --site-id 64 --dry-run

# Provision for real
python3 netbox/provision_site.py --site EQ4LON --site-id 64 --token $NETBOX_TOKEN
```

**Environment variables:**

| Variable | Purpose | Default |
|----------|---------|---------|
| `NETBOX_URL` | NetBox base URL | `http://192.168.0.36` |
| `NETBOX_TOKEN` | API authentication token | — |

### templates/

Jinja2 templates that generate device configurations (Arista EOS).
Templates pull variables from NetBox at render time — no hardcoded IPs
or site-specific data.

Planned templates:
- `mgtsw.j2` — Management switch (MGTSW) full config
- `trdsw.j2` — Trading switch config
- `infsw.j2` — Infrastructure switch config

### playbooks/

Ansible playbooks that orchestrate config generation and deployment.
All inventory comes from NetBox via the `netbox.netbox.nb_inventory` plugin.

Planned playbooks:
- `site_deploy.yml` — Generate and push configs for all devices at a site
- `vlan_update.yml` — Push VLAN changes across a site
- `validate.yml` — Pre/post change validation

### scripts/

Standalone utilities that don't fit neatly into NetBox or Ansible:
- Config diff/audit tools
- Pre-change validation scripts
- IP addressing calculators

## Standards Reference

All addressing formulas, naming conventions, VLAN assignments, and
design decisions are documented in the `network_standards/` repository.
This repo implements those standards — it does not redefine them.

Key standards documents:

| Document | What it defines |
|----------|-----------------|
| `sites.md` | Site naming convention ({DC}{CITY}), site registry, ASN assignment |
| `devices.md` | Device naming ({ROLE}{TYPE}{CAB}{SIDE}-{SITE}), IP offsets, L2/L3 behavior |
| `ip-addressing/site-addressing.md` | Per-site htcolo /21 and netinfra /24 derivation |
| `ip-addressing/wan-p2p.md` | Hub-to-colo /30 links |
| `ip-addressing/wan-regional.md` | Inter-region WAN /30 links (10.255.0.0/20) |
| `vlans/standard-vlans.md` | VLAN ID assignments |
| `automation.md` | Pipeline architecture (NetBox → Ansible) |

## Conventions

- **Never hardcode IPs** — derive from site_id using the formulas in network_standards
- **Never manually edit NetBox** for bulk operations — use the scripts in `netbox/`
- **NetBox is the source of truth** — Ansible queries it at runtime, not static vars
- **Dry-run first** — all scripts support `--dry-run` to preview changes
- **One site_id, everything derived** — a single even integer (0–190) determines all addressing
