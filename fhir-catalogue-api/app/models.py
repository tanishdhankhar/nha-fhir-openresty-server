from pydantic import BaseModel, Field


class SnomedCode(BaseModel):
    code: str = Field(..., description="SNOMED CT concept identifier", example="53741008")
    display: str = Field(..., description="Human-readable SNOMED concept name", example="Coronary arteriosclerosis")
    patient_count: int | None = Field(
        None,
        description="Number of the dataset's member patients with this condition (null until refresh_catalogue.py has run)",
        example=17,
    )


class Package(BaseModel):
    id: str = Field(..., description="Dataset id: the claim group name", example="DEATHCLAIMS")
    label: str = Field(..., description="Display name", example="Deathcases")
    description: str = Field(..., description="Brief description of the package scope")
    procedure: str = Field(..., description="Primary procedure covered (N/A for claim-group datasets)", example="N/A")
    procedure_snomed_code: str = Field(..., description="SNOMED CT code for the procedure (N/A for claim-group datasets)", example="N/A")
    pre_auth_ref: str = Field(..., description="Pre-authorisation reference used in FHIR claims", example="N/A")
    snomed_code_count: int = Field(..., description="Number of SNOMED conditions in catalogue for this package")
    bundle_count: int = Field(..., description="Patient bundles in this dataset (the claim group's member patients)", example=66)
    group_id: str | None = Field(None, description="FHIR Group id of the claim group behind this dataset", example="claim-group-deathclaims")
    patient_count: int | None = Field(None, description="Member patients in the claim group", example=66)
    claim_count: int | None = Field(None, description="Claims across the claim group's members", example=66)


class PackageDetail(Package):
    snomed_codes: list[SnomedCode] = Field(..., description="All SNOMED conditions catalogued for this package")


class PatientRecord(BaseModel):
    patient_id: str = Field(..., description="FHIR Patient resource ID", example="6721c0cb-10e1-22ad-7729-4fb17edd3461")
    name: str = Field(..., description="Patient name (anonymized placeholder)", example="PATIENT_NAME_0001")
    gender: str = Field(..., description="Patient gender", example="male")
    birth_date: str = Field(..., description="Year of birth (YYYY; anonymized to the year)", example="1981")
    condition_code: str = Field(..., description="SNOMED code of the diagnosed condition", example="53741008")
    condition_display: str = Field(..., description="Display name of the condition", example="Coronary arteriosclerosis")
    condition_status: str = Field(..., description="Clinical status of the condition", example="active")


class PatientQueryResult(BaseModel):
    package: str = Field(..., description="Dataset (claim group) queried", example="DEATHCLAIMS")
    snomed_code: str = Field(..., description="SNOMED code filtered on", example="53741008")
    snomed_display: str = Field(..., description="Display name of the SNOMED code", example="Coronary arteriosclerosis")
    total: int = Field(..., description="Total number of matching patient records")
    patients: list[PatientRecord]
    raw: dict = Field(
        ...,
        description="Raw, unmodified FHIR Bundle containing every Condition and Patient resource backing this result",
    )


class PatientMultiQuery(BaseModel):
    package: str = Field(..., description="Dataset (claim group name) to scope the query to", example="DEATHCLAIMS")
    snomed_codes: list[str] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="One or more SNOMED CT concept codes — all must belong to the given package",
        example=["53741008", "709044004"],
    )


class PatientMultiQueryResult(BaseModel):
    package: str = Field(..., description="Dataset (claim group) queried", example="DEATHCLAIMS")
    snomed_codes: list[str] = Field(..., description="SNOMED codes that were queried")
    invalid_codes: list[str] = Field(
        ...,
        description="Submitted codes rejected because they are not in the package catalogue",
    )
    total: int = Field(..., description="Total number of matching patient records across all codes")
    patients: list[PatientRecord]
    raw: dict = Field(
        ...,
        description="Raw, unmodified FHIR Bundle containing every Condition and Patient resource backing this result",
    )


class HealthResponse(BaseModel):
    status: str = Field(..., example="ok")
    fhir_reachable: bool = Field(..., description="Whether the FHIR server responded to a ping")
    fhir_base_url: str = Field(..., description="Configured FHIR base URL")


class ErrorResponse(BaseModel):
    detail: str = Field(..., description="Error message", example="Package 'XY001A' not found in catalogue")


class FHIRQueryExecuteRequest(BaseModel):
    resource_type: str = Field(
        ...,
        description="FHIR resource type the query targets — the query string must start with this type.",
        example="Patient",
    )
    query: str = Field(
        ...,
        description=(
            "Full FHIR R4 search query string (relative, no leading slash), e.g. "
            "'Patient?_has:Condition:patient:code=http://snomed.info/sct|44054006&birthdate=ge1966-01-01' "
            "or a 'Patient?_filter=...' expression."
        ),
        example="Patient?_has:Condition:patient:code=http://snomed.info/sct|44054006&birthdate=ge1966-01-01",
    )


# ---------------------------------------------------------------------------
# Claim groups
# ---------------------------------------------------------------------------


class ClaimGroup(BaseModel):
    id: str = Field(..., description="FHIR Group resource id", example="claim-group-deathclaims")
    name: str = Field(..., description="Group name", example="DEATHCLAIMS")
    description: str = Field(..., description="What the group holds, from the Group narrative")
    patient_count: int = Field(..., description="Number of member patients", example=66)
    claim_count: int = Field(..., description="Number of claims across all members", example=66)
    last_updated: str | None = Field(None, description="Group.meta.lastUpdated on the FHIR server")


class GroupMember(BaseModel):
    patient_id: str = Field(..., description="FHIR Patient id of the member", example="legacy-11404")
    claim_ids: list[str] = Field(..., description="FHIR Claim ids that put this patient in the group", example=["legacy-11409"])


class ClaimGroupMembers(BaseModel):
    group_id: str = Field(..., example="claim-group-deathclaims")
    group_name: str = Field(..., example="DEATHCLAIMS")
    offset: int = Field(..., description="Index of the first member on this page", example=0)
    limit: int = Field(..., description="Page size", example=100)
    total: int = Field(..., description="Total members in the group", example=66)
    has_more: bool = Field(..., description="Whether more members exist past this page")
    members: list[GroupMember]


class GroupMemberBundle(GroupMember):
    bundle: dict = Field(
        ...,
        description="Every resource for this member (Patient/{id}/$everything), as one FHIR searchset Bundle",
    )


class ClaimGroupBundles(BaseModel):
    group_id: str = Field(..., example="claim-group-deathclaims")
    group_name: str = Field(..., example="DEATHCLAIMS")
    offset: int = Field(..., description="Index of the first member on this page", example=0)
    limit: int = Field(..., description="Members per page", example=10)
    total: int = Field(..., description="Total members in the group", example=66)
    has_more: bool = Field(..., description="Whether more members exist past this page")
    bundles: list[GroupMemberBundle]
