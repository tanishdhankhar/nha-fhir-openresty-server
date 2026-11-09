import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .catalogue import (
    get_all_packages,
    get_member_ids,
    get_package,
    get_snomed_codes,
    get_snomed_display,
    is_valid_snomed_for_package,
)
from .config import Settings, get_settings
from .fhir import FHIRClient, FHIRUnavailable
from .models import (
    ClaimGroup,
    ClaimGroupBundles,
    ClaimGroupMembers,
    ErrorResponse,
    FHIRQueryExecuteRequest,
    GroupMember,
    GroupMemberBundle,
    HealthResponse,
    Package,
    PackageDetail,
    PatientMultiQuery,
    PatientMultiQueryResult,
    PatientQueryResult,
    SnomedCode,
)

# FHIR resource types the generic query executor is allowed to target.
# Defense-in-depth only — the real authorization gate is the ABAC approval
# flow in omop-auth, which decides which query strings ever reach here.
ALLOWED_QUERY_RESOURCE_TYPES = {
    "Patient",
    "Condition",
    "Observation",
    "Encounter",
    "MedicationRequest",
    "Procedure",
    "DiagnosticReport",
    "AllergyIntolerance",
    "Immunization",
    "CarePlan",
    "Coverage",
    "Claim",
}


def _snomed_codes(raw_codes: list[dict]) -> list[SnomedCode]:
    # patient_count is the number of the package's (group's) members with the
    # code, computed by refresh_catalogue.py — a live FHIR count would cover
    # every patient on the server, not just this group.
    return [SnomedCode(code=c["code"], display=c["display"], patient_count=c.get("patient_count")) for c in raw_codes]


def _validate_execute_query(resource_type: str, query: str) -> None:
    if resource_type not in ALLOWED_QUERY_RESOURCE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported resource_type '{resource_type}'")
    if query.startswith("/") or query.startswith("http://") or query.startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail="query must be a relative FHIR search query string, not an absolute URL or path",
        )
    if not (query == resource_type or query.startswith(f"{resource_type}?")):
        raise HTTPException(
            status_code=400,
            detail=f"query must target resource type '{resource_type}' (expected it to start with '{resource_type}?')",
        )

# ---------------------------------------------------------------------------
# FHIR client lifecycle
# ---------------------------------------------------------------------------

_fhir_client: FHIRClient | None = None


def get_fhir_client() -> FHIRClient:
    if _fhir_client is None:
        raise RuntimeError("FHIR client not initialised")
    return _fhir_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _fhir_client
    settings = get_settings()
    _fhir_client = FHIRClient(
        base_url=settings.fhir_base_url,
        username=settings.fhir_username,
        password=settings.fhir_password,
        cache_ttl=settings.count_cache_ttl,
    )
    await db.init_pool(settings.database_url)
    yield
    await db.close_pool()
    await _fhir_client.close()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

settings = get_settings()

tags_metadata = [
    {
        "name": "packages",
        "description": (
            "Browse the datasets. Each package is one claim group on the FHIR server "
            "(`DEATHCLAIMS` — shown as Deathcases — and `NAFU`); its id is the group name. "
            "A package's SNOMED codes are the attributes found in its members' Conditions, "
            "each with the number of members who have it."
        ),
    },
    {
        "name": "patients",
        "description": (
            "Query live patient records from HAPI FHIR. "
            "Filter by package and SNOMED condition code to retrieve matching patient demographics and condition details."
        ),
    },
    {
        "name": "queries",
        "description": (
            "Execute pre-built FHIR R4 search queries (e.g. ones produced by a "
            "text-to-query pipeline and approved via omop-auth's ABAC flow) "
            "directly against HAPI FHIR."
        ),
    },
    {
        "name": "groups",
        "description": (
            "Claim groups stored as FHIR Group resources (e.g. DEATHCLAIMS, NAFU). "
            "Each member is a Patient, linked to the claim(s) that put them in the group. "
            "`/groups/{group_id}/bundles` returns every member's data, so a caller with "
            "access to a group can read every bundle in it. **No authorization is "
            "performed here** — omop-auth checks the caller holds an approved group "
            "grant before it calls these endpoints."
        ),
    },
    {
        "name": "health",
        "description": "Service and FHIR server liveness checks.",
    },
]

