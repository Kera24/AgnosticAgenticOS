"""Secret redaction. Every string that reaches a log file or prompt goes
through redact(). Values of secret-looking environment variables are masked
by value; well-known token shapes are masked by pattern."""
import os
import re

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),            # OpenAI-style keys
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),        # Anthropic keys
    re.compile(r"sk-or-[A-Za-z0-9_\-]{16,}"),         # OpenRouter keys
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),        # GitHub tokens
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),     # Slack tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),                  # AWS access key ids
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"), # bearer headers
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWTs
]

_SECRET_ENV_RE = re.compile(r"(_API_KEY|_TOKEN|_SECRET|_PASSWORD|_CREDENTIALS?)$",
                            re.IGNORECASE)

MASK = "[REDACTED]"


def secret_env_values():
    vals = []
    for name, value in os.environ.items():
        if _SECRET_ENV_RE.search(name) and value and len(value) >= 8:
            vals.append(value)
    return vals


def redact(text, extra_values=None):
    if text is None:
        return text
    if not isinstance(text, str):
        text = str(text)
    for value in (extra_values or []) + secret_env_values():
        if value:
            text = text.replace(value, MASK)
    for pat in _PATTERNS:
        text = pat.sub(MASK, text)
    return text


def looks_like_secret(text):
    """True when content appears to contain a credential — used for alerts."""
    if not isinstance(text, str):
        return False
    return any(p.search(text) for p in _PATTERNS)
