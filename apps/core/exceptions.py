"""
The error contract (api.md §1.5) and the machine-readable code vocabulary
(api.md Appendix C).

The frontend's ``apiClient`` reads ``message``, then ``detail``, then ``error``,
and throws ``ApiError { message, status, endpoint, payload }``. Every error this
backend emits is shaped so that read succeeds, and carries a ``code`` so the UI
can branch without string-matching a message.
"""
import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.http import Http404
from rest_framework import status as http_status
from rest_framework.exceptions import APIException
from rest_framework.response import Response

# NOTE: ``rest_framework.views`` is imported lazily inside
# :func:`api_exception_handler`, not here. Importing it at module scope binds
# ``api_settings.DEFAULT_AUTHENTICATION_CLASSES`` while this module is still
# initialising, which pulls in apps.accounts.authentication -- which imports
# this module. That is a genuine cycle, and deferring the import is the fix.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Appendix C -- machine-readable code vocabulary
# ---------------------------------------------------------------------------
class Codes:
    # Document lifecycle
    HAS_DEPENDENTS = "HAS_DEPENDENTS"
    ALREADY_CANCELLED = "ALREADY_CANCELLED"
    ALREADY_FINALIZED = "ALREADY_FINALIZED"
    NOT_FINALIZED = "NOT_FINALIZED"
    DRAFT_ONLY = "DRAFT_ONLY"
    NUMBER_ALLOCATION_FAILED = "NUMBER_ALLOCATION_FAILED"

    # Stock and QC
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    SERIAL_MISMATCH = "SERIAL_MISMATCH"
    SERIAL_ALREADY_SOLD = "SERIAL_ALREADY_SOLD"
    SERIAL_ALREADY_RETURNED = "SERIAL_ALREADY_RETURNED"
    QC_BLOCKED = "QC_BLOCKED"
    WEIGHT_VARIANCE_EXCEEDED = "WEIGHT_VARIANCE_EXCEEDED"
    OVER_RECEIPT = "OVER_RECEIPT"
    OVER_DISPATCH = "OVER_DISPATCH"
    OVER_INVOICE = "OVER_INVOICE"
    OVER_RETURN = "OVER_RETURN"

    # Money
    CREDIT_LIMIT_EXCEEDED = "CREDIT_LIMIT_EXCEEDED"
    PAYMENT_EXCEEDS_BALANCE = "PAYMENT_EXCEEDS_BALANCE"
    PAYMENT_ON_DRAFT = "PAYMENT_ON_DRAFT"
    PAYMENT_ON_CANCELLED = "PAYMENT_ON_CANCELLED"
    ALREADY_SETTLED = "ALREADY_SETTLED"
    UNBALANCED_JOURNAL_ENTRY = "UNBALANCED_JOURNAL_ENTRY"

    # PMS gates
    NOT_STARTED = "NOT_STARTED"
    INCOMPLETE = "INCOMPLETE"
    BLOCKED_TASKS = "BLOCKED_TASKS"
    OPEN_DELAY = "OPEN_DELAY"
    NEEDS_DOCUMENT = "NEEDS_DOCUMENT"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    POLICY_DESIGN_APPROVAL = "POLICY_DESIGN_APPROVAL"
    POLICY_QA_CERTIFICATE = "POLICY_QA_CERTIFICATE"
    NO_STAGES = "NO_STAGES"
    STAGE_OPEN = "STAGE_OPEN"
    NO_PROJECT = "NO_PROJECT"
    NO_STAGE = "NO_STAGE"
    BAD_TARGET = "BAD_TARGET"
    ALREADY_DONE = "ALREADY_DONE"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    NO_SIGNOFF = "NO_SIGNOFF"
    IN_USE = "IN_USE"
    LAST_ONE = "LAST_ONE"

    # Sharing
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    TOKEN_INVALID = "TOKEN_INVALID"
    ALREADY_DECIDED = "ALREADY_DECIDED"

    # Generic
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"


class EvenmoreAPIError(APIException):
    """Base for every deliberately-raised error. Carries the api.md §1.5 body."""

    status_code = http_status.HTTP_400_BAD_REQUEST
    default_code = Codes.VALIDATION_FAILED
    default_message = "Request could not be completed."

    def __init__(self, message=None, *, code=None, detail=None, field_errors=None, payload=None):
        self.message = message or self.default_message
        self.code = code or self.default_code
        self.detail_text = detail
        self.field_errors = field_errors or None
        self.payload = payload
        super().__init__(detail=self.message, code=self.code)

    def body(self):
        body = {"message": self.message, "code": self.code}
        if self.detail_text:
            body["detail"] = self.detail_text
        if self.field_errors:
            body["field_errors"] = self.field_errors
        if self.payload is not None:
            body["payload"] = self.payload
        return body


class ValidationFailed(EvenmoreAPIError):
    """400 -- field-level validation failure. Always populate `field_errors`."""

    status_code = http_status.HTTP_400_BAD_REQUEST
    default_code = Codes.VALIDATION_FAILED
    default_message = "Some fields need attention."


