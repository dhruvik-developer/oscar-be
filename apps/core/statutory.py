"""Statutory & tax validation helpers (GSTIN, PAN, Phone)."""
import re
from rest_framework import serializers

GSTIN_REGEX = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
PAN_REGEX = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$")
PHONE_DIGITS_REGEX = re.compile(r"^\+?[0-9]{10,15}$")


def validate_gstin(value):
    if not value:
        return value
    cleaned = str(value).strip().upper()
    if not cleaned:
        return ""
    if not GSTIN_REGEX.match(cleaned):
        raise serializers.ValidationError("Enter a valid 15-character GSTIN (e.g. 24ABCDE1234F1Z5).")
    return cleaned


def validate_pan(value):
    if not value:
        return value
    cleaned = str(value).strip().upper()
    if not cleaned:
        return ""
    if not PAN_REGEX.match(cleaned):
        raise serializers.ValidationError("Enter a valid 10-character PAN (e.g. ABCDE1234F).")
    return cleaned


def validate_phone(value):
    if not value:
        return value
    cleaned = re.sub(r"[\s\-().]", "", str(value).strip())
    if not cleaned:
        return ""
    if not PHONE_DIGITS_REGEX.match(cleaned):
        raise serializers.ValidationError("Enter a valid contact number (10 to 15 digits).")
    return str(value).strip()
