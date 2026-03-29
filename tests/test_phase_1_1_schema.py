import pytest
import time
from pydantic import ValidationError
from schemas.blackboard import (
    BlackboardState, WorkflowEnum, WorkflowStage,
    PartySchema, PartyRole, ExtractionResult, LegalRequirementsResult
)
from schemas.validators import validate_deadline, DeadlineStatus, is_historical_document

def make_valid_state() -> dict:
    now = int(time.time())
    return {
        "case_id": "CASE-001",
        "version": 0,
        "wf_id": WorkflowEnum.WF6_DISPUTE_INITIATION,
        "confidence": 0.95,
        "parties": [
            {"name": "John Smith", "party_role": PartyRole.PLAINTIFF},
            {"name": "Acme Corp", "party_role": PartyRole.DEFENDANT},
        ],
        "stage": WorkflowStage.INITIATED,
        "created_at_unix": now,
        "updated_at_unix": now,
    }

def test_valid_state_instantiates():
    state = BlackboardState(**make_valid_state())
    assert state.wf_id == WorkflowEnum.WF6_DISPUTE_INITIATION
    assert state.version == 0

def test_string_date_rejected_in_blackboard():
    data = make_valid_state()
    data["filing_deadline_unix"] = "October 12th"   # the exact failure case
    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "filing_deadline_unix" in str(exc_info.value)

def test_string_date_rejected_in_legal_requirements():
    with pytest.raises(ValidationError):
        LegalRequirementsResult(
            jurisdiction="California",
            filing_deadline_unix="October 12th",  # must fail
            compliance_rules=[],
            risk_flags=[],
            requires_human_approval=True,
        )

def test_confidence_out_of_range_rejected():
    data = make_valid_state()
    data["confidence"] = 1.5
    with pytest.raises(ValidationError):
        BlackboardState(**data)

def test_blackboard_is_immutable():
    state = BlackboardState(**make_valid_state())
    with pytest.raises(Exception):
        state.version = 1   # frozen model — must raise

def test_extraction_result_requires_at_least_one_party():
    with pytest.raises(ValidationError):
        ExtractionResult(
            wf_id=WorkflowEnum.WF6_DISPUTE_INITIATION,
            confidence=0.9,
            parties=[],   # must fail
        )

def test_legal_requirments_accepts_past_deadline():
    past_unix = 1_000_000  # May 1970
    result = LegalRequirementsResult(
        jurisdiction="California",
        filing_deadline_unix=past_unix,
        compliance_rules=[],
        risk_flags=[],
        requires_human_approval=True,
    )
    assert result.filing_deadline_unix == past_unix

def test_legal_requirments_still_rejects_string_deadline():
    with pytest.raises(ValidationError):
        LegalRequirementsResult(
            jurisdiction="California",
            filing_deadline_unix="October 12th",  # must fail
            compliance_rules=[],
            risk_flags=[],
            requires_human_approval=True,
        )

def test_deadline_validator_is_deterministic():
    deadline = 1_800_000_000  # a fixed future timestamp
    now      = 1_700_000_000  # a fixed reference time

    result1 = validate_deadline(deadline, now_unix = now)
    result2 = validate_deadline(deadline, now_unix = now)

    assert result1 == result2
    assert result1.status == DeadlineStatus.FUTURE

def test_historical_document_deadline_evaluates_correctly():
    """A 2023 deadline extracted from an audit document must not crash."""
    may_2023  = 1_682_899_200   # May 1 2023 00:00 UTC
    jan_2025  = 1_735_689_600   # Jan 1 2025 00:00 UTC — our injected "now"

    result = validate_deadline(may_2023, now_unix=jan_2025)

    assert result.status == DeadlineStatus.PAST
    assert result.days_overdue is not None
    assert result.days_overdue > 0
    assert result.days_remaining is None