app = FastAPI(
    title=settings.app_title,
    description=settings.app_description,
    version=settings.app_version,
    openapi_tags=tags_metadata,
    license_info={"name": "MIT"},
    contact={"name": "NHA FHIR Platform"},
    lifespan=lifespan,
    # Swagger UI at /docs, ReDoc at /redoc, raw spec at /openapi.json.
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

@app.exception_handler(FHIRUnavailable)
async def fhir_unavailable(_request, exc: FHIRUnavailable):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    tags=["health"],
    summary="Service health check",
    description=(
        "Returns the liveness status of this API and whether the configured "
        "HAPI FHIR server is reachable. Safe to call without authentication."
    ),
    response_model=HealthResponse,
)
async def health(
    fhir: FHIRClient = Depends(get_fhir_client),
    cfg: Settings = Depends(get_settings),
):
    reachable = await fhir.ping()
    return HealthResponse(
        status="ok",
        fhir_reachable=reachable,
        fhir_base_url=cfg.fhir_base_url,
    )


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

@app.get(
    "/packages",
    tags=["packages"],
    summary="List all datasets (claim groups)",
    description=(
        "Returns one package per claim group on the FHIR server (Deathcases, NAFU), "
        "with its label, description, number of SNOMED attributes and member counts."
    ),
    response_model=list[Package],
    responses={502: {"model": ErrorResponse, "description": "FHIR server unavailable"}},
)
async def list_packages(fhir: FHIRClient = Depends(get_fhir_client)):
    return [Package(**p) for p in await get_all_packages(fhir)]


@app.get(
    "/packages/{package_id}",
    tags=["packages"],
    summary="Get a dataset (claim group) with its SNOMED attributes",
    description=(
        "Returns one package — `package_id` is the group name (`DEATHCLAIMS`) or its "
        "FHIR Group id — with every SNOMED attribute found in its members' Conditions "
        "and how many members have each. Run `refresh_catalogue.py` after loading data "
        "to update the attributes."
    ),
    response_model=PackageDetail,
    responses={404: {"model": ErrorResponse, "description": "Package not found"}},
)
async def get_package_detail(
    package_id: str,
    fhir: FHIRClient = Depends(get_fhir_client),
):
    pkg = await get_package(fhir, package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail=f"Package '{package_id}' not found in catalogue")

    raw_codes = await get_snomed_codes(fhir, package_id) or []
    return PackageDetail(**pkg, snomed_codes=_snomed_codes(raw_codes))


@app.get(
    "/packages/{package_id}/snomed-codes",
    tags=["packages"],
    summary="List SNOMED attributes for a dataset (claim group)",
    description=(
        "Returns every SNOMED attribute found in the package's members' Conditions, "
        "each with the number of members who have it."
    ),
    response_model=list[SnomedCode],
    responses={404: {"model": ErrorResponse, "description": "Package not found"}},
)
async def list_snomed_codes(
    package_id: str,
    fhir: FHIRClient = Depends(get_fhir_client),
):
    raw_codes = await get_snomed_codes(fhir, package_id)
    if raw_codes is None:
        raise HTTPException(status_code=404, detail=f"Package '{package_id}' not found in catalogue")

    return _snomed_codes(raw_codes)


# ---------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------

@app.get(
    "/patients",
    tags=["patients"],
    summary="Query patient records by package and SNOMED condition",
    description=(
        "Retrieves patient records from HAPI FHIR for the given package (claim group) "
        "and SNOMED condition code: `Condition` resources with the code, with their "
        "`Patient` included, kept only for patients who are members of the group. \n\n"
        "The requested SNOMED code must be one of the package's attributes — "
        "otherwise `422`."
    ),
    response_model=PatientQueryResult,
    responses={
        404: {"model": ErrorResponse, "description": "Package not found"},
        422: {"model": ErrorResponse, "description": "SNOMED code not valid for this package"},
    },
)
async def query_patients(
    package: str = Query(..., description="Dataset id: claim group name (e.g. DEATHCLAIMS)", example="DEATHCLAIMS"),
    snomed_code: str = Query(..., description="SNOMED CT concept code", example="53741008"),
    fhir: FHIRClient = Depends(get_fhir_client),
):
    pkg = await get_package(fhir, package)
    if not pkg:
        raise HTTPException(status_code=404, detail=f"Package '{package}' not found in catalogue")

    if not await is_valid_snomed_for_package(fhir, package, snomed_code):
        raise HTTPException(
            status_code=422,
            detail=f"SNOMED code '{snomed_code}' is not in the catalogue for package '{package}'",
        )

    display = await get_snomed_display(fhir, package, snomed_code) or snomed_code
    members = await get_member_ids(fhir, package)
    total, patients, raw = await fhir.get_patients(members, snomed_code)

    return PatientQueryResult(
        package=pkg["id"],
        snomed_code=snomed_code,
        snomed_display=display,
        total=total,
        patients=patients,
        raw=raw,
    )


