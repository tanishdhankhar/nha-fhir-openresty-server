#!/usr/bin/env python3
"""
Anonymize sensitive fields across fhir_legacy_bundles/ and fhir_v6/, writing
sanitized copies (mirroring the original directory layout) into
anonymized_fhir_bundles_nov09/. convert_to_transaction_bundles.py then turns
those into the upload-ready fhir_bundles_nov09/.

Fields anonymized:
  - Full name        (Patient.name[].text, Practitioner.name[].text)
  - Address           (Patient.address -> redacted)
  - Phone numbers      (telecom entries with system == "phone")
  - Attachment URLs    (DocumentReference.content[].attachment.url, when not
                        already masked upstream)
  - PM-JAY IDs        (Patient.identifier[].value except urn:layerkg:record
                        case ids, Coverage.subscriberId, Coverage beneficiary-id
                        identifiers) -> keyed hash "HMAC_<32 hex>"
  - Birth date        (Patient.birthDate -> year only)
  - Date of death     (Patient.deceasedDateTime and the Claim
                        supportingInfo "deathdate" -> year-month only)

Replacement is deterministic within a run: the same original value (same
Patient/Practitioner id, same phone number, same attachment URL) always maps
to the same placeholder, so repeat-patient linkage across files is preserved
without exposing the original PII.

ID hashes use HMAC-SHA256 with the secret in PII_HMAC_KEY (read from the
environment or .env). The same key always gives the same hash, so keep it
stable across runs and never commit it: without the key the hashes can't be
reversed by guessing IDs.

After writing, the output is checked with check_patient_pii.py and the
script exits non-zero if any Patient field isn't on the approved list.

Usage:
  PII_HMAC_KEY=... python3 anonymize_fhir_bundles.py
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

from check_patient_pii import check_dirs

SOURCE_DIRS = ["fhir_legacy_bundles", "fhir_v6"]
OUTPUT_ROOT = Path("anonymized_fhir_bundles_nov09")

_name_counters: dict[str, int] = {}
_name_map: dict[tuple, str] = {}
_phone_map: dict[str, str] = {}
_phone_counter = 0
_attachment_map: dict[str, str] = {}
_attachment_counter = 0
_hashed_ids: set[str] = set()

# Patient identifiers with this system are our own case/record ids, not PM-JAY
# person ids, so they stay readable.
UNHASHED_IDENTIFIER_SYSTEMS = {"urn:layerkg:record"}
BENEFICIARY_ID_SYSTEM = "https://pmjay.gov.in/fhir/beneficiary-id"


def load_hmac_key() -> bytes:
    key = os.environ.get("PII_HMAC_KEY")
    env_file = Path(".env")
    if not key and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.strip().startswith("PII_HMAC_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        sys.exit("PII_HMAC_KEY is not set (env or .env). Generate one with: python3 -c 'import secrets; print(secrets.token_hex(32))'")
    return key.encode()


_HMAC_KEY = b""


def hash_id(value):
    if not isinstance(value, str) or not value or value.startswith("HMAC_"):
        return value
    _hashed_ids.add(value)
    return "HMAC_" + hmac.new(_HMAC_KEY, value.strip().encode(), hashlib.sha256).hexdigest()[:32]


def anon_name(resource_type: str, resource_id: str, original_text: str) -> str:
    key = (resource_type, resource_id or original_text)
    if key not in _name_map:
        _name_counters[resource_type] = _name_counters.get(resource_type, 0) + 1
        _name_map[key] = f"{resource_type.upper()}_NAME_{_name_counters[resource_type]:04d}"
    return _name_map[key]


def anon_phone(value: str) -> str:
    global _phone_counter
    if value not in _phone_map:
        _phone_counter += 1
        _phone_map[value] = f"PHONE_REDACTED_{_phone_counter:04d}"
    return _phone_map[value]


def anon_attachment_url(url: str) -> str:
    global _attachment_counter
    if url not in _attachment_map:
        _attachment_counter += 1
        ext = Path(url).suffix
        _attachment_map[url] = f"REDACTED_ATTACHMENT_{_attachment_counter:04d}{ext}"
    return _attachment_map[url]


def coarsen_ddmmyyyy(value: str) -> str:
    """'29/05/2026' -> '05/2026'. Anything else is dropped to 'REDACTED'."""
    parts = value.strip().split()[0].split("/") if value.strip() else []
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{parts[1]}/{parts[2]}"
    return "REDACTED"


def anonymize_resource(resource: dict) -> None:
    rtype = resource.get("resourceType")
    rid = resource.get("id")

    if rtype in ("Patient", "Practitioner"):
        for n in resource.get("name", []) or []:
            if n.get("text"):
                n["text"] = anon_name(rtype, rid, n["text"])

    if rtype == "Patient" and resource.get("address"):
        resource["address"] = [{"text": "REDACTED"} for _ in resource["address"]]

    if rtype == "Patient":
        for ident in resource.get("identifier", []) or []:
            if ident.get("value") and ident.get("system") not in UNHASHED_IDENTIFIER_SYSTEMS:
                ident["value"] = hash_id(ident["value"])
        if isinstance(resource.get("birthDate"), str):
            resource["birthDate"] = resource["birthDate"][:4]
        if isinstance(resource.get("deceasedDateTime"), str):
            resource["deceasedDateTime"] = resource["deceasedDateTime"][:7]

    if rtype == "Claim":
        for info in resource.get("supportingInfo", []) or []:
            codes = {c.get("code") for c in (info.get("category") or {}).get("coding", [])}
            if "deathdate" in codes:
                if isinstance(info.get("timingDate"), str):
                    info["timingDate"] = info["timingDate"][:7]
                if isinstance(info.get("valueString"), str):
                    info["valueString"] = coarsen_ddmmyyyy(info["valueString"])

    if rtype == "Coverage":
        if resource.get("subscriberId"):
            resource["subscriberId"] = hash_id(resource["subscriberId"])
        for ident in resource.get("identifier", []) or []:
            if ident.get("value") and ident.get("system") == BENEFICIARY_ID_SYSTEM:
                ident["value"] = hash_id(ident["value"])

    for t in resource.get("telecom", []) or []:
        if t.get("system") == "phone" and t.get("value"):
            t["value"] = anon_phone(t["value"])

    if rtype == "DocumentReference":
        for c in resource.get("content", []) or []:
            att = c.get("attachment", {})
            url = att.get("url")
            if url and "MASKED" not in url:
                att["url"] = anon_attachment_url(url)


def process_file(src: Path, dst: Path) -> None:
    data = json.loads(src.read_text())
    entries = data.get("entry", []) if data.get("resourceType") == "Bundle" else [{"resource": data}]
    for entry in entries:
        resource = entry.get("resource")
        if resource:
            anonymize_resource(resource)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data, indent=2))


def main() -> int:
    global _HMAC_KEY
    _HMAC_KEY = load_hmac_key()
    total = 0
    for src_dir in SOURCE_DIRS:
        src_root = Path(src_dir)
        for src_file in src_root.rglob("*.json"):
            rel = src_file.relative_to(src_root.parent)
            process_file(src_file, OUTPUT_ROOT / rel)
            total += 1

    print(f"Anonymized {total} files -> {OUTPUT_ROOT}")
    print(f"  Names replaced           : {sum(_name_counters.values())} ({dict(_name_counters)})")
    print(f"  Phone numbers replaced   : {_phone_counter}")
    print(f"  Attachment URLs replaced : {_attachment_counter}")
    print(f"  PM-JAY IDs hashed        : {len(_hashed_ids)} distinct")

    violations = check_dirs([OUTPUT_ROOT])
    for v in violations[:200]:
        print(v)
    if violations:
        print(f"FAILED: {len(violations)} Patient PII violations in {OUTPUT_ROOT}")
        return 1
    print(f"PII check passed for {OUTPUT_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
