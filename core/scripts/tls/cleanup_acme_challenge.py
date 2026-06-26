#!/usr/bin/env python3
"""
Delete a stale _acme-challenge TXT record left by an interrupted acme.sh run.

Only the _acme-challenge.<subdomain> TXT record for the specified domain is
touched — no other records are modified.

Usage:
    python3 cleanup_acme_challenge.py cloudns    <domain> <auth_id> <auth_password>
    python3 cleanup_acme_challenge.py cloudflare <domain> <cf_token>
"""

import json
import sys
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _get(url, headers=None, timeout=10):
    try:
        with urlopen(Request(url, headers=headers or {}), timeout=timeout) as r:
            return json.loads(r.read())
    except (URLError, Exception):
        return {}


def _delete(url, headers=None, timeout=10):
    try:
        with urlopen(Request(url, method="DELETE", headers=headers or {}), timeout=timeout) as r:
            return json.loads(r.read())
    except (URLError, Exception):
        return {}


def cleanup_cloudns(domain, auth_id, auth_password):
    """Remove the _acme-challenge TXT record for *domain* from ClouDNS."""
    base = "https://api.cloudns.net/dns"
    auth = {"auth-id": auth_id, "auth-password": auth_password}

    def api(ep, **params):
        params.update(auth)
        return _get(f"{base}/{ep}.json?" + urlencode(params))

    # Walk up the labels to find the ClouDNS zone that owns this domain
    parts = domain.split(".")
    root = None
    for i in range(1, len(parts) - 1):
        cand = ".".join(parts[i:])
        if api("get-zone-info", **{"domain-name": cand}).get("status") == "Success":
            root = cand
            break

    if not root:
        return

    # e.g. domain=hawaii.blbj.abrdns.com, root=blbj.abrdns.com
    #      sub=hawaii, host=_acme-challenge.hawaii
    sub = domain[: -(len(root) + 1)]
    host = f"_acme-challenge.{sub}"

    recs = api("records", **{"domain-name": root, "type": "TXT", "host": host})
    if isinstance(recs, dict):
        for rid, rec in recs.items():
            if isinstance(rec, dict) and rec.get("type") == "TXT":
                api("delete-record", **{"domain-name": root, "record-id": rid})
                print(f"Removed stale {host} TXT record (ClouDNS id={rid})")


def cleanup_cloudflare(domain, cf_token):
    """Remove the _acme-challenge TXT record for *domain* from Cloudflare."""
    hdrs = {"Authorization": f"Bearer {cf_token}", "Content-Type": "application/json"}

    def get(path, **params):
        url = "https://api.cloudflare.com/client/v4" + path
        if params:
            url += "?" + urlencode(params)
        return _get(url, headers=hdrs)

    # Find the Cloudflare zone that owns this domain
    parts = domain.split(".")
    zone_id = None
    for i in range(1, len(parts) - 1):
        cand = ".".join(parts[i:])
        zones = get("/zones", name=cand).get("result", [])
        if zones:
            zone_id = zones[0]["id"]
            break

    if not zone_id:
        return

    # e.g. domain=hawaii.blbj.abrdns.com → challenge=_acme-challenge.hawaii.blbj.abrdns.com
    challenge = f"_acme-challenge.{domain}"
    recs = get(f"/zones/{zone_id}/dns_records", type="TXT", name=challenge)
    for rec in recs.get("result", []):
        _delete(
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec['id']}",
            headers=hdrs,
        )
        print(f"Removed stale {challenge} TXT record (CF id={rec['id']})")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} cloudns    <domain> <auth_id> <auth_password>")
        print(f"       {sys.argv[0]} cloudflare <domain> <cf_token>")
        sys.exit(1)

    provider = sys.argv[1]
    if provider == "cloudns":
        if len(sys.argv) < 5:
            print("cloudns requires: domain auth_id auth_password")
            sys.exit(1)
        cleanup_cloudns(sys.argv[2], sys.argv[3], sys.argv[4])
    elif provider == "cloudflare":
        if len(sys.argv) < 4:
            print("cloudflare requires: domain cf_token")
            sys.exit(1)
        cleanup_cloudflare(sys.argv[2], sys.argv[3])
    else:
        print(f"Unknown provider: {provider}")
        sys.exit(1)
