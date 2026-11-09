"""
Async FHIR client wrapping httpx.

All FHIR queries go through this module. Results that are expensive to
re-fetch (patient ID lists per package, per-code counts) are cached
in-process with a configurable TTL.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SNOMED_SYSTEM = "http://snomed.info/sct"


class _TTLCache:
    def __init__(self, ttl: int) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry and (time.monotonic() - entry[0]) < self._ttl:
            return entry[1]
        return None

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic(), value)


class FHIRClient:
    def __init__(self, base_url: str, username: str = "", password: str = "", cache_ttl: int = 300) -> None:
        auth = (username, password) if username and password else None
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=auth,
            headers={
                "Accept": "application/fhir+json",
                "Content-Type": "application/fhir+json",
            },
            timeout=30.0,
        )
        self._cache = _TTLCache(cache_ttl)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        try:
            r = await self._client.get("/metadata", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Patient data for a package + single SNOMED code
    # ------------------------------------------------------------------

    async def get_patients(self, member_ids: set[str], snomed_code: str) -> tuple[int, list[dict], dict]:
        """
        Returns (total, list_of_patient_records, raw_bundle) for every
        Condition matching the SNOMED code whose subject is one of member_ids
        (the package's claim-group members), with the linked Patient included.
        """
        code_param = f"{SNOMED_SYSTEM}|{snomed_code}"

        conditions: list[dict] = []
        patients_by_id: dict[str, dict] = {}
        url_or_params: str | dict = {
            "code": code_param,
            "_include": "Condition:subject",
            "_count": 200,
        }

        while url_or_params:
            try:
                if isinstance(url_or_params, dict):
                    r = await self._client.get("/Condition", params=url_or_params)
                else:
                    r = await self._client.get(url_or_params)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("FHIR Condition query failed: %s", exc)
                break

            bundle = r.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                rtype = resource.get("resourceType")
                if rtype == "Condition":
                    conditions.append(resource)
                elif rtype == "Patient":
                    patients_by_id[resource.get("id", "")] = resource

            next_url = _next_link(bundle)
            url_or_params = next_url if next_url else None

        conditions, patients_by_id = _only_members(conditions, patients_by_id, member_ids)
        records = [_shape_record(cond, patients_by_id) for cond in conditions]
        records = [r for r in records if r]  # drop any that failed to shape
        return len(records), records, _build_bundle(conditions, patients_by_id)

    # ------------------------------------------------------------------
    # Patient data for a package + multiple SNOMED codes (OR logic)
    # ------------------------------------------------------------------

    async def get_patients_multi(self, member_ids: set[str], snomed_codes: list[str]) -> tuple[int, list[dict], dict]:
        """
        Returns (total, list_of_patient_records, raw_bundle) for any member of
        member_ids (the package's claim-group members) whose condition matches
        ANY of the supplied SNOMED codes.

        FHIR represents OR as comma-separated values within one code parameter:
          code=http://snomed.info/sct|4834000,http://snomed.info/sct|302231008
        Group membership is applied here rather than in the search, because the
        member list can be hundreds of ids — too long for a subject= parameter.
        """
        # FHIR OR: comma-separated token values in one parameter
        code_param = ",".join(f"{SNOMED_SYSTEM}|{code}" for code in snomed_codes)

        conditions: list[dict] = []
        patients_by_id: dict[str, dict] = {}
        url_or_params: str | dict = {
            "code": code_param,
            "_include": "Condition:subject",
            "_count": 500,
        }

        while url_or_params:
            try:
                if isinstance(url_or_params, dict):
                    r = await self._client.get("/Condition", params=url_or_params)
                else:
                    r = await self._client.get(url_or_params)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("FHIR multi-code Condition query failed: %s", exc)
                break

            bundle = r.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                rtype = resource.get("resourceType")
                if rtype == "Condition":
                    conditions.append(resource)
                elif rtype == "Patient":
                    patients_by_id[resource.get("id", "")] = resource

            next_url = _next_link(bundle)
            url_or_params = next_url if next_url else None

        conditions, patients_by_id = _only_members(conditions, patients_by_id, member_ids)
        records = [_shape_record(cond, patients_by_id) for cond in conditions]
        records = [r for r in records if r]
        return len(records), records, _build_bundle(conditions, patients_by_id)

    # ------------------------------------------------------------------
    # Arbitrary pre-built FHIR search query (ABAC-approved queries)
    # ------------------------------------------------------------------

    async def execute_query(self, query: str) -> tuple[int, dict]:
        """
        Executes a pre-built, relative FHIR R4 search query string (e.g.
        "Patient?_has:Condition:patient:code=...") against the upstream FHIR
        server and returns (status_code, body) verbatim. Performs no
        authorization itself — the caller (omop-auth's ABAC gate) is expected
        to have already verified the requester holds an approved grant for
        this exact query before calling here.
        """
        try:
            r = await self._client.get(query)
        except httpx.HTTPError as exc:
            logger.warning("FHIR query execution failed: %s", exc)
            return 502, {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "transient", "diagnostics": str(exc)}],
            }

        try:
            body = r.json()
        except ValueError:
            body = {
                "resourceType": "OperationOutcome",
                "issue": [{
                    "severity": "error",
                    "code": "invalid",
                    "diagnostics": "Upstream FHIR server returned a non-JSON response",
                }],
            }
        return r.status_code, body

    # ------------------------------------------------------------------
    # Claim groups (FHIR Group resources: DEATHCLAIMS, NAFU, ...)
    # ------------------------------------------------------------------

    async def get_claim_groups(self) -> list[dict]:
        """
        Every claim Group on the server (identified by GROUP_SYSTEM), shaped by
        _shape_group and cached for the count TTL. Raises FHIRUnavailable when
        the server can't be reached, so callers return 502 instead of an empty
        list that looks like "no groups".
        """
        cached = self._cache.get("claim-groups")
        if cached is not None:
            return cached

        groups: list[dict] = []
        url_or_params: str | dict | None = {"identifier": f"{GROUP_SYSTEM}|", "_count": 50}
        while url_or_params:
            try:
                if isinstance(url_or_params, dict):
                    r = await self._client.get("/Group", params=url_or_params)
                else:
                    r = await self._client.get(url_or_params)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                raise FHIRUnavailable(f"FHIR Group search failed: {exc}") from exc
            bundle = r.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                if resource.get("resourceType") == "Group":
                    groups.append(_shape_group(resource))
            url_or_params = _next_link(bundle)

        groups.sort(key=lambda g: g["name"])
        self._cache.set("claim-groups", groups)
        return groups

    async def get_claim_group(self, group_id: str) -> dict | None:
        """One claim group by FHIR id (claim-group-deathclaims) or by name
        (DEATHCLAIMS, case-insensitive). None when there's no such group."""
        wanted = group_id.strip().lower()
        for g in await self.get_claim_groups():
            if g["id"].lower() == wanted or g["name"].lower() == wanted:
                return g
        return None

    async def get_patient_everything(self, patient_id: str) -> dict:
        """
        Every resource in the patient's compartment (Patient/{id}/$everything),
        following paging, merged into one searchset Bundle. This is how a
        member's claim bundle is rebuilt: the upload transactions were split
        into individual resources on the server.
        """
        entries: list[dict] = []
        seen: set[str] = set()
        url_or_params: str | dict | None = {"_count": 500}
        path = f"/Patient/{patient_id}/$everything"
        while url_or_params:
            try:
                if isinstance(url_or_params, dict):
                    r = await self._client.get(path, params=url_or_params)
                else:
                    r = await self._client.get(url_or_params)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                raise FHIRUnavailable(f"FHIR $everything failed for Patient/{patient_id}: {exc}") from exc
            bundle = r.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                key = f"{resource.get('resourceType')}/{resource.get('id')}"
                if key in seen:
                    continue
                seen.add(key)
                entries.append({"fullUrl": key, "resource": resource})
            url_or_params = _next_link(bundle)

        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(entries),
            "entry": entries,
        }


class FHIRUnavailable(Exception):
    """The upstream FHIR server failed or couldn't be reached."""


# Claim groups are FHIR Group resources tagged with this identifier system
# (see create_claim_groups.py in the repo root). Each member is a Patient,
# with the claim(s) that put them in the group listed in MEMBER_CLAIM_EXT.
GROUP_SYSTEM = "https://example.org/fhir/CodeSystem/claim-group"
MEMBER_CLAIM_EXT = "https://example.org/fhir/StructureDefinition/group-member-claim"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _shape_group(group: dict) -> dict:
    members = []
    for m in group.get("member", []) or []:
        if m.get("inactive"):
            continue
        ref = (m.get("entity") or {}).get("reference", "")
        claim_ids = [
            ((ext.get("valueReference") or {}).get("reference", "")).rsplit("/", 1)[-1]
            for ext in m.get("extension", []) or []
            if ext.get("url") == MEMBER_CLAIM_EXT
        ]
        members.append({"patient_id": ref.rsplit("/", 1)[-1], "claim_ids": [c for c in claim_ids if c]})

    name = group.get("name") or next(
        (i.get("value") for i in group.get("identifier", []) if i.get("system") == GROUP_SYSTEM), group.get("id", "")
    )
    description = re.sub(r"<[^>]+>", "", (group.get("text") or {}).get("div", "")).strip()
    return {
        "id": group.get("id", ""),
        "name": name,
        "description": description,
        "patient_count": len(members),
        "claim_count": sum(len(m["claim_ids"]) for m in members),
        "last_updated": (group.get("meta") or {}).get("lastUpdated"),
        "members": members,
    }

def _next_link(bundle: dict) -> str | None:
    for link in bundle.get("link", []):
        if link.get("relation") == "next":
            return link.get("url")
    return None


def _subject_id(condition: dict) -> str:
    ref = condition.get("subject", {}).get("reference", "")
    return ref.rsplit("/", 1)[-1]


def _only_members(
    conditions: list[dict], patients: dict[str, dict], member_ids: set[str]
) -> tuple[list[dict], dict[str, dict]]:
    """Keep only Conditions (and Patients) belonging to the given members."""
    kept = [c for c in conditions if _subject_id(c) in member_ids]
    return kept, {pid: p for pid, p in patients.items() if pid in member_ids}


def _shape_record(condition: dict, patients: dict[str, dict]) -> dict | None:
    subject_ref = condition.get("subject", {}).get("reference", "")
    patient_id = subject_ref.split("/", 1)[1] if "/" in subject_ref else subject_ref
    patient = patients.get(patient_id, {})

    codings = condition.get("code", {}).get("coding", [])
    coding = next((c for c in codings if c.get("system") == SNOMED_SYSTEM), codings[0] if codings else {})
    status = (
        condition.get("clinicalStatus", {})
        .get("coding", [{}])[0]
        .get("code", "unknown")
    )

    name = _extract_name(patient)
    if not name:
        return None

    return {
        "patient_id": patient_id,
        "name": name,
        "gender": patient.get("gender", "unknown"),
        "birth_date": patient.get("birthDate", "unknown"),
        "condition_code": coding.get("code", ""),
        "condition_display": coding.get("display", ""),
        "condition_status": status,
    }


def _build_bundle(conditions: list[dict], patients: dict[str, dict]) -> dict:
    entries = [{"resource": cond} for cond in conditions]
    entries += [{"resource": patient} for patient in patients.values()]
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(entries),
        "entry": entries,
    }


def _extract_name(patient: dict) -> str:
    for name_entry in patient.get("name", []):
        family = name_entry.get("family", "")
        given = " ".join(name_entry.get("given", []))
        prefix = " ".join(name_entry.get("prefix", []))
        parts = [p for p in [prefix, given, family] if p]
        if parts:
            return " ".join(parts)
        if name_entry.get("text"):
            return name_entry["text"]
    return ""
