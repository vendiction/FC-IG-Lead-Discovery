"""Encrypt/decrypt sensitive blobs (IG session cookies) with Fernet."""
from __future__ import annotations
import json
from typing import Any
from cryptography.fernet import Fernet
from .settings import get_settings


def _fernet() -> Fernet:
    key = get_settings().fernet_key.encode()
    return Fernet(key)


def encrypt_dict(data: Any) -> str:
    """Encrypt any JSON-serializable payload. Name is historical — also handles
    lists (used for IG cookie arrays since 2026-06-24)."""
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_dict(token: str) -> Any:
    return json.loads(_fernet().decrypt(token.encode()).decode())
