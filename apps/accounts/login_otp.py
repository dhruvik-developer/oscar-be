"""Two-factor sign-in: an emailed 6-digit code after a correct password.

The challenge id goes back to the browser; the code goes only to the inbox.
Codes are stored as keyed hashes (see ``password_reset.hash_otp``), live for
LOGIN_OTP_TTL, and die after ``LoginChallenge.MAX_ATTEMPTS`` wrong guesses.
"""
import hmac
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import LoginChallenge
from .password_reset import hash_otp, new_code, send_code_email, workspace_name

LOGIN_OTP_TTL = timedelta(minutes=5)
RESEND_COOLDOWN = timedelta(seconds=30)
#: How long after the first code a "resend" may still be asked for.
RESEND_WINDOW = timedelta(minutes=15)


def mask_email(email):
    """``drashti08@gmail.com`` → ``d•••••••8@gmail.com``: enough to recognise."""
    local, _, domain = (email or "").partition("@")
    if len(local) <= 2:
        return f"{local[:1]}•@{domain}"
    return f"{local[0]}{'•' * (len(local) - 2)}{local[-1]}@{domain}"


def start_challenge(user, *, ip=None):
    """Email `user` a sign-in code. Returns (challenge, sent)."""
    now = timezone.now()
    # Only the newest code works.
    LoginChallenge.objects.filter(user=user, used_at__isnull=True).update(used_at=now)
    code = new_code()
    challenge = LoginChallenge.objects.create(
        user=user, otp_hash="", expires_at=now + LOGIN_OTP_TTL, ip=ip
    )
    challenge.otp_hash = hash_otp(challenge.id, code)
    challenge.save(update_fields=["otp_hash"])

    workspace = workspace_name(user)
    minutes = int(LOGIN_OTP_TTL.total_seconds() // 60)
    sent = send_code_email(
        user,
        subject=f"{code} is your {workspace} sign-in code",
        body=(
            f"Hello {user.name or user.email},\n\n"
            f"Someone signed in to {workspace} with your password.\n\n"
            f"Your sign-in code is: {code}\n"
            f"It is valid for {minutes} minutes.\n\n"
            "If this was not you, do not share the code; change your password "
            "now, because your password is known to someone else."
        ),
        debug_hint=f"sign-in code {code}",
    )
    return challenge, sent


def resend_challenge(challenge_id, *, ip=None):
    """A fresh code for the user behind `challenge_id`, or None.

    Returns (challenge, sent) or None when the id is unknown, used, too old,
    or asked again within RESEND_COOLDOWN.
    """
    old = LoginChallenge.objects.select_related("user", "user__client").filter(pk=challenge_id).first()
    now = timezone.now()
    if (
        old is None
        or old.used_at is not None
        or old.created_at < now - RESEND_WINDOW
        or old.created_at > now - RESEND_COOLDOWN
    ):
        return None
    return start_challenge(old.user, ip=ip)


def verify_challenge(challenge_id, code):
    """The user when `code` matches the live challenge, else None.

    A wrong code is counted (and committed) against the challenge.
    """
    code = (code or "").strip()
    with transaction.atomic():
        challenge = (
            # Lock the challenge row only; the role join is nullable.
            LoginChallenge.objects.select_for_update(of=("self",))
            .select_related("user", "user__client", "user__role")
            .filter(pk=challenge_id)
            .first()
        )
        if challenge is None or not challenge.is_usable:
            return None
        if len(code) != 6 or not hmac.compare_digest(
            challenge.otp_hash, hash_otp(challenge.id, code)
        ):
            LoginChallenge.objects.filter(pk=challenge.pk).update(attempts=F("attempts") + 1)
            return None
        challenge.used_at = timezone.now()
        challenge.save(update_fields=["used_at"])
        return challenge.user
