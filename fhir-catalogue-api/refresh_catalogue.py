#!/usr/bin/env python3
"""
Re-derive each dataset's SNOMED attributes from a live scan of the FHIR
server and write them to Postgres (catalogue.snomed_codes / catalogue.package_meta).

Each dataset ("package") is one claim group — a FHIR Group built by
create_claim_groups.py (DEATHCLAIMS, NAFU). A group's attributes are the
SNOMED codes on its member patients' Conditions, each stored with the number
of members who have it. Rows for packages that are no longer a group (such as
the old single PMJAY package) are removed.

The catalogue has no automatic refresh — run this whenever bundles or groups
are added to or removed from FHIR, so /packages/{id}/snomed-codes stays in
sync with what's actually queryable.

Usage:
  python3 refresh_catalogue.py [FHIR_BASE_URL] [DATABASE_URL]

Reads FHIR_USERNAME/FHIR_PASSWORD (or CATALOGUE_FHIR_USERNAME/PASSWORD)
from the environment for FHIR auth, same convention as the other scripts
in this repo.

DATABASE_URL can also be supplied as discrete POSTGRES_HOST/PORT/DB/USER/
PASSWORD env vars instead (same names app/config.py reads) — lets this run
as a Kubernetes Job using the exact same secretKeyRef entries as the
catalogue-api Deployment, with no shell string-concatenation required.
"""

import os
import sys
import asyncio

import asyncpg
import httpx

FHIR_BASE = (sys.argv[1].rstrip("/") if len(sys.argv) > 1
             else os.environ.get("CATALOGUE_FHIR_BASE_URL", "http://localhost:8080/fhir").rstrip("/"))


def _database_url() -> str:
    if len(sys.argv) > 2:
        return sys.argv[2]
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5433")
    db = os.environ.get("POSTGRES_DB", "hapi")
    user = os.environ.get("POSTGRES_USER", "admin")
    password = os.environ.get("POSTGRES_PASSWORD", "password")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL = _database_url()

FHIR_USER = os.environ.get("FHIR_USERNAME") or os.environ.get("CATALOGUE_FHIR_USERNAME", "admin")
FHIR_PASS = os.environ.get("FHIR_PASSWORD") or os.environ.get("CATALOGUE_FHIR_PASSWORD", "password")

SNOMED_SYSTEM = "http://snomed.info/sct"
GROUP_SYSTEM = "https://example.org/fhir/CodeSystem/claim-group"

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS catalogue;

CREATE TABLE IF NOT EXISTS catalogue.snomed_codes (
    package_id TEXT NOT NULL,
    code TEXT NOT NULL,
    display TEXT NOT NULL,
    PRIMARY KEY (package_id, code)
);

ALTER TABLE catalogue.snomed_codes ADD COLUMN IF NOT EXISTS patient_count INT;

CREATE TABLE IF NOT EXISTS catalogue.package_meta (
    package_id TEXT PRIMARY KEY,
    bundle_count INT NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _next(bundle: dict) -> str | None:
    return next((link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"), None)


async def fetch_groups(client: httpx.AsyncClient) -> dict[str, set[str]]:
    """Claim group name -> member Patient ids."""
    groups: dict[str, set[str]] = {}
    url_or_params: str | dict | None = {"identifier": f"{GROUP_SYSTEM}|", "_count": 50}
    while url_or_params:
        r = await (client.get("/Group", params=url_or_params) if isinstance(url_or_params, dict)
                   else client.get(url_or_params))
        r.raise_for_status()
        bundle = r.json()
        for entry in bundle.get("entry", []):
            g = entry.get("resource", {})
            name = (g.get("name") or g.get("id", "")).upper()
            groups[name] = {
                (m.get("entity") or {}).get("reference", "").rsplit("/", 1)[-1]
                for m in g.get("member", []) or []
                if not m.get("inactive")
            }
        url_or_params = _next(bundle)
    return groups


async def scan_conditions(client: httpx.AsyncClient) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Every Condition's SNOMED codes: (patient id -> codes, code -> display)."""
    codes_by_patient: dict[str, set[str]] = {}
    displays: dict[str, str] = {}
    url_or_params: str | dict | None = {"_elements": "code,subject", "_count": 500}
    while url_or_params:
        r = await (client.get("/Condition", params=url_or_params) if isinstance(url_or_params, dict)
                   else client.get(url_or_params))
        r.raise_for_status()
        bundle = r.json()
        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            patient_id = (resource.get("subject") or {}).get("reference", "").rsplit("/", 1)[-1]
            for coding in resource.get("code", {}).get("coding", []):
                if coding.get("system") == SNOMED_SYSTEM and coding.get("code"):
                    codes_by_patient.setdefault(patient_id, set()).add(coding["code"])
                    displays.setdefault(coding["code"], coding.get("display", ""))
        url_or_params = _next(bundle)
    return codes_by_patient, displays


def group_attributes(members: set[str], codes_by_patient: dict[str, set[str]]) -> dict[str, int]:
    """SNOMED code -> number of members with it."""
    counts: dict[str, int] = {}
    for pid in members:
        for code in codes_by_patient.get(pid, ()):
            counts[code] = counts.get(code, 0) + 1
    return counts


async def main() -> None:
    print(f"FHIR server : {FHIR_BASE}")
    print(f"Database    : {DATABASE_URL.split('@')[-1]}\n")

    async with httpx.AsyncClient(
        base_url=FHIR_BASE,
        auth=(FHIR_USER, FHIR_PASS) if FHIR_USER and FHIR_PASS else None,
        headers={"Accept": "application/fhir+json"},
        timeout=60.0,
    ) as client:
        print("Loading claim groups ...")
        groups = await fetch_groups(client)
        if not groups:
            sys.exit("No claim groups found on the FHIR server. Push claim_groups.fhir.json first.")
        print("  " + ", ".join(f"{n} ({len(m)} patients)" for n, m in groups.items()))

        print("Scanning Condition resources for SNOMED codes ...")
        codes_by_patient, displays = await scan_conditions(client)
        print(f"  {len(displays)} distinct SNOMED codes across {len(codes_by_patient)} patients")

    attributes = {name: group_attributes(members, codes_by_patient) for name, members in groups.items()}

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(SCHEMA_SQL)
        async with conn.transaction():
            # Replace everything: packages that are no longer a group disappear.
            await conn.execute("DELETE FROM catalogue.snomed_codes")
            await conn.execute("DELETE FROM catalogue.package_meta")
            for name, counts in attributes.items():
                await conn.executemany(
                    "INSERT INTO catalogue.snomed_codes (package_id, code, display, patient_count) VALUES ($1, $2, $3, $4)",
                    [(name, code, displays[code], n) for code, n in counts.items()],
                )
                await conn.execute(
                    "INSERT INTO catalogue.package_meta (package_id, bundle_count, refreshed_at) VALUES ($1, $2, now())",
                    name,
                    len(groups[name]),
                )
    finally:
        await conn.close()

    print("\nCatalogue refreshed:")
    for name, counts in attributes.items():
        print(f"  {name}: {len(counts)} SNOMED attributes, {len(groups[name])} patients")


if __name__ == "__main__":
    asyncio.run(main())