def test_imminent_deadline_triggers_escalation():
    now      = 1_700_000_000
    deadline = now + (48 * 3600)   # 48 hours from now — within 72h threshold

    result = validate_deadline(deadline, now_unix=now)

    assert result.status == DeadlineStatus.IMMINENT
    assert result.requires_escalation is True

def test_is_historical_document():
    past   = 1_000_000
    future = 9_999_999_999
    now    = 1_700_000_000

    assert is_historical_document(past, now_unix=now) is True
    assert is_historical_document(future, now_unix=now) is False   

def test_chronological_paradox_rejected_in_legal_requirements():
    """
    Statute of limitations cannot expire before the filing deadline.
    LLM hallucination producing this state must be caught at schema level.
    """
    filing   = 1_800_000_000   # later timestamp
    statute  = 1_700_000_000   # earlier timestamp — paradox

    with pytest.raises(ValidationError) as exc_info:
        LegalRequirementsResult(
            jurisdiction="California",
            filing_deadline_unix=filing,
            statute_of_limitations_unix=statute,   # statute expires before deadline
            compliance_rules=[],
            risk_flags=[],
            requires_human_approval=True,
        )
    assert "statute_of_limitations" in str(exc_info.value).lower() or \
           "filing_deadline" in str(exc_info.value).lower()

def test_chronological_paradox_rejected_in_blackboard():
    """Same paradox caught at BlackboardState level."""
    data = make_valid_state()
    now  = int(time.time())
    data["filing_deadline_unix"]        = now + 200_000   # further in future
    data["statute_of_limitations_unix"] = now + 100_000   # expires sooner — paradox

    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "statute_of_limitations" in str(exc_info.value).lower() or \
           "Chronologically impossible" in str(exc_info.value)

def test_timestamps_ordered_in_blackboard():
    """updated_at before created_at is an impossible audit state."""
    data = make_valid_state()
    data["created_at_unix"] = 1_700_000_100
    data["updated_at_unix"] = 1_700_000_000   # before created — impossible

    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "updated_at" in str(exc_info.value).lower()

def test_approval_true_without_approver_rejected():
    """Approved audit trail with no approver identity is invalid."""
    data = make_valid_state()
    data["human_approved"] = True
    data["decision_by"]    = None   # missing — must fail
    data["decision_timestamp_unix"] = int(time.time()) + 1000

    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "decision_by" in str(exc_info.value).lower()

def test_approval_true_without_timestamp_rejected():
    """Approved audit trail with no timestamp is invalid."""
    data = make_valid_state()
    data["human_approved"]          = True
    data["decision_by"]             = "judge.smith@court.gov"
    data["decision_timestamp_unix"] = None   # missing — must fail

    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "decision_timestamp_unix" in str(exc_info.value).lower()

def test_approval_false_without_reason_rejected():
    """Rejection with no documented reason is invalid."""
    data = make_valid_state()
    data["human_approved"]   = False
    data["rejection_reason"] = None   # missing — must fail

    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "rejection_reason" in str(exc_info.value).lower()

def test_hitl_fields_without_decision_rejected():
    """
    Orphaned HITL fields with human_approved=None is an inconsistent state.
    Prevents partial writes from corrupting the audit trail.
    """
    data = make_valid_state()
    data["human_approved"] = None
    data["decision_by"]    = "someone@court.gov"   # orphaned — must fail

    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "decision_by" in str(exc_info.value).lower()

def test_valid_approval_state_accepted():
    """A fully consistent approval state must pass all cross-field checks."""
    data = make_valid_state()
    now  = int(time.time())
    data["human_approved"]          = True
    data["decision_by"]             = "judge.smith@court.gov"
    data["decision_timestamp_unix"] = now + 500

    state = BlackboardState(**data)
    assert state.human_approved is True
    assert state.decision_by == "judge.smith@court.gov"



