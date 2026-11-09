#!/usr/bin/env python3
"""
Fail if any Patient resource contains a field that isn't on the approved
list, or an approved field whose value doesn't look anonymized. Also fails
if a Claim still carries an exact date of death (supportingInfo "deathdate").

Every leaf value in every Patient is checked against ALLOWED_PATHS. A path
that isn't listed fails, so a new field coming from upstream gets flagged
instead of silently passing through.

Usage:
  python3 check_patient_pii.py [DIR ...]
  (default: anonymized_fhir_bundles_sep22 converted_anonymized_fhir_bundles_sep22)

Exit code 0 = clean, 1 = violations found.
"""

import json
import re
import sys
from pathlib import Path

DEFAULT_DIRS = ["anonymized_fhir_bundles_nov09", "fhir_bundles_nov09"]

HASHED_ID = re.compile(r"HMAC_[0-9a-f]{32}")
CASE_ID = re.compile(r"[A-Z]+_[A-Z]{2}(?:_\d{4})?_R\d+_\d+")
PMJAY_EXT = "https://example.org/fhir/StructureDefinition/pmjay-claim-payload/"


def any_value(_v, _p):
    return True


def matches(pattern):
    rx = re.compile(pattern)
    return lambda v, _p: isinstance(v, str) and rx.fullmatch(v) is not None


def one_of(*values):
    return lambda v, _p: v in values


def identifier_value(v, patient):
    # Case/record ids are allowed in plain text; everything else must be hashed.
    if v is None:
        return True
    return isinstance(v, str) and (HASHED_ID.fullmatch(v) is not None or CASE_ID.fullmatch(v) is not None)


# path -> rule(value, patient) -> bool
ALLOWED_PATHS = {
    "resourceType": one_of("Patient"),
    "id": any_value,
    "meta.versionId": any_value,
    "meta.lastUpdated": any_value,
    "meta.source": matches(r"urn:layerkg:" + CASE_ID.pattern),
    "meta.tag[].system": one_of("urn:layerkg:origin", "urn:layerkg:document-type"),
    "meta.tag[].code": matches(r"[a-z_ &]+"),
    "text.status": one_of("generated"),
    "text.div": matches(
        r'<div xmlns="http://www\.w3\.org/1999/xhtml"><p>Patient — the subject of '
        + CASE_ID.pattern
        + r"; named nowhere in the record</p></div>"
    ),
    "identifier[].system": any_value,
    "identifier[].value": identifier_value,
    "name[].text": matches(r"PATIENT_NAME_\d{4}"),
    "name[].extension[].url": one_of("http://hl7.org/fhir/StructureDefinition/data-absent-reason"),
    "name[].extension[].valueCode": any_value,
    "telecom[].system": one_of("phone"),
    "telecom[].use": any_value,
    "telecom[].value": matches(r"PHONE_REDACTED_\d{4}"),
    "address[].text": one_of("REDACTED"),
    "gender": one_of("male", "female", "other", "unknown"),
    "birthDate": matches(r"\d{4}"),
    "deceasedBoolean": one_of(True, False),
    "deceasedDateTime": matches(r"\d{4}-\d{2}"),
    "extension[].url": one_of(
        PMJAY_EXT + "encounter.newBornflag",
        PMJAY_EXT + "encounter.bentype",
        PMJAY_EXT + "encounter.newborndetails",
    ),
    "extension[].valueString": matches(r"[A-Z]{1,3}"),
    "extension[].extension[].url": one_of(PMJAY_EXT + "newbornadditionaldocuments"),
    "extension[].extension[].extension[].url": one_of(
        PMJAY_EXT + "attachmentname",
        PMJAY_EXT + "attachmentcontent",
    ),
    "extension[].extension[].extension[].valueString": matches(r"(MASKED|REDACTED)_[A-Z0-9_.]+"),
}


def find_patients(obj):
    if isinstance(obj, dict):
        if obj.get("resourceType") == "Patient":
            yield obj
        for v in obj.values():
            yield from find_patients(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_patients(v)


def leaves(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for v in obj:
            yield from leaves(v, path + "[]")
    else:
        yield path, obj


def find_claims(obj):
    if isinstance(obj, dict):
        if obj.get("resourceType") == "Claim":
            yield obj
        for v in obj.values():
            yield from find_claims(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_claims(v)


def check_claim_death_dates(f, data) -> list[str]:
    """The exact date of death is also on Claim.supportingInfo ('deathdate')."""
    out = []
    for claim in find_claims(data):
        for info in claim.get("supportingInfo", []) or []:
            codes = {c.get("code") for c in (info.get("category") or {}).get("coding", [])}
            if "deathdate" not in codes:
                continue
            ok = (info.get("timingDate") is None or re.fullmatch(r"\d{4}-\d{2}", info["timingDate"])) and \
                 (info.get("valueString") is None or re.fullmatch(r"\d{2}/\d{4}|REDACTED", info["valueString"]))
            if not ok:
                out.append(f"{f}: Claim/{claim.get('id')}: value not anonymized: supportingInfo[deathdate]")
    return out


def check_dirs(dirs) -> list[str]:
    violations = []
    for d in dirs:
        for f in sorted(Path(d).rglob("*.json")):
            data = json.loads(f.read_text())
            violations.extend(check_claim_death_dates(f, data))
            for patient in find_patients(data):
                for path, value in leaves(patient):
                    rule = ALLOWED_PATHS.get(path)
                    if rule is None:
                        violations.append(f"{f}: Patient/{patient.get('id')}: field not on approved list: {path}")
                    elif not rule(value, patient):
                        # Don't echo the value itself: it may be the PII we're catching.
                        violations.append(f"{f}: Patient/{patient.get('id')}: value not anonymized: {path}")
    return violations


def main() -> int:
    dirs = sys.argv[1:] or DEFAULT_DIRS
    dirs = [d for d in dirs if Path(d).exists()]
    violations = check_dirs(dirs)
    for v in violations[:200]:
        print(v)
    if violations:
        print(f"\nFAILED: {len(violations)} Patient PII violations in {', '.join(dirs)}")
        return 1
    print(f"OK: no Patient PII violations in {', '.join(dirs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
