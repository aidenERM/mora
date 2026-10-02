import os
import re


def clean(value):
    """Strip credential-shaped text before persistence, model input, or DM output."""
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if not isinstance(value, str):
        return value
    for key, secret in os.environ.items():
        if len(secret) >= 8 and any(part in key.upper() for part in ("API_KEY", "SECRET", "PASSWORD", "BOT_TOKEN", "ACCESS_KEY")):
            value = value.replace(secret, "[credential removed]")
    value = re.sub(r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}", "[credential removed]", value)
    value = re.sub(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}", "[credential removed]", value)
    return re.sub(r"(?i)\b(password|secret|api[_ -]?key|bot[_ -]?token)\s*(?:is|[:=])\s*\S+", r"\1: [credential removed]", value)