def test_legal_requirements_valid_chronological_order():
    """
    filing_deadline before statute_of_limitations is the correct order.
    Must be accepted without error.
    """
    result = LegalRequirementsResult(
        jurisdiction="California",
        filing_deadline_unix=1_700_000_000,     # earlier — correct
        statute_of_limitations_unix=1_800_000_000,  # later — correct
        compliance_rules=[],
        risk_flags=[],
        requires_human_approval=True,
    )
    assert result.filing_deadline_unix < result.statute_of_limitations_unix



def make_initial_state(now: int) -> BlackboardState:
    """Helper that uses the named constructor — replaces make_valid_state() calls
    that need a real BlackboardState object rather than a raw dict."""
    return BlackboardState.initial(
        case_id="CASE-001",
        wf_id=WorkflowEnum.WF6_DISPUTE_INITIATION,
        confidence=0.95,
        parties=[
            PartySchema(name="John Smith", party_role=PartyRole.PLAINTIFF),
            PartySchema(name="Acme Corp",  party_role=PartyRole.DEFENDANT),
        ],
        stage=WorkflowStage.INITIATED,
        now_unix=now,
    )


def test_initial_state_is_version_zero():
    """Named constructor always produces version=0."""
    now = 1_700_000_000
    state = make_initial_state(now)
    assert state.version == 0
    assert state.created_at_unix == now
    assert state.updated_at_unix == now


def test_evolve_increments_version():
    """evolve() always produces version+1 — never skips, never jumps."""
    now = 1_700_000_000
    state_v0 = make_initial_state(now)
    state_v1 = state_v0.evolve(
        jurisdiction="California",
        now_unix=now + 100,
    )
    assert state_v0.version == 0   # original unchanged
    assert state_v1.version == 1   # new state incremented


def test_evolve_carries_forward_unchanged_fields():
    """Fields not mentioned in evolve() are preserved from the parent state."""
    now = 1_700_000_000
    state_v0 = make_initial_state(now)
    state_v1 = state_v0.evolve(
        jurisdiction="California",
        now_unix=now + 100,
    )
    # These were set in v0 and not touched in evolve() — must survive
    assert state_v1.wf_id == state_v0.wf_id
    assert state_v1.confidence == state_v0.confidence
    assert state_v1.parties == state_v0.parties
    assert state_v1.case_id == state_v0.case_id


def test_evolve_applies_changes():
    """Fields mentioned in evolve() are updated in the new state."""
    now = 1_700_000_000
    state_v0 = make_initial_state(now)
    state_v1 = state_v0.evolve(
        jurisdiction="California",
        now_unix=now + 100,
    )
    assert state_v1.jurisdiction == "California"
    assert state_v1.updated_at_unix == now + 100


def test_evolve_does_not_mutate_original():
    """The frozen original state is completely unchanged after evolve()."""
    now = 1_700_000_000
    state_v0 = make_initial_state(now)
    state_v1 = state_v0.evolve(
        jurisdiction="California",
        now_unix=now + 100,
    )
    # v0 must be bit-for-bit identical to what it was before evolve() was called
    assert state_v0.version == 0
    assert state_v0.jurisdiction is None
    assert state_v0.updated_at_unix == now



def test_evolve_requires_now_unix():
    """Time must always be injected — evolve() refuses to source it internally."""
    now = 1_700_000_000
    state = make_initial_state(now)

    with pytest.raises(ValueError) as exc_info:
        state.evolve(jurisdiction="California")   # no now_unix
    assert "now_unix" in str(exc_info.value)


def test_evolve_rejects_clock_regression():
    """
    now_unix before updated_at_unix means the clock went backwards.
    Possible replay attack or test using stale timestamps — must be rejected.
    """
    now = 1_700_000_000
    state_v0 = make_initial_state(now)

    with pytest.raises(ValueError) as exc_info:
        state_v0.evolve(
            jurisdiction="California",
            now_unix=now - 500,   # before the state was created — impossible
        )
    assert "Clock regression" in str(exc_info.value)


