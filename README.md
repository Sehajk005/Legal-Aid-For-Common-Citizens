# Phase 1.1 — Pydantic State Schema

This is the foundation everything else in the system is built on. Before writing a single agent, before touching a vector database, before any LLM call — the data contract had to exist and be provably correct.

The goal of this phase was simple: make invalid state impossible to represent, not just discouraged.

---

## What's in this phase

A single file: `schemas/blackboard.py`

It defines every data structure the multi-agent system will read from and write to. No runtime logic, no network calls, no agents. Just types.

The main models:

- `WorkflowEnum` — all 20 legal workflow identifiers (WF1 through WF20)
- `PartyRole` / `PartySchema` — who is involved in a case and in what capacity
- `WorkflowStage` — where in the lifecycle a case currently sits
- `ExtractionResult` — what the Document Intelligence Agent produces after reading a document
- `LegalRequirementsResult` — what the Legal Knowledge Agent produces after checking jurisdiction and deadlines
- `BlackboardState` — the shared global state that all agents read from and write to

There's also `schemas/validators.py` which handles deadline evaluation with injected time — more on that below.

---

## Design decisions worth knowing about

**Every field has a strict type.** There's no `Optional[str]` used as a lazy escape hatch. If a field can be absent at extraction time vs. after legal analysis, those are modelled as separate types (`ExtractionResult` vs `LegalRequirementsResult`) with the optionality explicit by design.

**`BlackboardState` is frozen and immutable.** Agents cannot mutate the shared state in place. The only way to produce a new state is through `evolve()`, which always increments the version counter and requires an explicitly injected timestamp. This makes concurrent writes safe to reason about — the lock manager in Phase 1.2 relies on this.

**`evolve()` has five guards before it does anything:**
1. Genesis fields (`case_id`, `created_at_unix`, `version`) cannot be changed — ever
2. `now_unix` must be passed explicitly — time is never sourced from inside the schema
3. Clock regression is rejected — `now_unix` cannot be before the last `updated_at_unix`
4. List fields (`risk_flags`, `compliance_rules`, etc.) cannot be passed to `evolve()` — use `append_to()` instead
5. Unknown field names raise immediately — catches typos before they silently corrupt state

**`append_to()` exists for list fields specifically.** If two agents both try to add items to `risk_flags`, they cannot safely do it via `evolve()` — one would overwrite the other. `append_to()` reads the current list, deduplicates, and merges, giving the lock manager a clean retry path.

**Deadline validation is separate from the schema.** `validators.py` contains `validate_deadline()` which evaluates whether a deadline is future, imminent, or past — but it takes `now_unix` as a parameter rather than calling `time.time()` internally. This makes every test deterministic and means the system can process historical documents (auditing a missed 2023 deadline, for example) without the schema rejecting them as invalid.

**Timestamps are validated for magnitude.** LLMs frequently produce millisecond timestamps (13 digits) instead of second timestamps (10 digits). Any value above `10_000_000_000` is rejected with an error message that includes the correct seconds value — `divide by 1000 before passing to this schema`.

**Cross-field relationships are validated at the model level.** Field-level validators check individual fields. `model_validator` checks how fields relate to each other:
- `filing_deadline_unix` must come before `statute_of_limitations_unix`
- `updated_at_unix` cannot be before `created_at_unix`
- `human_approved = True` requires both `decision_by` and `decision_timestamp_unix`
- `human_approved = False` requires `decision_by`, `decision_timestamp_unix`, and `rejection_reason` — anonymous rejections are not valid audit records

**`extra="forbid"` on every model.** A misspelled field name is a `ValueError` immediately, not a silent drop. `filing_deadlines_unix` (note the extra `s`) raises and tells you exactly which key was unrecognised.

---

## Running the tests

```bash
pytest tests/test_phase_1_1_schema.py -v
```

48 tests, all passing. They cover:

- Valid instantiation
- String dates rejected (`"October 12th"` fails on `filing_deadline_unix`)
- Immutability (`frozen=True` — direct field assignment raises)
- Cross-field chronological paradoxes (filing after statute of limitations)
- HITL audit trail completeness (approval and rejection both require identity + timestamp)
- `evolve()` — version chain, immutable field protection, clock regression, typo detection
- `append_to()` — accumulation without overwrite, deduplication, concurrent append simulation
- Epoch magnitude — millisecond timestamps rejected with corrected value in error message
- Deterministic deadline validation with injected time

---

## What this phase does not cover

No agents, no LLMs, no database, no async code. Those come in later phases. This phase only answers: if the system is running and an agent tries to write something to the blackboard, what is and isn't allowed?

The answer is now enforced structurally rather than by convention.

---

## Files

```
schemas/
├── __init__.py
├── blackboard.py      # all models
└── validators.py      # deadline evaluation with injected time

tests/
├── __init__.py
└── test_phase_1_1_schema.py
```
