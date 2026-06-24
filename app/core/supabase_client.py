"""Supabase client. Service-role key, bypasses RLS. Use for backend workers only."""
from __future__ import annotations
from functools import lru_cache
from supabase import create_client, Client
from .settings import get_settings


@lru_cache
def get_supabase() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_service_role_key)
