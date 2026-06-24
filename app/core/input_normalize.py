"""Pure input-normalization helpers.

Extracted from m8_handoff/discord_bot.py so they're testable without
the discord dependency. Used by the operator-input slash commands
to handle the messiness of how humans paste handles and hashtags:
sometimes with @ / #, sometimes as full URLs, sometimes with extra
whitespace.
"""
from __future__ import annotations


def normalize_handle(raw: str) -> str:
    """Strip @, whitespace, URL fragments. Returns lowercase username.

    Tolerates:
      - bare handle:  'healthcoachclaudia' → 'healthcoachclaudia'
      - @prefixed:    '@healthcoachclaudia' → 'healthcoachclaudia'
      - profile URL:  'https://instagram.com/healthcoachclaudia/' → 'healthcoachclaudia'
      - with query:   '.../healthcoachclaudia/?hl=en' → 'healthcoachclaudia'
      - whitespace:   '  Handle  ' → 'handle'

    Preserves valid IG-handle characters (dots, underscores).
    """
    h = raw.strip().lstrip("@").lower()
    if "instagram.com/" in h:
        h = h.split("instagram.com/", 1)[1]
    h = h.split("/")[0].split("?")[0]
    return h


def normalize_hashtag(raw: str) -> str:
    """Strip leading #, whitespace, lowercase."""
    return raw.strip().lstrip("#").lower()
