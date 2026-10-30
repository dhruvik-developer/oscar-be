"""Indian mobile numbers, shared by serializers (mirrors src/utils/phone.js).

10 digits starting 6-9, optionally written with a +91 / 91 / 0 prefix and
spaces, dashes or brackets. Stored as ``+91 98765 43210``.
"""
import re

from rest_framework import serializers

_SEPARATORS = re.compile(r"[\s\-().]")
_MOBILE = re.compile(r"^[6-9]\d{9}$")


def national_mobile(value):
    """The 10-digit national number, or ``None`` when not a valid mobile."""
    digits = _SEPARATORS.sub("", str(value or ""))
    if digits.startswith("+91"):
        digits = digits[3:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if _MOBILE.match(digits) else None


def clean_mobile(value):
    """Serializer helper: blank becomes ``None``, a valid number is
    normalised, anything else raises a field error."""
    raw = str(value or "").strip()
    if not raw or raw == "+91":
        return None
    national = national_mobile(raw)
    if national is None:
        raise serializers.ValidationError(
            "Enter a valid 10-digit mobile number (e.g. +91 98765 43210)."
        )
    return f"+91 {national[:5]} {national[5:]}"