def test_chain_of_evolve_produces_correct_versions():
    """
    Multiple sequential evolve() calls produce a clean version chain.
    This is the exact pattern the lock manager will use.
    """
    now = 1_700_000_000
    state_v0 = make_initial_state(now)

    state_v1 = state_v0.evolve(
        jurisdiction="California",
        now_unix=now + 100,
    )
    state_v2 = state_v1.evolve(
        filing_deadline_unix=1_800_000_000,
        now_unix=now + 200,
    )
    state_v3 = state_v2.evolve(
        stage=WorkflowStage.UNDER_REVIEW,
        now_unix=now + 300,
    )

    assert state_v0.version == 0
    assert state_v1.version == 1
    assert state_v2.version == 2
    assert state_v3.version == 3

    # Full lineage preserved — each state is a complete snapshot
    assert state_v3.jurisdiction == "California"       # from v1
    assert state_v3.filing_deadline_unix == 1_800_000_000    # from v2
    assert state_v3.stage == WorkflowStage.UNDER_REVIEW  # from v3

def test_rejection_without_decision_by_rejected():
    """
    FIX 1: An anonymous rejection must be refused.
    Previously only rejection_reason was required for rejections.
    Now decision_by and decision_timestamp_unix are also mandatory.
    """
    data = make_valid_state()
    data["human_approved"] = False
    data["rejection_reason"] = "Insufficient evidence."
    data["decision_by"] = None   # anonymous — must fail
    data["decision_timestamp_unix"] = int(time.time()) + 100

    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "decision_by" in str(exc_info.value).lower()


def test_rejection_without_timestamp_rejected():
    """FIX 1: An untimestamped rejection must be refused."""
    data = make_valid_state()
    data["human_approved"] = False
    data["rejection_reason"] = "Insufficient evidence."
    data["decision_by"] = "judge.smith@court.gov"
    data["decision_timestamp_unix"] = None   # missing — must fail

    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "decision_timestamp_unix" in str(exc_info.value).lower()


def test_valid_rejection_requires_full_identity():
    """FIX 1: A fully identified, timestamped rejection must pass."""
    data = make_valid_state()
    now = int(time.time())
    data["human_approved"] = False
    data["rejection_reason"] = "Insufficient evidence."
    data["decision_by"] = "judge.smith@court.gov"
    data["decision_timestamp_unix"] = now + 100

    state = BlackboardState(**data)
    assert state.human_approved is False
    assert state.decision_by == "judge.smith@court.gov"
    assert state.rejection_reason == "Insufficient evidence."


def test_evolve_blocks_case_id_hijack():
    """FIX 2: No agent can overwrite case_id via evolve()."""
    now = 1_700_000_000
    state = make_initial_state(now)

    with pytest.raises(ValueError) as exc_info:
        state.evolve(case_id="HIJACKED-CASE", now_unix=now + 100)
    assert "immutable" in str(exc_info.value).lower()
    assert "case_id" in str(exc_info.value)


def test_evolve_blocks_created_at_hijack():
    """FIX 2: No agent can overwrite created_at_unix via evolve()."""
    now = 1_700_000_000
    state = make_initial_state(now)

    with pytest.raises(ValueError) as exc_info:
        state.evolve(created_at_unix=0, now_unix=now + 100)
    assert "immutable" in str(exc_info.value).lower()
    assert "created_at_unix" in str(exc_info.value)


def test_evolve_blocks_version_hijack():
    """FIX 2: version cannot be set manually via evolve()."""
    now = 1_700_000_000
    state = make_initial_state(now)

    with pytest.raises(ValueError) as exc_info:
        state.evolve(version=99, now_unix=now + 100)
    assert "immutable" in str(exc_info.value).lower()


def test_misspelled_field_in_evolve_raises_error():
    """
    FIX 3: A misspelled field name must raise ValidationError immediately.
    Previously Pydantic would silently drop the unknown field, committing
    a state with missing deadline data and no indication of the error.
    """
    now = 1_700_000_000
    state = make_initial_state(now)

    with pytest.raises(ValidationError) as exc_info:
        # 'filing_deadlines_unix' is a typo — extra 's'
        state.evolve(filing_deadlines_unix=1_800_000_000, now_unix=now + 100)
    assert "filing_deadlines_unix" in str(exc_info.value)


