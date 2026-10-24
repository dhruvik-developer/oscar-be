"""
snake_case columns <-> camelCase JSON (db.md §1, bullet 2).

db.md stores ``snake_case`` columns; api.md §0 says the JSON field names are
"not negotiable ... the UI reads them directly". This module is the single
translation between the two, so no serializer has to repeat ``source=``.

Only *declared serializer field names* are translated. The contents of a
``jsonb`` column are never touched: ``items.custom_field_values`` is keyed by
whatever the tenant named their custom fields, and ``crm_forms.schema`` is a
builder-authored tree. Renaming keys inside those would corrupt user data.
"""
import re

_UNDERSCORE_RE = re.compile(r"_([a-z0-9])")
_CAMEL_RE = re.compile(r"(?<!^)(?<![A-Z_])([A-Z])")

#: Field names the frontend spells in a way ``camelize`` would not produce.
#: Each is a name read straight out of the frontend source, so it wins over the
#: mechanical rule.
OVERRIDES = {
    "gstin": "gstin",
    "gst_treatment": "gstTreatment",
    "hsn_code": "hsnCode",
    "sku": "sku",
    "uom": "uom",
    "cgst": "cgst",
    "sgst": "sgst",
    "igst": "igst",
    "cess": "cess",
    "pan": "pan",
    "cin": "cin",
    "ifsc": "ifsc",
    "ifsc_code": "ifscCode",
    "uan": "uan",
    "url": "url",
    "ip": "ip",
    "qc_status": "qcStatus",
    "rma_number": "rmaNumber",
    "po_number": "poNumber",
    "grn_number": "grnNumber",
    "pms_project_id": "pmsProjectId",
    "crm_order_id": "crmOrderId",
    "crm_customer_id": "crmCustomerId",
    "kpi_scores": "kpiScores",
    "hr_comments": "hrComments",
    "wfh": "wfh",
}

_REVERSE_OVERRIDES = {value: key for key, value in OVERRIDES.items() if value != key}


def camelize(name):
    """``line_items`` -> ``lineItems``; leading underscores preserved."""
    if name in OVERRIDES:
        return OVERRIDES[name]
    if not name or "_" not in name:
        return name
    return _UNDERSCORE_RE.sub(lambda match: match.group(1).upper(), name)


def underscoreize(name):
    """``lineItems`` -> ``line_items``."""
    if name in _REVERSE_OVERRIDES:
        return _REVERSE_OVERRIDES[name]
    if not name:
        return name
    return _CAMEL_RE.sub(lambda match: "_" + match.group(1).lower(), name)


def camelize_keys(data):
    """Camelize the keys of a plain dict/list tree.

    For hand-built payloads (dashboards, reports, print payloads) that never
    pass through a serializer. Not used on stored jsonb.
    """
    if isinstance(data, dict):
        return {camelize(str(key)): camelize_keys(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [camelize_keys(item) for item in data]
    return data
