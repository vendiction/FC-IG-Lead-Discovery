"""
Test mode gate. Used by M1, M2, M3 to hard-stop when TEST_MODE is on and the
accounts table has reached TEST_MODE_PROFILE_LIMIT entries.

Centralized here so we have one place to flip when going to production.
"""
from __future__ import annotations
from app.core.settings import get_settings
from app.core.supabase_client import get_supabase
from app.core.logging import get_logger

log = get_logger(__name__)


def at_test_limit() -> bool:
    """
    Returns True if test mode is enabled AND we've hit the profile limit.
    Discovery workers should stop enqueueing new handles when this is True.
    """
    s = get_settings()
    if not s.test_mode:
        return False

    sb = get_supabase()
    r = sb.table("accounts").select("id", count="exact").limit(1).execute()
    count = r.count or 0
    if count >= s.test_mode_profile_limit:
        log.info("test_mode.limit_reached",
                 current=count, limit=s.test_mode_profile_limit)
        return True
    return False


def accounts_remaining() -> int:
    """How many more accounts we'll accept before stopping in test mode."""
    s = get_settings()
    if not s.test_mode:
        return 999_999

    sb = get_supabase()
    r = sb.table("accounts").select("id", count="exact").limit(1).execute()
    count = r.count or 0
    return max(0, s.test_mode_profile_limit - count)
