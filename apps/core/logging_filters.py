import logging
import re

SENSITIVE_KEY_RE = re.compile(
    r"(password|token|otp|secret|authorization|bearer|cookie|auth_header)",
    re.IGNORECASE,
)

AUTH_BEARER_RE = re.compile(r"((?:Bearer|Token)\s+)[A-Za-z0-9\-_.]+", re.IGNORECASE)
KEY_VALUE_RE = re.compile(
    r"((?:password|token|otp|secret|authorization|refresh|access)['\"]?\s*[:=]\s*['\"])[^'\"]+(['\"])",
    re.IGNORECASE,
)
URL_PARAM_RE = re.compile(
    r"([?&](?:token|otp|password|secret|code)=)[^&\s]+",
    re.IGNORECASE,
)


def _mask_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = AUTH_BEARER_RE.sub(r"\1[REDACTED]", text)
    text = KEY_VALUE_RE.sub(r"\1[REDACTED]\2", text)
    text = URL_PARAM_RE.sub(r"\1[REDACTED]", text)
    return text


def _mask_value(val):
    if isinstance(val, str):
        return _mask_text(val)
    if isinstance(val, dict):
        return {
            k: ("[REDACTED]" if SENSITIVE_KEY_RE.search(str(k)) else _mask_value(v))
            for k, v in val.items()
        }
    if isinstance(val, (list, tuple)):
        masked = [_mask_value(x) for x in val]
        return tuple(masked) if isinstance(val, tuple) else masked
    return val


class SensitiveDataFilter(logging.Filter):
    """Filters sensitive tokens, passwords, OTPs, and headers from log output."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _mask_text(record.msg)
        if record.args:
            record.args = _mask_value(record.args)
        return True
