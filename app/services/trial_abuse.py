"""Trial abuse prevention service."""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.machine_id import MachineId
from app.models.user import User

logger = logging.getLogger(__name__)

TRIAL_DAYS = 30
GRACE_PERIOD_DAYS = 3
MIN_ACCOUNT_AGE_FOR_DELETE_DAYS = 1


async def get_or_create_user_by_github(
    github_login: str,
    supabase_id: str,
    email: str | None,
    display_name: str | None,
    session: AsyncSession,
    machine_id: str | None = None,
    device_id: str | None = None,
) -> User:
    """
    Upsert user by Supabase ID.

    If the user already exists (same supabase_id), update github_login and
    record the machine fingerprint, then return.
    If new, create with trial reset — but carry over trial_days_used from:
      1. A prior user with the same github_login (account-level abuse).
      2. A prior user on the same machine (machine-level abuse).
    The higher of the two carry-over values is used.
    """
    # Look up by supabase_id first
    result = await session.execute(
        select(User).where(User.supabase_id == supabase_id)
    )
    user = result.scalar_one_or_none()

    if user is not None:
        # Update mutable fields
        if github_login:
            user.github_login = github_login
        if email:
            user.email = email
        if display_name:
            user.display_name = display_name
        await session.flush()
        # Upsert machine fingerprint for existing user
        if machine_id:
            await _upsert_machine_id(user.id, machine_id, device_id, session)
        return user

    # --- New user: determine how many trial days to carry over ---
    # T-BE-06: Aggregate across ALL prior accounts matching by any identity vector
    # to prevent trial resets via account deletion + re-signup.

    # 1. Carry-over by github_login (same GitHub account, new Clerk registration)
    github_carried = 0
    if github_login:
        prev_result = await session.execute(
            select(func.max(User.trial_days_used)).where(User.github_login == github_login)
        )
        github_carried = prev_result.scalar_one_or_none() or 0

    # 2. Carry-over by email (same email address, potentially different OAuth provider)
    email_carried = 0
    if email:
        email_result = await session.execute(
            select(func.max(User.trial_days_used)).where(User.email == email, User.email != "")
        )
        email_carried = email_result.scalar_one_or_none() or 0

    # 3. Carry-over by machine_id (same hardware, different GitHub account)
    machine_carried = 0
    if machine_id:
        # Find ALL user IDs linked to this machine fingerprint
        mid_result = await session.execute(
            select(MachineId.user_id).where(MachineId.machine_id == machine_id)
        )
        machine_user_ids = [row[0] for row in mid_result.all()]
        if machine_user_ids:
            max_result = await session.execute(
                select(func.max(User.trial_days_used)).where(User.id.in_(machine_user_ids))
            )
            machine_carried = max_result.scalar_one_or_none() or 0

    # Take the worst-case (highest usage) across all identity vectors
    carried_trial_days_used = max(github_carried, email_carried, machine_carried)

    user = User(
        id=str(uuid.uuid4()),
        supabase_id=supabase_id,
        github_login=github_login or "",
        email=email or "",
        display_name=display_name or github_login or "",
        plan="free",
        trial_days_used=carried_trial_days_used,
        created_at=datetime.now(tz=timezone.utc),
        account_deleted=False,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)

    # Record machine fingerprint for the new user
    if machine_id:
        await _upsert_machine_id(user.id, machine_id, device_id, session)

    return user


