#!/usr/bin/env python3
"""
Push the transaction bundles in fhir_bundles_nov09/ to a FHIR server.

Files are under DEATHCLAIMS/ and NAFU/, each with the same layout below.
Each group folder is pushed in turn, then claim_groups.fhir.json.

fhir_legacy_bundles/ pages are uploaded in an explicit dependency order
(Organization/Practitioner/Patient first, down to DocumentReference last)
since they reference each other by relative id. fhir_v6/ case bundles are
self-contained transactions (internal urn:uuid references resolve within
each bundle) so they're uploaded in any order. groups/ (DEATHCLAIMS / NAFU)
(claim_groups.fhir.json) goes last because its members reference Patients
and Claims from the rest.

Usage:
  python3 push_converted_bundles.py [FHIR_BASE_URL]
"""

import os
import sys
import json
import glob
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

FHIR_BASE = (sys.argv[1].rstrip("/") if len(sys.argv) > 1
             else os.environ.get("CATALOGUE_FHIR_BASE_URL", "http://localhost:8080/fhir").rstrip("/"))
ROOT = Path("fhir_bundles_nov09")

FHIR_USER = os.environ.get("FHIR_USERNAME") or os.environ.get("CATALOGUE_FHIR_USERNAME", "admin")
FHIR_PASS = os.environ.get("FHIR_PASSWORD") or os.environ.get("CATALOGUE_FHIR_PASSWORD", "password")
AUTH = HTTPBasicAuth(FHIR_USER, FHIR_PASS)
HEADERS = {
    "Content-Type": "application/fhir+json",
    "Accept": "application/fhir+json",
}

GROUP_DIRS = ["DEATHCLAIMS", "NAFU"]

LEGACY_TYPE_ORDER = [
    "Organization", "Practitioner", "Patient",
    "Coverage", "Encounter",
    "Condition", "Procedure",
    "Claim", "ClaimResponse",
    "DocumentReference",
]


def upload_file(path: Path) -> bool:
    bundle = json.loads(path.read_text())
    try:
        resp = requests.post(FHIR_BASE, json=bundle, auth=AUTH, headers=HEADERS, timeout=120)
    except requests.RequestException as e:
        print(f"  FAILED (connection error: {e})")
        return False

    if resp.status_code in (200, 201):
        print(f"  OK ({resp.status_code})")
        return True

    print(f"  FAILED ({resp.status_code})")
    print("   ", resp.text[:500])
    return False


def main() -> None:
    print(f"FHIR server : {FHIR_BASE}")
    print(f"Auth        : {FHIR_USER} / ***\n")

    success = 0
    failed = []

    all_files = []
    for group_dir in GROUP_DIRS:
        for rtype in LEGACY_TYPE_ORDER:
            all_files.extend(sorted((ROOT / group_dir / "fhir_legacy_bundles" / rtype).glob("*.json")))
        all_files.extend(sorted((ROOT / group_dir / "fhir_v6").glob("*.json")))
    all_files.extend(sorted(ROOT.glob("claim_groups*.json")))
    total = len(all_files)

    for i, f in enumerate(all_files, 1):
        rel = f.relative_to(ROOT)
        print(f"[{i}/{total}] {rel} ... ", end="", flush=True)
        if upload_file(f):
            success += 1
        else:
            failed.append(str(rel))

    print("\n===== Push complete =====")
    print(f"  Success: {success}/{total}")
    print(f"  Failed:  {len(failed)}")
    if failed:
        print("\nFailed files:")
        for f in failed:
            print(f"  - {f}")


if __name__ == "__main__":
    main()