class NotAuthenticated(EvenmoreAPIError):
    """401 -- missing or expired token. Never 403, the client clears on 401."""

    status_code = http_status.HTTP_401_UNAUTHORIZED
    default_code = "NOT_AUTHENTICATED"
    default_message = "Authentication credentials were not provided or have expired."


class PermissionDenied(EvenmoreAPIError):
    """403 -- authenticated but lacks the RBAC permission.

    api.md §1.5: put the permission id in `code`.
    """

    status_code = http_status.HTTP_403_FORBIDDEN
    default_code = Codes.PERMISSION_DENIED
    default_message = "You do not have permission to perform this action."


class NotFound(EvenmoreAPIError):
    """404 -- not found, or not visible to the caller's tenant."""

    status_code = http_status.HTTP_404_NOT_FOUND
    default_code = Codes.NOT_FOUND
    default_message = "That record no longer exists."


class Conflict(EvenmoreAPIError):
    """409 -- state-machine violation or optimistic-lock conflict."""

    status_code = http_status.HTTP_409_CONFLICT
    default_code = Codes.VERSION_CONFLICT
    default_message = "This record has changed since you opened it."


class BusinessRuleViolation(EvenmoreAPIError):
    """422 -- a business rule that is not field-level.

    Insufficient stock, credit limit, stage gates. The message is shown to the
    user verbatim (api-integration.md §5.5), so write it for a human.
    """

    status_code = http_status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "BUSINESS_RULE_VIOLATION"
    default_message = "That action is not allowed in the current state."


class RateLimited(EvenmoreAPIError):
    status_code = http_status.HTTP_429_TOO_MANY_REQUESTS
    default_code = Codes.RATE_LIMITED
    default_message = "Too many requests. Please wait a moment."


def _flatten_drf_detail(detail):
    """Turn a DRF error detail into (message, field_errors)."""
    if isinstance(detail, dict):
        field_errors = {}
        for key, value in detail.items():
            if isinstance(value, (list, tuple)):
                field_errors[key] = [str(item) for item in value]
            elif isinstance(value, dict):
                # Nested serializer -- flatten to dotted keys so the frontend can
                # bind `lineItems[0].qty` style paths into the form.
                for inner_key, inner_value in _flatten_drf_detail(value)[1].items():
                    field_errors[f"{key}.{inner_key}"] = inner_value
            else:
                field_errors[key] = [str(value)]
        non_field = field_errors.pop("non_field_errors", None)
        message = non_field[0] if non_field else "Some fields need attention."
        return message, field_errors
    if isinstance(detail, (list, tuple)):
        return (str(detail[0]) if detail else "Request could not be completed."), None
    return str(detail), None


def api_exception_handler(exc, context):
    """The single place every error becomes an api.md §1.5 body."""
    from rest_framework.views import exception_handler as drf_exception_handler

    if isinstance(exc, EvenmoreAPIError):
        return Response(exc.body(), status=exc.status_code)

    if isinstance(exc, Http404):
        return Response(NotFound().body(), status=http_status.HTTP_404_NOT_FOUND)

    if isinstance(exc, DjangoPermissionDenied):
        return Response(PermissionDenied().body(), status=http_status.HTTP_403_FORBIDDEN)

    if isinstance(exc, DjangoValidationError):
        field_errors = getattr(exc, "message_dict", None)
        return Response(
            ValidationFailed(
                message=(exc.messages[0] if exc.messages else None),
                field_errors=field_errors,
            ).body(),
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, IntegrityError):
        # A database constraint caught what the handler should have. db.md calls
        # these out explicitly (over-dispatch checks, unique document numbers):
        # the constraint is the guarantee, this is the friendly surface.
        logger.warning("IntegrityError surfaced to the API: %s", exc)
        return Response(
            Conflict(
                message="That change conflicts with an existing record.",
                code=Codes.VERSION_CONFLICT,
                detail=str(exc).split("\n")[0],
            ).body(),
            status=http_status.HTTP_409_CONFLICT,
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled exception in %s", context.get("view"))
        return Response(
            {
                "message": "The server is unavailable. Please try again shortly.",
                "code": "INTERNAL_ERROR",
            },
            status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    message, field_errors = _flatten_drf_detail(response.data)
    code = getattr(exc, "default_code", None) or Codes.VALIDATION_FAILED

    if response.status_code == http_status.HTTP_401_UNAUTHORIZED:
        code = "NOT_AUTHENTICATED"
    elif response.status_code == http_status.HTTP_403_FORBIDDEN:
        code = Codes.PERMISSION_DENIED
    elif response.status_code == http_status.HTTP_404_NOT_FOUND:
        code = Codes.NOT_FOUND
    elif response.status_code == http_status.HTTP_429_TOO_MANY_REQUESTS:
        code = Codes.RATE_LIMITED
        message = "Too many requests. Please wait a moment."

    body = {"message": message, "code": code}
    if field_errors:
        body["field_errors"] = field_errors
    response.data = body
    return response
