#!/usr/bin/env python3
"""
Sort every claim in fhir_bundles_nov09/ into one of two FHIR Groups, write
them as a transaction bundle (claim_groups.fhir.json), and split the bundle
files into one folder per group:

  fhir_bundles_nov09/
    claim_groups.fhir.json
    DEATHCLAIMS/fhir_legacy_bundles/<Type>/page_*.json, DEATHCLAIMS/fhir_v6/*.json
    NAFU/fhir_legacy_bundles/<Type>/page_*.json,        NAFU/fhir_v6/*.json

fhir_v6 files are one case each, so the whole file moves. Legacy pages mix
patients, so each page is split by the patient each entry belongs to.
Entries tied to no patient (Organization, Practitioner) are copied into
every group folder that has legacy data, so each folder uploads on its own.

  DEATHCLAIMS  claims where the patient died. A claim counts if any of these
               is present for it:
                 - Patient.deceasedBoolean / deceasedDateTime
                 - Encounter discharge disposition "expired" / "dead"
                 - Claim supportingInfo "deathdate" with a value
                 - an attached DocumentReference of a death document type
                   (death summary, death certificate, mortality audit)
  NAFU         every other claim.

A Condition that only *mentions* death (verificationStatus unconfirmed /
refuted, usually picked up from document text) does not count.

The server is FHIR R4, where Group.member.entity can only point at people
(Patient), not Claims. So each member is the Patient, and the claim(s) that
put them in the group are listed in a member extension
(MEMBER_CLAIM_EXT -> valueReference Claim/<id>). A patient with both a death
claim and other claims appears in both groups, each with its own claims.

Usage:
  python3 create_claim_groups.py [ROOT]   (default: fhir_bundles_nov09)
"""

import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

GROUP_SYSTEM = "https://example.org/fhir/CodeSystem/claim-group"
MEMBER_CLAIM_EXT = "https://example.org/fhir/StructureDefinition/group-member-claim"
OUTPUT_FILE = Path("claim_groups.fhir.json")
SOURCE_DIRS = ("fhir_legacy_bundles", "fhir_v6")

DEATH_DOC = re.compile(r"death summary|death certificate|death-summary|death-certificate|mortality audit", re.I)
EXPIRED = re.compile(r"\bexp\b|expired|deceased|dead", re.I)

GROUPS = {
    "DEATHCLAIMS": {
        "id": "claim-group-deathclaims",
        "display": "Death claims",
        "description": "Claims where the patient died: patient marked deceased, discharge status expired, "
                       "a death date on the claim, or a death summary / death certificate / mortality audit attached.",
    },
    "NAFU": {
        "id": "claim-group-nafu",
        "display": "NAFU",
        "description": "All claims not in DEATHCLAIMS.",
    },
}


