# Network Automation

This repository contains all automation code for provisioning, configuring,
and managing network infrastructure. It is the operational counterpart to the
[network_standards](../network_standards/) repository, which defines the
formulas and conventions -- this repo implements them.

## Architecture

```
network_standards/     Defines the "what" -- formulas, naming, addressing
        |
        v
network_automation/    Implements the "how" -- scripts, templates, playbooks
        |
        v
    NetBox             Single source of truth for all network data
        |
        v
    Devices            Configs generated and pushed via Ansible
```

## Prerequisites

- Python 3.10+
- Access to a NetBox instance with an API token
- (Optional) [netbox-branching](https://github.com/netboxlabs/netbox-branching) plugin for peer-review workflow

## Installation

```bash
git clone <repo-url> network_automation
cd network_automation

# Create a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install the package and all dependencies
pip install -e .
```

This installs the `provision` command into your PATH.  Editable mode (`-e`)
means any code changes take effect immediately without reinstalling.

**Environment setup:**

```bash
# Required for live runs (not needed for --dry-run)
export NETBOX_URL="http://192.168.0.36"
export NETBOX_TOKEN="your-api-token-here"
```

Or pass them as flags: `provision --url ... --token ...`

## Quick Start

```bash
# Preview all sites
provision --dry-run

# Preview a single site
provision DCAMER --dry-run

# Provision (creates a NetBox branch for peer review)
provision DCAMER

# Merge after approval
provision --merge <BRANCH_ID>

# Run tests
python3 -m unittest discover -s tests -v
```

Also works as `python3 -m netbox --dry-run` without installing.

## Directory Structure

```
network_automation/
├── pyproject.toml              Package config, dependencies, entry points
├── netbox/                     NetBox provisioning package (library)
│   ├── __init__.py
│   ├── __main__.py             python3 -m netbox entrypoint
│   ├── cli.py                  CLI argument parsing and wiring
│   ├── constants.py            Network standards (VLANs, regions, offsets)
│   ├── client.py               NetBox REST client with branch support
│   ├── addressing.py           Pure IP/ASN derivation (no I/O)
│   ├── resources.py            Idempotent ensure_* CRUD helpers
│   ├── inventory.py            YAML inventory loader
│   └── reconcile.py            Provision / decommission / reconcile
├── inventory/                  Desired state (declarative)
│   └── sites/
│       ├── sites.yml           Site registry with ASN slot allocation
│       ├── DCAMER/
│       │   └── hosts.yml       Devices at DCAMER
│       └── DCEMEA/
│           └── hosts.yml       Devices at DCEMEA
├── tests/                      Unit tests (mirrors netbox/ structure)
│   └── test_addressing.py      Tests for pure derivation logic
├── templates/                  Jinja2 config templates (Arista EOS)
├── playbooks/                  Ansible playbooks for config deployment
└── scripts/                    Standalone utility scripts
```

### netbox/

A Python package that provisions and reconciles NetBox state from
the declarative inventory.  NetBox is the single source of truth --
these modules are the only sanctioned way to bulk-provision network data.

| Module | Responsibility |
|--------|----------------|
| `cli.py` | CLI wiring only -- parses args, calls library functions |
| `constants.py` | All network standards values (regions, VLANs, device catalog, offsets) |
| `client.py` | `NetBoxClient` -- REST session with branch-scoped request support |
| `addressing.py` | Pure IP/ASN derivation functions (deterministic, no I/O, unit-testable) |
| `resources.py` | Idempotent `ensure_*` helpers (get-or-create pattern for NetBox objects) |
| `inventory.py` | Loads `sites.yml` + per-site `hosts.yml` into a dict |
| `reconcile.py` | Orchestrates provisioning, decommissioning, and dry-run preview |
| `__main__.py` | Two-line shim so `python3 -m netbox` works |

**Adding a site:**

```bash
# 1. Assign the next available site_id in inventory/sites/sites.yml
# 2. Create inventory/sites/<SITE>/hosts.yml with device list
# 3. Preview
provision NEWSITE --dry-run

# 4. Apply -- creates a NetBox branch for peer review
provision NEWSITE

# 5. Review in NetBox UI, get approval, then merge
provision --merge <BRANCH_ID>
```

**Removing a site:**

```bash
# 1. Remove from inventory/sites/sites.yml
# 2. Delete inventory/sites/<SITE>/ directory
# 3. Run full reconcile -- it removes the site from NetBox (in a branch)
provision
```

**Environment variables:**

| Variable | Purpose | Default |
|----------|---------|---------|
| `NETBOX_URL` | NetBox base URL | `http://192.168.0.36` |
| `NETBOX_TOKEN` | API authentication token | -- |

### tests/

Unit tests that mirror the `netbox/` package structure.  Run with:

```bash
python3 -m unittest discover -s tests -v
```

Tests for `addressing.py` are pure math -- no mocking, no network calls,
no fixtures.  This is a direct benefit of separating derivation logic
from I/O.

### templates/

Jinja2 templates that generate device configurations (Arista EOS).
Templates pull variables from NetBox at render time -- no hardcoded IPs
or site-specific data.

Planned templates:
- `mgtsw.j2` -- Management switch (MGTSW) full config
- `trdsw.j2` -- Trading switch config
- `infsw.j2` -- Infrastructure switch config

### playbooks/

Ansible playbooks that orchestrate config generation and deployment.
All inventory comes from NetBox via the `netbox.netbox.nb_inventory` plugin.

Planned playbooks:
- `site_deploy.yml` -- Generate and push configs for all devices at a site
- `vlan_update.yml` -- Push VLAN changes across a site
- `validate.yml` -- Pre/post change validation

### scripts/

Standalone utilities that don't fit neatly into NetBox or Ansible:
- Config diff/audit tools
- Pre-change validation scripts
- IP addressing calculators

## Standards Reference

All addressing formulas, naming conventions, VLAN assignments, and
design decisions are documented in the `network_standards/` repository.
This repo implements those standards -- it does not redefine them.

Key standards documents:

| Document | What it defines |
|----------|-----------------|
| `sites.md` | Site naming convention ({DC}{CITY}), site registry, ASN assignment |
| `devices.md` | Device naming ({ROLE}{TYPE}{CAB}{SIDE}-{SITE}), IP offsets, L2/L3 behavior |
| `ip-addressing/site-addressing.md` | Per-site htcolo /21 and netinfra /24 derivation |
| `ip-addressing/wan-p2p.md` | Hub-to-colo /30 links |
| `ip-addressing/wan-regional.md` | Inter-region WAN /30 links (10.255.0.0/20) |
| `vlans/standard-vlans.md` | VLAN ID assignments |
| `automation.md` | Pipeline architecture (NetBox -> Ansible) |

## Conventions

### Network

- **Never hardcode IPs** -- derive from site_id using the formulas in network_standards
- **Never manually edit NetBox** for bulk operations -- use the `provision` command
- **NetBox is the source of truth** -- Ansible queries it at runtime, not static vars
- **Dry-run first** -- always preview with `--dry-run` before applying
- **One site_id, everything derived** -- a single even integer (0-190) determines all addressing
- **Branch workflow** -- all NetBox changes go through a branch for peer review before merging to main
- **sites.yml is the intent** -- declares what exists; NetBox holds the complete derived state

### Python Code Structure

- **No God scripts** -- if a file exceeds ~200 lines, split it into focused modules
- **Single responsibility** -- each `.py` file has one job (see module table above)
- **Pure logic is separate from I/O** -- derivation functions must be testable without mocking
- **Constants in one place** -- `constants.py` is the single source for magic numbers
- **CLI entrypoints are thin** -- parse args, call functions, print results; no business logic
- **Idempotent resources** -- use the `ensure_*` get-or-create pattern for all external state
- **pyproject.toml** -- use PEP 621 for dependencies and entry points, not requirements.txt
- **Entry points** -- register CLI commands via `[project.scripts]` in pyproject.toml
- **PEP 8 + 88-char lines** -- standard Python style, Black-compatible formatting
- **Docstrings on every module** -- first line states the module's single responsibility
- **Tests alongside code** -- `tests/` mirrors `netbox/` structure; pure logic gets tested first

See `.cursor/rules/python-project-standards.mdc` for the full coding standard.
