from __future__ import annotations
from pydantic import BaseModel, Field, field_validator, model_validator
from enum import Enum
from typing import List, Dict, Any, Optional, Self

# ----- Enums -----------------------------------------
class WorkflowEnum(str, Enum):
    WF1_REGISTRATION = "WF1_REGISTRATION"
    WF2_PLANNING = "WF2_PLANNING"
    WF3_AGREEMENT = "WF3_AGREEMENT"
    WF4_TRANSFER = "WF4_TRANSFER"
    WF5_AUTHORIZATION = "WF5_AUTHORIZATION"
    WF6_DISPUTE_INITIATION = "WF6_DISPUTE_INITIATION"
    WF7_DISPUTE_RESPONSE = "WF7_DISPUTE_RESPONSE"
    WF8_DISCOVERY = "WF8_DISCOVERY"
    WF9_RESOLUTION = "WF9_RESOLUTION"
    WF10_JUDGMENT = "WF10_JUDGMENT"
    WF11_FINANCIAL = "WF11_FINANCIAL"
    WF12_CORPORATE = "WF12_CORPORATE"
    WF13_EMPLOYMENT = "WF13_EMPLOYMENT"
    WF14_BENEFIT = "WF14_BENEFIT"
    WF15_COMPLIANCE = "WF15_COMPLIANCE"
    WF16_IP = "WF16_IP"
    WF17_FAMILY = "WF17_FAMILY"
    WF18_EMERGENCY = "WF18_EMERGENCY"
    WF19_MODIFICATION = "WF19_MODIFICATION"
    WF20_VERIFICATION = "WF20_VERIFICATION"

class WorkflowStage(str, Enum):
    INITIATED = "INITIATED"
    UNDER_REVIEW = "UNDER_REVIEW"
    PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"

class PartyRole(str, Enum):
    PLAINTIFF = "PLAINTIFF"  # Person who files a lawsuit
    DEFENDANT = "DEFENDANT" # Person being sued
    PETITIONER = "PETITIONER" # Person filing a petition
    RESPONDENT = "RESPONDENT" # Person responding to a petition
    CREDITOR = "CREDITOR" # Person owed money
    DEBTOR = "DEBTOR" # Person who owes money
    EMPLOYER = "EMPLOYER"  # The company/person giving work
    EMPLOYEE = "EMPLOYEE" # The person doing work
    LESSOR = "LESSOR" # Person who gives something on rent
    LESSEE = "LESSEE" # Person who takes something on rent
    GRANTOR = "GRANTOR" # Person who transfers property/rights
    GRANTEE = "GRANTEE" # Person who receives property/rights
    WITNESS = "WITNESS" # Person who observes and signs
    NOTARY = "NOTARY" # Official who verifies signatures
    UNKNOWN = "UNKNOWN"


# ----- Epoch magnitude guard ------------------------------------

_UNIX_SECONDS_MAX: int = 10_000_000_000   # year ~2286 — a safe upper bound
_UNIX_SECONDS_MIN: int = 0                # no negative timestamps


def _validate_unix_seconds(value: int, field_name: str) -> int:
    """
    Rejects timestamps that are clearly in milliseconds (13+ digits).
    Raises ValueError with the correct seconds value so the agent
    can retry without guessing the right unit.
    """
    if not isinstance(value, int):
        return value   # hand off to Pydantic's type validator

    if value < _UNIX_SECONDS_MIN:
        raise ValueError(
            f"{field_name} cannot be negative. Got {value}."
        )
    if value > _UNIX_SECONDS_MAX:
        # Almost certainly milliseconds — compute the seconds equivalent so the error message gives the agent exactly what to retry with
        likely_seconds = value // 1000
        raise ValueError(
            f"{field_name} looks like milliseconds ({value}). "
            f"All timestamps must be Unix seconds (10 digits). "
            f"Likely correct value: {likely_seconds}. "
            f"Divide by 1000 before passing to this schema."
        )
    return value

# ----- Sub-models ----------------------------------------------------------