def resources(obj):
    if isinstance(obj, dict):
        if obj.get("resourceType") not in (None, "Bundle"):
            yield obj
        for v in obj.values():
            yield from resources(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from resources(v)


def ref_id(reference) -> str:
    """'Patient/x' or 'urn:uuid:x' -> 'x'."""
    ref = (reference or {}).get("reference", "") if isinstance(reference, dict) else (reference or "")
    return ref.rsplit("/", 1)[-1].replace("urn:uuid:", "")


def has_death_date(claim: dict) -> bool:
    for info in claim.get("supportingInfo", []) or []:
        codes = {c.get("code") for c in (info.get("category") or {}).get("coding", [])}
        if "deathdate" in codes and (info.get("timingDate") or info.get("valueString")):
            return True
    return False


def patient_signals(rs) -> set[str]:
    """Patient ids with a death signal anywhere in this set of resources."""
    dead = set()
    for r in rs:
        t = r["resourceType"]
        if t == "Patient" and (r.get("deceasedBoolean") or r.get("deceasedDateTime")):
            dead.add(r["id"])
        elif t == "Encounter":
            disp = (r.get("hospitalization") or {}).get("dischargeDisposition") or {}
            if EXPIRED.search(json.dumps(disp)):
                dead.add(ref_id(r.get("subject")))
        elif t == "DocumentReference":
            kinds = json.dumps([r.get("type"), r.get("category")])
            if DEATH_DOC.search(kinds):
                dead.add(ref_id(r.get("subject")))
    return dead


def classify(root: Path) -> dict[str, dict[str, list[str]]]:
    """-> {group: {patient_id: [claim_id, ...]}}"""
    groups = {name: defaultdict(list) for name in GROUPS}

    # Legacy pages: resources of each type live in separate files and link by Patient id.
    legacy = [r for f in sorted((root / "fhir_legacy_bundles").rglob("*.json"))
              for r in resources(json.loads(f.read_text()))]
    dead = patient_signals(legacy)
    for claim in (r for r in legacy if r["resourceType"] == "Claim"):
        pid = ref_id(claim.get("patient"))
        name = "DEATHCLAIMS" if pid in dead or has_death_date(claim) else "NAFU"
        groups[name][pid].append(claim["id"])

    # v6: one file per case, with one Patient and its Claim(s).
    for f in sorted((root / "fhir_v6").glob("*.json")):
        rs = list(resources(json.loads(f.read_text())))
        dead = patient_signals(rs)
        for claim in (r for r in rs if r["resourceType"] == "Claim"):
            pid = ref_id(claim.get("patient"))
            name = "DEATHCLAIMS" if pid in dead or has_death_date(claim) else "NAFU"
            groups[name][pid].append(claim["id"])

    return groups


def build_group(name: str, members: dict[str, list[str]]) -> dict:
    meta = GROUPS[name]
    n_claims = sum(len(c) for c in members.values())
    return {
        "resourceType": "Group",
        "id": meta["id"],
        "text": {
            "status": "generated",
            "div": f'<div xmlns="http://www.w3.org/1999/xhtml"><p><b>{name}</b>: {meta["description"]} '
                   f"{len(members)} patients, {n_claims} claims.</p></div>",
        },
        "identifier": [{"system": GROUP_SYSTEM, "value": name}],
        "active": True,
        "type": "person",
        "actual": True,
        "code": {
            "coding": [{"system": GROUP_SYSTEM, "code": name, "display": meta["display"]}],
            "text": name,
        },
        "name": name,
        "quantity": len(members),
        "member": [
            {
                "extension": [
                    {"url": MEMBER_CLAIM_EXT, "valueReference": {"reference": f"Claim/{cid}"}}
                    for cid in sorted(set(claims))
                ],
                "entity": {"reference": f"Patient/{pid}"},
            }
            for pid, claims in sorted(members.items())
        ],
    }


def patient_of(resource: dict):
    if resource.get("resourceType") == "Patient":
        return resource.get("id")
    for key in ("patient", "subject", "beneficiary"):
        ref = resource.get(key)
        if isinstance(ref, dict) and ref.get("reference", "").startswith(("Patient/", "urn:uuid:")):
            return ref_id(ref)
    return None


def split_into_group_folders(root: Path, groups: dict) -> None:
    claim_group = {cid: name for name, members in groups.items() for cids in members.values() for cid in cids}
    patient_groups = defaultdict(set)
    for name, members in groups.items():
        for pid in members:
            patient_groups[pid].add(name)

    def write(name, rel, bundle):
        dst = root / name / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(bundle, indent=2))

    legacy_root = root / "fhir_legacy_bundles"
    legacy_files = sorted(legacy_root.rglob("*.json"))
    # Groups that hold at least one legacy patient get the shared legacy entries.
    legacy_pids = {patient_of(r) for f in legacy_files for r in resources(json.loads(f.read_text()))} - {None}
    legacy_groups = {g for pid in legacy_pids for g in (patient_groups.get(pid) or {"NAFU"})}

    for f in legacy_files:
        bundle = json.loads(f.read_text())
        per_group = defaultdict(list)
        for entry in bundle.get("entry", []):
            r = entry.get("resource", {})
            if r.get("resourceType") == "Claim":
                targets = {claim_group.get(r["id"], "NAFU")}
            else:
                pid = patient_of(r)
                targets = (patient_groups.get(pid) or {"NAFU"}) if pid else legacy_groups
            for name in targets:
                per_group[name].append(entry)
        for name, entries in per_group.items():
            write(name, f.relative_to(root), {**bundle, "entry": entries})

    for f in sorted((root / "fhir_v6").glob("*.json")):
        claims = [r for r in resources(json.loads(f.read_text())) if r["resourceType"] == "Claim"]
        names = {claim_group.get(c["id"], "NAFU") for c in claims} or {"NAFU"}
        name = "DEATHCLAIMS" if "DEATHCLAIMS" in names else "NAFU"
        dst = root / name / f.relative_to(root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(f, dst)

    for d in SOURCE_DIRS:
        shutil.rmtree(root / d, ignore_errors=True)

    for name in groups:
        n = sum(1 for _ in (root / name).rglob("*.json")) if (root / name).exists() else 0
        print(f"  {root / name}/: {n} files")


def write_group_bundle(root: Path = Path("fhir_bundles_nov09")) -> Path:
    if not all((root / d).is_dir() for d in SOURCE_DIRS):
        sys.exit(f"{root} has no {' / '.join(SOURCE_DIRS)} folders (already split?). "
                 "Re-run convert_to_transaction_bundles.py to rebuild it.")
    groups = classify(root)
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [],
    }
    for name, members in groups.items():
        g = build_group(name, members)
        bundle["entry"].append({
            "fullUrl": f"Group/{g['id']}",
            "resource": g,
            "request": {"method": "PUT", "url": f"Group/{g['id']}"},
        })
        print(f"  {name:<12}: {len(members)} patients, {sum(len(c) for c in members.values())} claims")

    both = set(groups["DEATHCLAIMS"]) & set(groups["NAFU"])
    if both:
        print(f"  {len(both)} patient(s) are in both groups (they have a death claim and other claims)")

    out = root / OUTPUT_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=2))
    print(f"Groups -> {out}")

    split_into_group_folders(root, groups)
    return out


if __name__ == "__main__":
    write_group_bundle(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fhir_bundles_nov09"))
