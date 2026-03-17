"""
CLI entrypoint for NetBox provisioning.

This module contains only argument parsing and wiring -- no business logic.
Installed as the ``provision`` console command via pyproject.toml.

Usage:
    provision --dry-run                 # preview all sites
    provision DCAMER --dry-run          # preview one site
    provision                           # full reconcile (all sites)
    provision DCAMER                    # provision one site only
    provision --merge 3                 # merge an existing branch

Environment variables:
    NETBOX_URL    - NetBox base URL (default: http://192.168.0.36)
    NETBOX_TOKEN  - API authentication token (required for live runs)
"""

import argparse
import os
import sys
import time

import requests

from netbox.client import NetBoxClient
from netbox.inventory import load_sites
from netbox.reconcile import provision_site, reconcile


PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PACKAGE_DIR)
INVENTORY_DIR = os.path.join(REPO_DIR, "inventory")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision",
        description="Reconcile NetBox state with inventory/ desired state.",
    )
    parser.add_argument(
        "site", nargs="?", default=None,
        help="Site code to provision (e.g. DCAMER). Omit for all sites.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("NETBOX_URL", "http://192.168.0.36"),
        help="NetBox URL (default: $NETBOX_URL or http://192.168.0.36)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("NETBOX_TOKEN"),
        help="NetBox API token (default: $NETBOX_TOKEN)",
    )
    parser.add_argument(
        "--inventory", default=INVENTORY_DIR,
        help="Path to inventory directory",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be created without making changes",
    )
    parser.add_argument(
        "--branch", default=None,
        help="NetBox branch name (default: auto-generated timestamp)",
    )
    parser.add_argument(
        "--merge", type=int, default=None, metavar="BRANCH_ID",
        help="Merge an existing branch by ID",
    )
    return parser


def handle_merge(args):
    """Merge an existing NetBox branch and exit."""
    if not args.token:
        print("Error: --token required", file=sys.stderr)
        sys.exit(1)
    nb = NetBoxClient(args.url, args.token)
    print(f"Merging branch {args.merge}...")
    result = nb.merge_branch(args.merge)
    print(f"Merge job submitted: {result.get('id', 'unknown')}")


def setup_branch(nb, branch_name):
    """Create a NetBox branch; fall back to main if plugin is missing.

    Returns the branch_id on success, or None if branching is unavailable.
    """
    print(f"Creating NetBox branch: {branch_name}")
    try:
        branch = nb.create_branch(
            branch_name,
            description="Automated provisioning from inventory",
        )
        branch_id = branch["id"]
        schema_id = branch["schema_id"]
        print(f"  Branch ID: {branch_id}, schema: {schema_id}")
        print("  Waiting for branch to be ready...")
        nb.wait_for_branch_ready(branch_id)
        print("  Branch ready.")
        nb.branch_schema_id = schema_id
        return branch_id
    except requests.exceptions.HTTPError as e:
        if "branching" in str(e).lower() or e.response.status_code == 404:
            print(
                "  Warning: NetBox branching plugin not available, "
                "operating on main"
            )
            print("  Install netbox-branching for branch-based workflow")
            return None
        raise


def print_post_run(branch_name, branch_id):
    """Print next-steps after a successful provisioning run."""
    if branch_id:
        print(f"\n{'=' * 60}")
        print(f"Changes staged in branch: {branch_name}")
        print(f"Branch ID: {branch_id}")
        print()
        print("Next steps:")
        print("  1. Review changes in NetBox UI")
        print("  2. Get peer approval")
        print(f"  3. Merge: provision --merge {branch_id}")
        print(f"{'=' * 60}")
    else:
        print("\nProvisioning complete (applied directly to main).")


def filter_sites(all_sites: dict, site_code: str) -> dict:
    """Return a single-site dict if site_code is given, or all sites."""
    if site_code is None:
        return all_sites
    upper = site_code.upper()
    if upper not in all_sites:
        print(
            f"Error: site '{site_code}' not found in inventory. "
            f"Available: {', '.join(sorted(all_sites))}",
            file=sys.stderr,
        )
        sys.exit(1)
    return {upper: all_sites[upper]}


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.merge:
        handle_merge(args)
        return

    all_sites = load_sites(args.inventory)
    sites = filter_sites(all_sites, args.site)

    if args.dry_run:
        reconcile(None, sites, dry_run=True)
        return

    if not args.token:
        print("Error: --token or NETBOX_TOKEN required", file=sys.stderr)
        sys.exit(1)

    nb = NetBoxClient(args.url, args.token)

    scope = args.site.upper() if args.site else "all"
    branch_name = (
        args.branch or f"provision-{scope}-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    branch_id = setup_branch(nb, branch_name)

    if args.site:
        cfg = sites[args.site.upper()]
        provision_site(nb, args.site.upper(), cfg["site_id"], cfg["devices"])
    else:
        reconcile(nb, sites)

    print_post_run(branch_name, branch_id)