class PartySchema(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"} # Parties are immutable once extracted

    name: str
    party_role: PartyRole
    jurisdiction: Optional[str] = None # explicit Optional — jurisdiction may be unknown at extraction

class ExtractionResult(BaseModel):
    """
    What the Document Intelligence Agent produces.
    Required fields: things we must know to proceed.
    Optional fields: things that may not be present in every document type.
    """
    model_config = {"extra": "forbid"}

    # Required
    wf_id: WorkflowEnum
    confidence: float
    parties: List[PartySchema]

    # Optional at extraction time — Legal Knowledge Agent fills these in
    filing_deadline_unix: Optional[int] = None # unix timestamp
    obligation_summary: Optional[str] = None # short description of what the document requires
    raw_date_strings: Optional[List[str]] = None # preserved for Legal Knowledge Agent to parse
    
    @field_validator("confidence")
    @classmethod
    def confidence_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {v}")
        return v
    
    @field_validator("parties")
    @classmethod
    def parties_not_empty(cls, v: List[PartySchema]) -> List[PartySchema]:
        if not v:
            raise ValueError("parties cannot be empty")
        return v

    @field_validator("filing_deadline_unix", mode="before")
    @classmethod
    def validate_filing_deadline_magnitude(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        return _validate_unix_seconds(v, "filing_deadline_unix")
    
class LegalRequirementsResult(BaseModel):
    """
    What the Legal Knowledge Agent produces.
    Takes ExtractionResult + raw doc context → typed legal requirements.
    """
    model_config = {"extra": "forbid"}

    jurisdiction: str
    filing_deadline_unix: int
    statute_of_limitations_unix: Optional[int] = None
    compliance_rules: List[str]
    risk_flags: List[str]
    requires_human_approval: bool # derived from WF type (WF6, WF10, WF11 = True)

    @field_validator("filing_deadline_unix", mode="before")
    @classmethod
    def validate_filing_deadline_magnitude(cls, v: int) -> int:
        return _validate_unix_seconds(v, "filing_deadline_unix")
    
    @field_validator("statute_of_limitations_unix", mode="before")
    @classmethod
    def validate_statute_of_limitations_magnitude(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        return _validate_unix_seconds(v, "statute_of_limitations_unix")

    @model_validator(mode="after")
    def deadline_must_precede_statute_of_limitations(self) -> Self:
        if (
            self.statute_of_limitations_unix is not None
            and self.filing_deadline_unix >= self.statute_of_limitations_unix
        ):
            raise ValueError(
                f"filing_deadline_unix ({self.filing_deadline_unix}) must be "
                f"before statute_of_limitations_unix ({self.statute_of_limitations_unix}). "
                f"A filing deadline cannot fall after the limitation period expires."
            )
        return self

# ----- BlackboardState ------------------------------------------------------

class BlackboardState(BaseModel):
    """
    The single source of truth. Every agent reads from and writes to this.
    All fields typed. Version is the CAS (compare-and-swap) token.
    """
    model_config = {"frozen": True, "extra": "forbid", "validate_assignment": True} # Blackboard states are immutable, you create new versions
    
    # Identify
    case_id: str
    version: int = 0
    created_at_unix: int

    # Document Intelligence Agent
    wf_id: WorkflowEnum
    confidence: float
    parties: List[PartySchema]
    stage: WorkflowStage
    raw_date_strings: Optional[List[str]] = None

    # Legal Knowledge Agent
    jurisdiction: Optional[str] = None
    filing_deadline_unix: Optional[int] = None
    statute_of_limitations_unix: Optional[int] = None
    compliance_rules: Optional[List[str]] = None
    risk_flags: Optional[List[str]] = None
    requires_human_approval: Optional[bool] = None

    # HITL Fields - only after human acts
    human_approved: Optional[bool] = None
    decision_by: Optional[str]  = None
    decision_timestamp_unix: Optional[int]  = None
    rejection_reason: Optional[str]  = None

    # Audit
    updated_at_unix: int 
    
    #  Immutable genesis fields — blocked in evolve() ----------------------
    _IMMUTABLE_FIELDS = frozenset({"case_id", "created_at_unix", "version"})

    # Only list fields that are safe to accumulate across agents
    _APPENDABLE_FIELDS = frozenset({
        "compliance_rules",
        "risk_flags",
        "raw_date_strings",
    })

    #  Field validators -----------------------------------------------------
    @field_validator("version")
    @classmethod
    def version_must_be_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"version must be >= 0, got {v}")
        return v

    @field_validator("confidence")
    @classmethod
    def confidence_must_be_probability(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {v}")
        return v

    @field_validator(
        "created_at_unix", "updated_at_unix",
        "filing_deadline_unix", "statute_of_limitations_unix",
        "decision_timestamp_unix",
        mode="before",
    )
    @classmethod
    def validate_all_unix_magnitudes(cls, v: Optional[int], info) -> Optional[int]:
        if v is None:
            return v
        return _validate_unix_seconds(v, info.field_name)
    
    # --- Cross-field validators ------------------------------------------------
    @model_validator(mode="after")
    def validate_chronological_order(self) -> Self:
        """
        filing_deadline must precede statute_of_limitations when both present.
        Catches LLM hallucinations that produce chronologically impossible states.
        """
        if (
            self.filing_deadline_unix is not None
            and self.statute_of_limitations_unix is not None
            and self.filing_deadline_unix >= self.statute_of_limitations_unix
        ):
            raise ValueError(
                f"filing_deadline_unix ({self.filing_deadline_unix}) must be "
                f"before statute_of_limitations_unix ({self.statute_of_limitations_unix}). "
                f"Chronologically impossible state — possible LLM hallucination."
            )
        return self

    @model_validator(mode="after")
    def validate_timestamps_ordered(self) -> Self:
        """updated_at must never be before created_at."""
        if self.updated_at_unix < self.created_at_unix:
            raise ValueError(
                f"updated_at_unix ({self.updated_at_unix}) cannot be before "
                f"created_at_unix ({self.created_at_unix})."
            )
        return self

    @model_validator(mode="after")
    def validate_decision_fields_consistent(self) -> Self:
        """
        Approval:  human_approved=True  → decision_by + decision_timestamp_unix required
        Rejection: human_approved=False → decision_by + decision_timestamp_unix + rejection_reason required
        Pending:   human_approved=None  → all decision fields must be None
        """
        if self.human_approved is True:
            if not self.decision_by:
                raise ValueError(
                    "human_approved is True but decision_by is missing. "
                    "Cannot record an approval without the approver's identity."
                )
            if self.decision_timestamp_unix is None:
                raise ValueError(
                    "human_approved is True but decision_timestamp_unix is missing. "
                    "Cannot record an approval without a timestamp."
                )

        if self.human_approved is False:
            if not self.decision_by:
                raise ValueError(
                    "human_approved is False but decision_by is missing. "
                    "An anonymous rejection has no legal standing. "
                    "The rejector's identity is required for audit non-repudiation."
                )
            if self.decision_timestamp_unix is None:
                raise ValueError(
                    "human_approved is False but decision_timestamp_unix is missing. "
                    "An untimestamped rejection cannot be placed on the audit trail."
                )
            if not self.rejection_reason:
                raise ValueError(
                    "human_approved is False but rejection_reason is missing. "
                    "A rejection without a documented reason cannot be actioned."
                )

        if self.human_approved is None:
            orphaned = [
                f for f in ("decision_by", "decision_timestamp_unix", "rejection_reason")
                if getattr(self, f) is not None
            ]
            if orphaned:
                raise ValueError(
                    f"human_approved is None but these decision fields are populated: "
                    f"{orphaned}. Set human_approved explicitly before writing decision data."
                )

        return self

    # -----State transition helper -------------------------------------------
    _IMMUTABLE_FIELDS = frozenset({"case_id", "created_at_unix", "version"})

    def evolve(self, **changes) -> "BlackboardState":
        """
        Protections:
        - Genesis fields (case_id, created_at_unix, version) cannot be overwritten.
        - version is always incremented by exactly 1 — never set by caller.
        - now_unix must be injected — never sourced internally.
        - Clock regression is rejected.
        - Extra / misspelled fields raise ValidationError immediately (extra="forbid").

        Uses model_copy(update=changes) instead of model_dump() + __init__.

        Detects list fields in changes and raises a clear error
        directing callers to use append_to() instead of evolve() for
        collection mutations. Prevents accidental hard overwrites.
        """
        # Block identity hijacking
        attempted_immutable = self._IMMUTABLE_FIELDS & changes.keys()
        if attempted_immutable:
            raise ValueError(
                f"The following fields are immutable and cannot be changed via evolve(): "
                f"{sorted(attempted_immutable)}."
            )

        # Require injected time
        if "now_unix" not in changes:
            raise ValueError(
                "evolve() requires now_unix to be passed explicitly."
            )

        now_unix = changes.pop("now_unix")

        # Block clock regression
        if now_unix < self.updated_at_unix:
            raise ValueError(
                f"now_unix ({now_unix}) is before the current updated_at_unix "
                f"({self.updated_at_unix}). Clock regression detected "
                f"possible replay attack or test using stale timestamps."
            )

        # Reject unknown / misspelled field names — model_copy() silently
        # drops them, so we must enforce extra="forbid" semantics manually.
        unknown = changes.keys() - self.__class__.model_fields.keys()
        if unknown:
            from pydantic import ValidationError as _VE
            from pydantic_core import InitErrorDetails, PydanticCustomError

            raise _VE.from_exception_data(
                title=self.__class__.__name__,
                line_errors=[
                    InitErrorDetails(
                        type=PydanticCustomError(
                            "extra_forbidden",
                            "Extra inputs are not permitted",
                        ),
                        loc=(field_name,),
                        input=changes[field_name],
                    )
                    for field_name in sorted(unknown)
                ],
                input_type="python",
            )

        # catch accidental list overwrites — direct callers to append_to()
        attempted_append = self._APPENDABLE_FIELDS & changes.keys()
        if attempted_append:
            raise ValueError(
                f"Do not pass list fields directly to evolve(): {sorted(attempted_append)}. "
                f"Use append_to(field, items, now_unix=...) to add items to a collection "
                f"without overwriting existing entries. "
                f"Direct list assignment in evolve() causes silent data loss when "
                f"two agents append concurrently."
            )
        
        # zero-copy clone — unchanged fields are not re-serialised
        return self.model_copy(update={
            **changes,
            "version":         self.version + 1,
            "updated_at_unix": now_unix,
        })

    
    def append_to(
        self,
        field: str,
        items: List,
        now_unix: int,
    ) -> BlackboardState:
        """
        Reads the current list, produces a new deduplicated list with
        the additions, then calls evolve() internally. Agents never
        perform read-modify-write in their own thread-scoped memory.
        """
        if field not in self._APPENDABLE_FIELDS:
            raise ValueError(
                f"'{field}' is not an appendable collection field. "
                f"Appendable fields are: {sorted(self._APPENDABLE_FIELDS)}. "
                f"Use evolve() for scalar field updates."
            )

        if not isinstance(items, list):
            raise ValueError(
                f"append_to() requires a list of items. Got {type(items).__name__}."
            )

        current_list: List = getattr(self, field) or []

        # Deduplicate while preserving order existing items first
        seen = set(current_list)
        new_items = [item for item in items if item not in seen]
        merged = current_list + new_items

        
        return self.model_copy(update={
            field:            merged,
            "version":        self.version + 1,
            "updated_at_unix": now_unix,
        })

    @classmethod
    def initial(
        cls,
        case_id: str,
        wf_id: "WorkflowEnum",
        confidence: float,
        parties: list,
        stage: "WorkflowStage",
        now_unix: int,
        **kwargs,
    ) -> "BlackboardState":
        """
        Creates the first BlackboardState for a case (version=0).
        The only place version=0 is ever set directly.
        All subsequent states must come from evolve().
        """
        return cls(
            case_id=case_id,
            version=0,
            wf_id=wf_id,
            confidence=confidence,
            parties=parties,
            stage=stage,
            created_at_unix=now_unix,
            updated_at_unix=now_unix,
            **kwargs,
        )