from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'


class BillingRecordStatus(StrEnum):
    ACTIVE = 'active'
    REVERSED = 'reversed'


class BillingPeriodStatus(StrEnum):
    OPEN = 'open'
    CLOSED = 'closed'


class SettlementKind(StrEnum):
    ORIGINAL = 'original'
    ADJUSTMENT = 'adjustment'
