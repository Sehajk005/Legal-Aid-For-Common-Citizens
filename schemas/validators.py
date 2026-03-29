from dataclasses import dataclass
from enum import Enum
from typing import Optional

class DeadlineStatus(str, Enum):
    FUTURE        = "FUTURE"          # deadline has not passed
    PAST          = "PAST"            # deadline passed — document is historical
    IMMINENT      = "IMMINENT"        # within 72 hours
    EXPIRED_GRACE = "EXPIRED_GRACE"   # past but within grace period

@dataclass(frozen=True)
class DeadlineValidationResult:
    status: DeadlineStatus
    days_remaining: Optional[int]
    days_overdue: Optional[int]
    requires_escalation: bool

def validate_deadline(
    filing_deadline_unix: int,
    now_unix: int,
    grace_period_days: int = 30,
    imminent_threshold_hours: int = 72,
) -> DeadlineValidationResult:
    """
    Evaluate a deadline against an injected reference time.
    
    Args:
        filing_deadline_unix:     The deadline as a Unix timestamp (int).
        now_unix:                 The reference time, injected by the caller.
        grace_period_days:        How many days past deadline still counts
                                  as recoverable (jurisdiction-specific).
        imminent_threshold_hours: Hours before deadline that trigger IMMINENT.
    """
    seconds_remaining = filing_deadline_unix - now_unix
    hours_remaining = seconds_remaining / 3600
    days_remaining = seconds_remaining / 86400
    
    if seconds_remaining > 0:
        if hours_remaining <= imminent_threshold_hours:
            return DeadlineValidationResult(
                status=DeadlineStatus.IMMINENT,
                days_remaining=int(days_remaining),
                days_overdue=None,
                requires_escalation=True,
            )
        return DeadlineValidationResult(
            status=DeadlineStatus.FUTURE,
            days_remaining=int(days_remaining),
            days_overdue=None,
            requires_escalation=False,
        )

    days_overdue = abs(days_remaining)
    within_grace = days_overdue <= grace_period_days

    return DeadlineValidationResult(
        status=DeadlineStatus.EXPIRED_GRACE if within_grace else DeadlineStatus.PAST,
        days_remaining=None,
        days_overdue=int(days_overdue),
        requires_escalation=True,
    )

def is_historical_document(
    filing_deadline_unix: int,
    now_unix:int,
) -> bool:
    """
    Return True if the document is historical (deadline has passed).
    """
    return filing_deadline_unix < now_unix