def test_misspelled_field_in_direct_instantiation_raises_error():
    """FIX 3: extra='forbid' applies at construction time too, not only in evolve()."""
    data = make_valid_state()
    data["filing_deadlines_unix"] = 1_800_000_000   # typo — extra 's'

    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "filing_deadlines_unix" in str(exc_info.value) or \
           "extra" in str(exc_info.value).lower()

# ── FIX 1: zero-copy cloning ───────────────────────────────────────────────

def test_evolve_reuses_party_objects_in_memory():
    """
    FIX 1: evolve() must not reconstruct frozen nested objects.
    The PartySchema instances in the new state must be the exact
    same objects in memory as in the original state.
    """
    now      = 1_700_000_000
    state_v0 = make_initial_state(now)
    state_v1 = state_v0.evolve(jurisdiction="California", now_unix=now + 100)

    # Same object identity — not just equal values, same memory address
    for original, evolved in zip(state_v0.parties, state_v1.parties):
        assert original is evolved, (
            "evolve() reconstructed a PartySchema that should have been reused. "
            "model_copy() should carry frozen nested objects by reference."
        )


def test_evolve_with_list_field_raises_clear_error():
    """
    FIX 2: Passing a list field directly to evolve() must be blocked.
    Agents must use append_to() instead to prevent hard overwrites.
    """
    now   = 1_700_000_000
    state = make_initial_state(now)

    with pytest.raises(ValueError) as exc_info:
        state.evolve(risk_flags=["new_flag"], now_unix=now + 100)
    assert "append_to" in str(exc_info.value)
    assert "risk_flags" in str(exc_info.value)


# ── FIX 2: collection mutators ────────────────────────────────────────────

def test_append_to_adds_items_without_overwriting():
    now = 1_700_000_000

    state_v0 = BlackboardState.initial(
        case_id="CASE-001",
        wf_id=WorkflowEnum.WF6_DISPUTE_INITIATION,
        confidence=0.95,
        parties=[
            PartySchema(name="John Smith", party_role=PartyRole.PLAINTIFF),
            PartySchema(name="Acme Corp",  party_role=PartyRole.DEFENDANT),
        ],
        stage=WorkflowStage.INITIATED,
        now_unix=now,
        compliance_rules=["rule_A"],   # passed through **kwargs to constructor
    )

    state_v1 = state_v0.append_to("compliance_rules", ["rule_B", "rule_C"], now_unix=now + 100)

    assert state_v0.compliance_rules == ["rule_A"]               # original unchanged
    assert state_v1.compliance_rules == ["rule_A", "rule_B", "rule_C"]
    assert state_v1.version == 1


def test_append_to_deduplicates():
    """FIX 2: Appending an already-present item must not create duplicates."""
    now      = 1_700_000_000
    state_v0 = make_initial_state(now)

    state_v1 = state_v0.model_copy(update={
        "risk_flags":      ["high_value"],
        "version":          1,
        "updated_at_unix":  now + 50,
    })

    state_v2 = state_v1.append_to(
        "risk_flags",
        ["high_value", "imminent_deadline"],   # "high_value" is a duplicate
        now_unix=now + 100,
    )

    assert state_v2.risk_flags == ["high_value", "imminent_deadline"]
    assert state_v2.risk_flags.count("high_value") == 1


def test_append_to_rejects_non_appendable_field():
    """FIX 2: Scalar fields cannot be used with append_to()."""
    now   = 1_700_000_000
    state = make_initial_state(now)

    with pytest.raises(ValueError) as exc_info:
        state.append_to("jurisdiction", ["California"], now_unix=now + 100)
    assert "appendable" in str(exc_info.value).lower()


