"""
lock_manager.py
---------------
Cross-bot profile locking via Supabase row-level updates.

Before launching a Dolphin Anty browser session, the bot atomically claims
the account row by setting locked_by / locked_at.  If another bot already
holds the lock (and it hasn't expired), the acquire fails and the caller
should skip the account.

The lock is always released in a finally block.  A 30-minute TTL acts as
a safety net for crashed processes.
"""

from datetime import datetime, timedelta, timezone
import logging

logger = logging.getLogger(__name__)

LOCK_TTL_MINUTES = 30

# Injected by app.py at startup (same pattern as utils.py)
_supabase = None


def init_lock_manager(supabase_client):
    global _supabase
    _supabase = supabase_client


def acquire_lock(username: str, platform: str, bot_id: str) -> bool:
    """
    Atomically acquire a lock on a social account.

    The UPDATE only touches the row if locked_by IS NULL (available) or
    locked_at is older than LOCK_TTL_MINUTES (stale / crashed bot).

    Returns True if the lock was acquired, False otherwise.
    """
    threshold = (datetime.now(timezone.utc) - timedelta(minutes=LOCK_TTL_MINUTES)).isoformat()
    try:
        result = (
            _supabase.table("social_accounts")
            .update({
                "locked_by": bot_id,
                "locked_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("username", username)
            .eq("platform", platform)
            .or_(f"locked_by.is.null,locked_at.lt.{threshold}")
            .execute()
        )
        acquired = len(result.data) > 0
        if acquired:
            print(f"[LOCK] Acquired lock for @{username} ({bot_id})")
        else:
            print(f"[LOCK] Cannot lock @{username} — held by another bot")
        return acquired
    except Exception as e:
        logger.error(f"[LOCK] Failed to acquire lock for @{username}: {e}")
        return False


def release_lock(username: str, platform: str, bot_id: str) -> None:
    """
    Release the lock on a social account.

    Only clears the lock if locked_by matches bot_id, preventing one bot
    from accidentally releasing another bot's lock.  Failures are logged
    but never re-raised — the TTL will clean up stale locks.
    """
    try:
        _supabase.table("social_accounts") \
            .update({"locked_by": None, "locked_at": None}) \
            .eq("username", username) \
            .eq("platform", platform) \
            .eq("locked_by", bot_id) \
            .execute()
        print(f"[LOCK] Released lock for @{username}")
    except Exception as e:
        logger.error(f"[LOCK] Failed to release lock for @{username}: {e}")


def check_locked_accounts(usernames: list[str], platform: str) -> dict:
    """
    Batch-check which accounts are currently locked (for preflight display).

    Returns a dict mapping username -> locked_by for accounts that have a
    non-expired lock.  Unlocked accounts are omitted from the result.
    """
    try:
        result = (
            _supabase.table("social_accounts")
            .select("username, locked_by, locked_at")
            .in_("username", usernames)
            .eq("platform", platform)
            .execute()
        )
        threshold = datetime.now(timezone.utc) - timedelta(minutes=LOCK_TTL_MINUTES)
        locked = {}
        for row in result.data:
            lb = row.get("locked_by")
            la = row.get("locked_at")
            if lb and la:
                lock_time = datetime.fromisoformat(la.replace("Z", "+00:00"))
                if lock_time > threshold:
                    locked[row["username"]] = lb
        return locked
    except Exception as e:
        logger.error(f"[LOCK] Failed to check locked accounts: {e}")
        return {}