async def _upsert_machine_id(
    user_id: str,
    machine_id: str,
    device_id: str | None,
    session: AsyncSession,
) -> None:
    """
    Upsert a MachineId record linking this user to their hardware fingerprint.
    Uses INSERT ... ON CONFLICT DO NOTHING via SQLAlchemy.
    """
    # Check if mapping already exists
    existing = await session.execute(
        select(MachineId).where(
            MachineId.user_id == user_id,
            MachineId.machine_id == machine_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return  # Already recorded

    mid = MachineId(
        id=str(uuid.uuid4()),
        user_id=user_id,
        machine_id=machine_id,
        dev_device_id=device_id,
        first_seen_at=datetime.now(tz=timezone.utc),
        last_seen_at=datetime.now(tz=timezone.utc),
    )
    session.add(mid)
    await session.flush()


def remaining_trial_days(user: User) -> int:
    """
    Return how many trial days the user has left.

    Returns 0 if trial is exhausted or user is pro/enterprise.
    """
    if user.plan in ("pro", "enterprise"):
        return 0  # Pro users don't have a trial countdown
    remaining = TRIAL_DAYS - user.trial_days_used
    return max(0, remaining)


def is_trial_expired(user: User) -> bool:
    """Return True if the free trial has expired with no active Pro subscription."""
    if user.plan in ("pro", "enterprise"):
        return False
    return remaining_trial_days(user) <= 0


def is_trial_expiring_soon(user: User, threshold_days: int = 5) -> bool:
    """Return True if the trial has fewer than threshold_days remaining."""
    if user.plan in ("pro", "enterprise"):
        return False
    return 0 < remaining_trial_days(user) <= threshold_days


def can_delete_account(user: User) -> bool:
    """
    Guard: prevent account deletion when trial has been fully consumed.

    The rationale is to prevent trial farming (create account → use all days →
    delete → repeat with same GitHub OAuth).  Users who carry over trial days
    on a fresh clerk_id would already have trial_days_used = 30, so they cannot
    delete either.

    Pro users can always delete their account.
    """
    if user.plan in ("pro", "enterprise"):
        return True
    # Free users can delete only while they still have some trial left
    # (i.e., they haven't used all 30 days successfully)
    return remaining_trial_days(user) > 0


def increment_trial_days(user: User, days: int = 1) -> None:
    """Increment trial_days_used (called nightly by the expiry checker job)."""
    user.trial_days_used = min(user.trial_days_used + days, TRIAL_DAYS)


# ---------------------------------------------------------------------------
# Fake Pakalon / Trial Abuse Detection
# ---------------------------------------------------------------------------

# Thresholds
_SUSPICIOUS_MACHINE_WINDOW_DAYS = 7
_SUSPICIOUS_MACHINE_THRESHOLD = 5       # >5 unique machines in 7 days → flag
_SUSPICIOUS_ACCOUNT_RESET_THRESHOLD = 3  # >3 accounts per machine → flag

class AbuseSignal:
    """Encapsulates a detected abuse signal for logging / alerting."""

    __slots__ = ("kind", "user_id", "github_login", "detail")

    def __init__(self, kind: str, user_id: str, github_login: str, detail: str):
        self.kind = kind
        self.user_id = user_id
        self.github_login = github_login
        self.detail = detail

    def __repr__(self) -> str:
        return (
            f"<AbuseSignal kind={self.kind!r} github={self.github_login!r} "
            f"user={self.user_id!r} detail={self.detail!r}>"
        )


async def count_recent_machine_ids_for_user(
    user_id: str,
    lookback_days: int,
    session: AsyncSession,
) -> int:
    """Return number of distinct machine_ids registered for *user_id* in the last *lookback_days*."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    result = await session.execute(
        select(func.count(MachineId.machine_id))
        .where(MachineId.user_id == user_id)
        .where(MachineId.first_seen_at >= cutoff)
    )
    return result.scalar_one() or 0


async def count_accounts_per_machine(
    machine_id_str: str,
    session: AsyncSession,
) -> int:
    """Return how many distinct user_ids share the same machine_id."""
    result = await session.execute(
        select(func.count(func.distinct(MachineId.user_id)))
        .where(MachineId.machine_id == machine_id_str)
    )
    return result.scalar_one() or 0


async def detect_trial_abuse_signals(
    user: User,
    machine_id: str | None,
    session: AsyncSession,
) -> list[AbuseSignal]:
    """
    Run a set of heuristics to detect fake-Pakalon / trial-reset abuse.

    Returns a (possibly empty) list of :class:`AbuseSignal` objects.
    The caller should log / store / alert on these; no automatic action is taken.

    Heuristics
    ----------
    1. **Rapid new machine registration** — the user associated >threshold distinct
       machines in the last 7 days, suggesting VM fingerprint cycling or Docker-
       container churn to reset the trial.
    2. **Machine shared across many accounts** — the presented machine_id is
       already linked to ≥threshold other users, suggesting shared-instance trial
       farming.
    3. **Near-zero trial remaining + high machine count** — user is almost out of
       trial days AND appears to be generating new machine fingerprints.
    """
    signals: list[AbuseSignal] = []

    # 1. Rapid machine registration
    recent_count = await count_recent_machine_ids_for_user(
        user.id,
        lookback_days=_SUSPICIOUS_MACHINE_WINDOW_DAYS,
        session=session,
    )
    if recent_count > _SUSPICIOUS_MACHINE_THRESHOLD:
        sig = AbuseSignal(
            kind="rapid_machine_registration",
            user_id=user.id,
            github_login=user.github_login or "",
            detail=(
                f"{recent_count} distinct machine IDs registered in the last "
                f"{_SUSPICIOUS_MACHINE_WINDOW_DAYS} days "
                f"(threshold={_SUSPICIOUS_MACHINE_THRESHOLD})"
            ),
        )
        signals.append(sig)
        logger.warning(
            "ABUSE_SIGNAL rapid_machine_registration github=%s user=%s detail=%s",
            user.github_login,
            user.id,
            sig.detail,
        )

    # 2. Machine shared across many accounts
    if machine_id:
        acct_count = await count_accounts_per_machine(machine_id, session)
        if acct_count >= _SUSPICIOUS_ACCOUNT_RESET_THRESHOLD:
            sig = AbuseSignal(
                kind="shared_machine_id",
                user_id=user.id,
                github_login=user.github_login or "",
                detail=(
                    f"machine_id '{machine_id[:16]}…' shared across "
                    f"{acct_count} accounts "
                    f"(threshold={_SUSPICIOUS_ACCOUNT_RESET_THRESHOLD})"
                ),
            )
            signals.append(sig)
            logger.warning(
                "ABUSE_SIGNAL shared_machine_id github=%s user=%s detail=%s",
                user.github_login,
                user.id,
                sig.detail,
            )

    # 3. Low trial + high machine count (combination signal)
    if remaining_trial_days(user) <= 2 and recent_count > 1:
        sig = AbuseSignal(
            kind="low_trial_with_new_machines",
            user_id=user.id,
            github_login=user.github_login or "",
            detail=(
                f"only {remaining_trial_days(user)} trial days remaining, "
                f"yet {recent_count} new machines in {_SUSPICIOUS_MACHINE_WINDOW_DAYS}d window"
            ),
        )
        signals.append(sig)
        logger.warning(
            "ABUSE_SIGNAL low_trial_with_new_machines github=%s user=%s detail=%s",
            user.github_login,
            user.id,
            sig.detail,
        )

    return signals
