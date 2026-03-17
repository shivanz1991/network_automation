"""
NetBox API client with branch support.

Provides a thin wrapper around the NetBox REST API using raw requests,
with support for the netbox-branching plugin.  Reusable by any script
that needs to talk to NetBox.
"""

import time

import requests


class NetBoxClient:
    """Low-level NetBox REST client with optional branch-scoped requests."""

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
        r = self.session.get(
            f"{self.url}/api/{endpoint}", params=params, headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    def post(self, endpoint, data):
        r = self.session.post(
            f"{self.url}/api/{endpoint}", json=data, headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    def delete(self, endpoint):
        r = self.session.delete(
            f"{self.url}/api/{endpoint}", headers=self._headers(),
        )
        r.raise_for_status()

    def get_or_none(self, endpoint, **filters):
        result = self.get(endpoint, params=filters)
        results = result.get("results", [])
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Branch operations (never scoped to a branch themselves)
    # ------------------------------------------------------------------

    def create_branch(self, name: str, description: str = "") -> dict:
        r = self.session.post(
            f"{self.url}/api/plugins/branching/branches/",
            json={"name": name, "description": description},
        )
        r.raise_for_status()
        return r.json()

    def get_branch(self, branch_id: int) -> dict:
        r = self.session.get(
            f"{self.url}/api/plugins/branching/branches/{branch_id}/",
        )
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