def test_two_agents_appending_concurrently_preserves_all_data():
    """
    FIX 2: Simulates two agents both appending to risk_flags from the same
    parent state. With append_to(), both agents produce a candidate state.
    The lock manager commits one, then the other agent retries from the
    committed state. Final result contains all flags from both agents.
    """
    now      = 1_700_000_000
    state_v0 = make_initial_state(now)

    # Both agents read state_v0 (no existing flags) and produce candidates
    candidate_agent1 = state_v0.append_to("risk_flags", ["high_value"],      now_unix=now + 100)
    candidate_agent2 = state_v0.append_to("risk_flags", ["imminent_deadline"], now_unix=now + 200)

    # Lock manager commits agent1 first (v0 → v1)
    committed_v1 = candidate_agent1
    assert committed_v1.version == 1

    # Agent2's candidate is now stale (based on v0, but blackboard is at v1)
    # Agent2 retries: re-reads committed_v1, appends its flag
    retried_v2 = committed_v1.append_to("risk_flags", ["imminent_deadline"], now_unix=now + 300)

    assert retried_v2.version == 2
    assert "high_value" in retried_v2.risk_flags
    assert "imminent_deadline" in retried_v2.risk_flags


# ── FIX 3: epoch magnitude ────────────────────────────────────────────────

def test_millisecond_timestamp_rejected_in_legal_requirements():
    """
    FIX 3: A 13-digit millisecond timestamp must be rejected with
    a clear error showing the correct seconds value.
    """
    millisecond_timestamp = 1_700_000_000_000   # 13 digits — milliseconds

    with pytest.raises(ValidationError) as exc_info:
        LegalRequirementsResult(
            jurisdiction="California",
            filing_deadline_unix=millisecond_timestamp,
            compliance_rules=[],
            risk_flags=[],
            requires_human_approval=True,
        )
    error_text = str(exc_info.value)
    assert "milliseconds" in error_text.lower() or "1700000000" in error_text


def test_millisecond_timestamp_rejected_in_extraction_result():
    """FIX 3: Magnitude guard applies at ExtractionResult level too."""
    with pytest.raises(ValidationError) as exc_info:
        ExtractionResult(
            wf_id=WorkflowEnum.WF6_DISPUTE_INITIATION,
            confidence=0.9,
            parties=[PartySchema(name="John Smith", party_role=PartyRole.PLAINTIFF)],
            filing_deadline_unix=1_700_000_000_000,   # milliseconds
        )
    assert "milliseconds" in str(exc_info.value).lower() or \
           "1700000000" in str(exc_info.value)


def test_millisecond_timestamp_rejected_in_blackboard():
    """FIX 3: Magnitude guard applies at BlackboardState level too."""
    data = make_valid_state()
    data["filing_deadline_unix"] = 1_700_000_000_000   # milliseconds

    with pytest.raises(ValidationError) as exc_info:
        BlackboardState(**data)
    assert "milliseconds" in str(exc_info.value).lower() or \
           "1700000000" in str(exc_info.value)


def test_valid_10_digit_timestamp_accepted():
    """FIX 3: A well-formed 10-digit seconds timestamp must pass."""
    result = LegalRequirementsResult(
        jurisdiction="California",
        filing_deadline_unix=1_800_000_000,   # year 2027 — valid
        compliance_rules=[],
        risk_flags=[],
        requires_human_approval=True,
    )
    assert result.filing_deadline_unix == 1_800_000_000


def test_error_message_contains_corrected_value():
    """
    FIX 3: The rejection error must tell the agent exactly what value
    to retry with — divide by 1000. Agents should not have to guess.
    """
    ms_timestamp = 1_700_000_000_000

    with pytest.raises(ValidationError) as exc_info:
        LegalRequirementsResult(
            jurisdiction="California",
            filing_deadline_unix=ms_timestamp,
            compliance_rules=[],
            risk_flags=[],
            requires_human_approval=True,
        )
    # The correct seconds value (1_700_000_000) must appear in the error
    assert "1700000000" in str(exc_info.value)