@app.post(
    "/fhir/queries/execute",
    tags=["queries"],
    summary="Execute a pre-built FHIR R4 search query",
    description=(
        "Runs an arbitrary, already-built FHIR R4 search query string against the "
        "upstream HAPI FHIR server and returns the resulting Bundle (or "
        "OperationOutcome on failure) verbatim, with the upstream status code "
        "passed through unchanged. \n\n"
        "**No authorization is performed here.** Callers are expected to have "
        "already verified the requester holds an approved grant for this exact "
        "query — in this platform that check happens in omop-auth's FHIR-query "
        "ABAC flow before it ever calls this endpoint."
    ),
    responses={400: {"model": ErrorResponse, "description": "Malformed or disallowed query"}},
)
async def execute_fhir_query(
    body: FHIRQueryExecuteRequest,
    fhir: FHIRClient = Depends(get_fhir_client),
):
    _validate_execute_query(body.resource_type, body.query)
    status_code, result = await fhir.execute_query(body.query)
    return JSONResponse(status_code=status_code, content=result)


@app.post(
    "/patients/query",
    tags=["patients"],
    summary="Query patients by package and multiple SNOMED codes",
    description=(
        "Accepts a list of SNOMED codes and retrieves all patient records matching "
        "**any** of them within the given package (OR logic). Only members of the "
        "package's claim group are returned. \n\n"
        "**Single FHIR round-trip:** codes are passed as a comma-separated token "
        "list in one `code` parameter — FHIR treats this as OR natively. \n\n"
        "Codes not found in the package catalogue are returned in `invalid_codes` "
        "and silently excluded from the query rather than rejecting the whole request. "
        "Each patient record includes `condition_code` so you can see which SNOMED "
        "code matched that patient."
    ),
    response_model=PatientMultiQueryResult,
    responses={
        404: {"model": ErrorResponse, "description": "Package not found"},
        422: {"model": ErrorResponse, "description": "No valid SNOMED codes after filtering"},
    },
)
async def query_patients_multi(
    body: PatientMultiQuery,
    fhir: FHIRClient = Depends(get_fhir_client),
):
    pkg = await get_package(fhir, body.package)
    if not pkg:
        raise HTTPException(status_code=404, detail=f"Package '{body.package}' not found in catalogue")

    package_codes = {c["code"] for c in (await get_snomed_codes(fhir, body.package) or [])}
    valid_codes = [c for c in body.snomed_codes if c in package_codes]
    invalid_codes = [c for c in body.snomed_codes if c not in valid_codes]

    if not valid_codes:
        raise HTTPException(
            status_code=422,
            detail=f"None of the submitted SNOMED codes belong to package '{body.package}'",
        )

    members = await get_member_ids(fhir, body.package)
    total, patients, raw = await fhir.get_patients_multi(members, valid_codes)

    return PatientMultiQueryResult(
        package=pkg["id"],
        snomed_codes=valid_codes,
        invalid_codes=invalid_codes,
        total=total,
        patients=patients,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Claim groups
# ---------------------------------------------------------------------------

# Members fetched concurrently per /bundles page; each one is a $everything
# call (with paging), so keep this modest to avoid hammering HAPI.
_BUNDLE_CONCURRENCY = 5


async def _group_or_404(fhir: FHIRClient, group_id: str) -> dict:
    try:
        group = await fhir.get_claim_group(group_id)
    except FHIRUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not group:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
    return group


def _summary(group: dict) -> ClaimGroup:
    return ClaimGroup(**{k: v for k, v in group.items() if k != "members"})


@app.get(
    "/groups",
    tags=["groups"],
    summary="List claim groups",
    description=(
        "Returns every claim Group on the FHIR server (Group resources with the "
        "`https://example.org/fhir/CodeSystem/claim-group` identifier), with member "
        "and claim counts. Cached for `COUNT_CACHE_TTL` seconds."
    ),
    response_model=list[ClaimGroup],
    responses={502: {"model": ErrorResponse, "description": "FHIR server unavailable"}},
)
async def list_groups(fhir: FHIRClient = Depends(get_fhir_client)):
    try:
        groups = await fhir.get_claim_groups()
    except FHIRUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [_summary(g) for g in groups]


@app.get(
    "/groups/{group_id}",
    tags=["groups"],
    summary="Get one claim group",
    description="`group_id` is the FHIR Group id (`claim-group-deathclaims`) or the group name (`DEATHCLAIMS`).",
    response_model=ClaimGroup,
    responses={
        404: {"model": ErrorResponse, "description": "Group not found"},
        502: {"model": ErrorResponse, "description": "FHIR server unavailable"},
    },
)
async def get_group(group_id: str, fhir: FHIRClient = Depends(get_fhir_client)):
    return _summary(await _group_or_404(fhir, group_id))


@app.get(
    "/groups/{group_id}/members",
    tags=["groups"],
    summary="List a claim group's members",
    description="Pages through the group's member patients and the claim ids that put each one in the group.",
    response_model=ClaimGroupMembers,
    responses={
        404: {"model": ErrorResponse, "description": "Group not found"},
        502: {"model": ErrorResponse, "description": "FHIR server unavailable"},
    },
)
async def list_group_members(
    group_id: str,
    offset: int = Query(0, ge=0, description="Index of the first member to return"),
    limit: int = Query(100, ge=1, le=1000, description="Members per page"),
    fhir: FHIRClient = Depends(get_fhir_client),
):
    group = await _group_or_404(fhir, group_id)
    members = group["members"]
    page = members[offset:offset + limit]
    return ClaimGroupMembers(
        group_id=group["id"],
        group_name=group["name"],
        offset=offset,
        limit=limit,
        total=len(members),
        has_more=offset + limit < len(members),
        members=[GroupMember(**m) for m in page],
    )


@app.get(
    "/groups/{group_id}/bundles",
    tags=["groups"],
    summary="Get every bundle in a claim group (paged by member)",
    description=(
        "Returns one FHIR Bundle per member patient on this page, built from "
        "`Patient/{id}/$everything` (so it holds the patient's claims, encounters, "
        "conditions, documents and so on). Page through with `offset` / `limit` "
        "until `has_more` is false to read every bundle in the group. \n\n"
        "Access is patient-level: if a patient is in more than one group, their "
        "bundle includes all of their claims. \n\n"
        "**No authorization is performed here** — omop-auth checks the caller holds "
        "an approved grant for this group before calling."
    ),
    response_model=ClaimGroupBundles,
    responses={
        404: {"model": ErrorResponse, "description": "Group not found"},
        502: {"model": ErrorResponse, "description": "FHIR server unavailable"},
    },
)
async def get_group_bundles(
    group_id: str,
    offset: int = Query(0, ge=0, description="Index of the first member to return"),
    limit: int = Query(10, ge=1, le=50, description="Members (bundles) per page"),
    fhir: FHIRClient = Depends(get_fhir_client),
):
    group = await _group_or_404(fhir, group_id)
    members = group["members"]
    page = members[offset:offset + limit]
    semaphore = asyncio.Semaphore(_BUNDLE_CONCURRENCY)

    async def fetch(member: dict) -> GroupMemberBundle:
        async with semaphore:
            bundle = await fhir.get_patient_everything(member["patient_id"])
        return GroupMemberBundle(**member, bundle=bundle)

    try:
        bundles = list(await asyncio.gather(*(fetch(m) for m in page)))
    except FHIRUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ClaimGroupBundles(
        group_id=group["id"],
        group_name=group["name"],
        offset=offset,
        limit=limit,
        total=len(members),
        has_more=offset + limit < len(members),
        bundles=bundles,
    )
