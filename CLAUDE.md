# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Operationalizes the standards defined in `network_standards/` by provisioning NetBox with all derived addressing, devices, VLANs, and prefixes. NetBox is then consumed by Ansible to generate and push device configs.

## Commands

```bash
# Install (editable)
pip install -e .

# Run tests
python -m pytest tests/

# Run a single test
python -m pytest tests/test_addressing.py::test_derive_site_addressing

# Preview provisioning (no API calls)
provision --dry-run
provision DCAMER --dry-run

# Provision a site (creates a NetBox branch)
provision DCAMER

# Merge a branch after review
provision --merge <BRANCH_ID>
```

Environment variables: `NETBOX_URL` (default: `http://192.168.0.36`), `NETBOX_TOKEN`.

## Architecture

The code is structured in four layers with strict separation of concerns:

```
cli.py          ← argument parsing only, no business logic
addressing.py   ← pure derivation functions (no I/O, fully unit-testable)
resources.py    ← idempotent ensure_* CRUD helpers (get-or-create pattern)
reconcile.py    ← orchestration: calls addressing + resources to provision/decommission sites
```

`constants.py` encodes all magic numbers from `network_standards/` (region blocks, VLAN offsets, device role offsets, local supernets). It is the single place to update when standards change.

`inventory/` holds declarative YAML: `sites/sites.yml` is the site registry (site_code → site_id), and `sites/<SITE>/hosts.yml` is the device list per site.

## Key Design Patterns

- **Deterministic addressing** — all IPs are derived from `site_id` via pure math in `addressing.py`. No address is ever manually chosen.
- **Idempotent resources** — all `ensure_*` functions in `resources.py` follow get-or-create; safe to run repeatedly.
- **Branch workflow** — all NetBox writes go into a named branch for peer review before merging to main.
- **Dry-run first** — `--dry-run` prints all derived objects without touching the API.

## Adding a New Site

1. Pick the next available even `site_id` from the spare slots in `inventory/sites/sites.yml`.
2. Add the site entry to `sites.yml` and create `inventory/sites/<SITE>/hosts.yml` with the device list.
3. Run `provision <SITE> --dry-run` to verify derived addressing.
4. Run `provision <SITE>` to create a NetBox branch.
5. Review in the NetBox UI, get approval, then run `provision --merge <BRANCH_ID>`.

## Relationship to network_standards

`constants.py` is a direct encoding of the formulas and values defined in `../network_standards/`. When a standard changes, update both the documentation in `network_standards/` and the corresponding constant here.
