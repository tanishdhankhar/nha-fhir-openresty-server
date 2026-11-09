"""
Catalogue of datasets ("packages").

Each package is one claim group — a FHIR Group on the server built by
create_claim_groups.py (DEATHCLAIMS, NAFU). The package id is the group's
name, and its member patients are the package's patients.

The group list and membership come live from FHIR (cached, see
FHIRClient.get_claim_groups). Each group's SNOMED code list ("attributes")
and per-code patient counts are derived by refresh_catalogue.py from a scan of
the members' Conditions and stored in Postgres (see app/db.py), because
computing them per request would mean scanning every Condition.
"""

from __future__ import annotations

from . import db
from .fhir import FHIRClient

# Display names and descriptions for the known groups. A group missing here
# still shows up, using its FHIR name and narrative.
_GROUP_META: dict[str, dict[str, str]] = {
    "DEATHCLAIMS": {
        "label": "Deathcases",
        "description": (
            "AB-PMJAY claims where the patient died: the patient is marked deceased, the "
            "discharge status is expired, the claim carries a death date, or a death summary, "
            "death certificate or mortality audit is attached."
        ),
    },
    "NAFU": {
        "label": "NAFU",
        "description": "All other AB-PMJAY claims (every claim not in Deathcases).",
    },
}


async def _groups_by_name(fhir: FHIRClient) -> dict[str, dict]:
    return {g["name"].upper(): g for g in await fhir.get_claim_groups()}


async def _group(fhir: FHIRClient, package_id: str) -> dict | None:
    """The claim group for a package id: the group name (DEATHCLAIMS, any case)
    or its FHIR Group id (claim-group-deathclaims)."""
    return await fhir.get_claim_group(package_id)


async def _package(group: dict) -> dict:
    name = group["name"].upper()
    meta = _GROUP_META.get(name, {})
    codes = await db.fetch_snomed_codes(name)
    return {
        "id": name,
        "label": meta.get("label", group["name"]),
        "description": meta.get("description", group["description"]),
        "procedure": "N/A",
        "procedure_snomed_code": "N/A",
        "pre_auth_ref": "N/A",
        "snomed_code_count": len(codes),
        "bundle_count": group["patient_count"],
        "group_id": group["id"],
        "patient_count": group["patient_count"],
        "claim_count": group["claim_count"],
    }


async def get_all_packages(fhir: FHIRClient) -> list[dict]:
    groups = await _groups_by_name(fhir)
    # Known groups first, in a fixed order; any others after, by name.
    order = list(_GROUP_META) + sorted(n for n in groups if n not in _GROUP_META)
    return [await _package(groups[n]) for n in order if n in groups]


async def get_package(fhir: FHIRClient, package_id: str) -> dict | None:
    group = await _group(fhir, package_id)
    return await _package(group) if group else None


async def get_member_ids(fhir: FHIRClient, package_id: str) -> set[str]:
    """FHIR Patient ids of the package's members (empty if no such package)."""
    group = await _group(fhir, package_id)
    return {m["patient_id"] for m in group["members"]} if group else set()


async def get_snomed_codes(fhir: FHIRClient, package_id: str) -> list[dict] | None:
    """The package's attributes: [{code, display, patient_count}], or None if
    there's no such package."""
    group = await _group(fhir, package_id)
    if not group:
        return None
    return await db.fetch_snomed_codes(group["name"].upper())


async def is_valid_snomed_for_package(fhir: FHIRClient, package_id: str, snomed_code: str) -> bool:
    codes = await get_snomed_codes(fhir, package_id)
    return any(c["code"] == snomed_code for c in codes or [])


async def get_snomed_display(fhir: FHIRClient, package_id: str, snomed_code: str) -> str | None:
    for c in await get_snomed_codes(fhir, package_id) or []:
        if c["code"] == snomed_code:
            return c["display"]
    return None
