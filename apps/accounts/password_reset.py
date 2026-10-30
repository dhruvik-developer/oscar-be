"""Password-reset tokens and their delivery.

One email carries a 6-digit code (valid for OTP_TTL) and a link. Either way
the code is required: the sign-in page exchanges it for a short-lived token,
and the link's page sends it along with the link's token, so the link alone
cannot change a password. The database keeps only hashes of both. Nothing here writes
either to the log, except in DEBUG when there is no mail server to deliver
them, so local development still has a way in.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import PasswordResetToken

log = logging.getLogger(__name__)

OTP_TTL = timedelta(minutes=10)
#: Lifetime of the token handed out for a correct code: long enough to type a
#: new password, short enough that a leaked response is worth little.
OTP_EXCHANGE_TTL = timedelta(minutes=15)


def hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def hash_otp(record_id, code):
    # Keyed and bound to the row, so a copied hash is useless elsewhere and a
    # leaked table cannot be brute-forced without SECRET_KEY.
    message = f"{record_id}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def email_configured():
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if backend.endswith(("locmem.EmailBackend", "console.EmailBackend")):
        return True
    return bool(getattr(settings, "EMAIL_HOST", ""))


def reset_url(raw_token):
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/reset-password?{urlencode({'token': raw_token})}"


def _minutes(delta):
    return int(delta.total_seconds() // 60)


def new_code():
    return f"{secrets.randbelow(10**6):06d}"


def workspace_name(user):
    return getattr(user.client, "name", "") or "Evenmore ERP"


def send_code_email(user, *, subject, body, debug_hint):
    """Deliver a code email to `user`. Returns True when sent.

    Without a mail server, DEBUG logs `debug_hint` so development can go on;
    otherwise the failure is logged without the secret.
    """
    if not email_configured():
        if settings.DEBUG:
            log.warning("Email is not configured; %s for %s", debug_hint, user.email)
        else:
            log.error("A code email for %s was not sent: email is not configured.", user.email)
        return False
    sender = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER or "noreply@evenmore.erp"
    try:
        EmailMultiAlternatives(subject=subject, body=body, from_email=sender, to=[user.email]).send()
    except Exception:
        log.exception("Code email to %s failed.", user.email)
        return False
    return True


def issue_reset(user, *, ttl=timedelta(hours=2)):
    """Email `user` a reset link and code. Returns True when sent.

    Any earlier unused link or code for the user stops working: only the
    newest email is valid.
    """
    now = timezone.now()
    PasswordResetToken.objects.filter(user=user, used_at__isnull=True).update(used_at=now)

    raw = secrets.token_urlsafe(48)
    code = new_code()
    record = PasswordResetToken.objects.create(
        user=user,
        token_hash=hash_token(raw),
        expires_at=now + ttl,
        otp_expires_at=now + min(OTP_TTL, ttl),
    )
    record.otp_hash = hash_otp(record.id, code)
    record.save(update_fields=["otp_hash"])
    link = reset_url(raw)

    workspace = workspace_name(user)
    text = (
        f"Hello {user.name or user.email},\n\n"
        f"We received a request to reset your password for {workspace}.\n\n"
        f"Your verification code is: {code}\n"
        f"It is valid for {_minutes(min(OTP_TTL, ttl))} minutes.\n\n"
        "Enter the code on the sign-in page (Forgot password), or open this link\n"
        "and enter the same code there:\n"
        f"{link}\n\n"
        "If you did not ask for this, ignore this email; your password stays the same.\n"
        "Never share this code with anyone."
    )
    return send_code_email(
        user,
        subject=f"{code} is your {workspace} password reset verification code",
        body=text,
        debug_hint=f"reset code {code}, link {link}",
    )


def otp_matches(record, code):
    """True when `code` is this row's live code. Call under select_for_update."""
    code = (code or "").strip()
    return (
        record.otp_usable
        and len(code) == 6
        and hmac.compare_digest(record.otp_hash, hash_otp(record.id, code))
    )


def exchange_otp(email, code):
    """Trade a correct code for a fresh reset token (raw), or None.

    Every live code for the address is a candidate (one per workspace). A
    wrong guess counts against all of them, so spreading guesses across
    workspaces gains nothing.
    """
    code = code.strip()
    with transaction.atomic():
        candidates = list(
            PasswordResetToken.objects.select_for_update()
            .filter(
                user__email=email.lower(),
                user__deleted_at__isnull=True,
                user__status="Active",
                used_at__isnull=True,
                otp_hash__isnull=False,
            )
            .select_related("user")
        )
        live = [record for record in candidates if record.otp_usable]
        match = next(
            (r for r in live if hmac.compare_digest(r.otp_hash, hash_otp(r.id, code))), None
        )
        if match is None:
            if live:
                PasswordResetToken.objects.filter(pk__in=[r.pk for r in live]).update(
                    otp_attempts=F("otp_attempts") + 1
                )
            return None

        now = timezone.now()
        match.used_at = now
        match.save(update_fields=["used_at"])
        raw = secrets.token_urlsafe(48)
        PasswordResetToken.objects.create(
            user=match.user,
            token_hash=hash_token(raw),
            expires_at=now + OTP_EXCHANGE_TTL,
        )
        return raw

