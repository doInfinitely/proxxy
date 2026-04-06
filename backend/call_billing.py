"""Call billing: minute tracking, Stripe checkout, Firebase auth verification.

Free tier: 10 minutes/month for signed-in users (resets on 1st of month).
Minute packs purchasable via Stripe Checkout:
  - Starter:    15 min / $2.99
  - Popular:    60 min / $9.99
  - Best Value: 200 min / $24.99
"""

import logging
import math
import os
import time
from datetime import datetime, timezone

import stripe

logger = logging.getLogger(__name__)

# ─── Minute pack definitions ───

MINUTE_PACKS = {
    "starter": {"minutes": 15, "price_cents": 299, "label": "Starter"},
    "popular": {"minutes": 60, "price_cents": 999, "label": "Popular"},
    "best_value": {"minutes": 200, "price_cents": 2499, "label": "Best Value"},
}

FREE_MINUTES_PER_MONTH = 10

# ─── Firebase Admin (lazy init) ───

_db = None
_auth_mod = None
_init_attempted = False


def _init_firebase():
    """Lazy-initialize firebase-admin. Returns True if ready."""
    global _db, _auth_mod, _init_attempted
    if _db is not None:
        return True
    if _init_attempted:
        return False
    _init_attempted = True
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore, auth

        if not firebase_admin._apps:
            cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            if cred_path:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
            else:
                firebase_admin.initialize_app()

        _db = firestore.client()
        _auth_mod = auth
        logger.info("CallBilling: Firebase initialized")
        return True
    except Exception as e:
        logger.warning(f"CallBilling: Firebase init failed: {e}")
        return False


def get_db():
    """Return the Firestore client, or None if Firebase is unavailable."""
    if _init_firebase():
        return _db
    return None


def verify_firebase_token(id_token: str) -> str | None:
    """Verify a Firebase ID token and return the uid, or None on failure."""
    if not _init_firebase() or not _auth_mod:
        return None
    try:
        decoded = _auth_mod.verify_id_token(id_token)
        return decoded["uid"]
    except Exception as e:
        logger.debug(f"Token verification failed: {e}")
        return None


# ─── Minute balance ───


def _get_user_ref(uid: str):
    """Get Firestore document reference for a user."""
    if not _init_firebase():
        return None
    return _db.collection("users").document(uid)


def get_minutes_balance(uid: str) -> dict:
    """Get the user's current call minute balance.

    Returns: {free: int, purchased: int, total: int}
    """
    ref = _get_user_ref(uid)
    if not ref:
        return {"free": 0, "purchased": 0, "total": 0}

    doc = ref.get()
    data = doc.to_dict() or {} if doc.exists else {}
    call_data = data.get("callMinutes", {})

    free = call_data.get("free", FREE_MINUTES_PER_MONTH)
    purchased = call_data.get("purchased", 0)
    reset_at = call_data.get("freeResetAt")

    # Check if free minutes should reset (1st of month)
    now = datetime.now(timezone.utc)
    should_reset = False
    if reset_at is None:
        should_reset = True
    else:
        # reset_at is a Firestore Timestamp
        reset_dt = reset_at if isinstance(reset_at, datetime) else reset_at.date_time
        if now >= reset_dt:
            should_reset = True

    if should_reset:
        free = FREE_MINUTES_PER_MONTH
        # Next reset: 1st of next month
        if now.month == 12:
            next_reset = now.replace(year=now.year + 1, month=1, day=1,
                                     hour=0, minute=0, second=0, microsecond=0)
        else:
            next_reset = now.replace(month=now.month + 1, day=1,
                                     hour=0, minute=0, second=0, microsecond=0)
        ref.set({"callMinutes": {
            "free": free,
            "purchased": purchased,
            "freeResetAt": next_reset,
        }}, merge=True)

    return {"free": free, "purchased": purchased, "total": free + purchased}


def deduct_minutes(uid: str, minutes_used: float) -> dict:
    """Deduct minutes from user's balance (free first, then purchased).

    Args:
        minutes_used: Duration in minutes (will be rounded up to nearest integer).

    Returns: Updated balance dict.
    """
    ref = _get_user_ref(uid)
    if not ref:
        return {"free": 0, "purchased": 0, "total": 0}

    to_deduct = max(1, math.ceil(minutes_used))

    doc = ref.get()
    data = (doc.to_dict() or {}) if doc.exists else {}
    call_data = data.get("callMinutes", {})
    free = call_data.get("free", 0)
    purchased = call_data.get("purchased", 0)

    # Deduct from free first
    if free >= to_deduct:
        free -= to_deduct
    else:
        to_deduct -= free
        free = 0
        purchased = max(0, purchased - to_deduct)

    ref.set({"callMinutes": {
        "free": free,
        "purchased": purchased,
    }}, merge=True)

    return {"free": free, "purchased": purchased, "total": free + purchased}


def add_purchased_minutes(uid: str, minutes: int) -> dict:
    """Add purchased minutes to user's balance."""
    ref = _get_user_ref(uid)
    if not ref:
        return {"free": 0, "purchased": 0, "total": 0}

    doc = ref.get()
    data = (doc.to_dict() or {}) if doc.exists else {}
    call_data = data.get("callMinutes", {})
    current = call_data.get("purchased", 0)
    new_total = current + minutes

    ref.set({"callMinutes": {"purchased": new_total}}, merge=True)

    free = call_data.get("free", FREE_MINUTES_PER_MONTH)
    return {"free": free, "purchased": new_total, "total": free + new_total}


# ─── Stripe Checkout ───


def create_checkout_session(uid: str, pack_id: str, return_url: str) -> str | None:
    """Create a Stripe Checkout Session for a minute pack.

    Returns: Checkout session URL, or None on failure.
    """
    pack = MINUTE_PACKS.get(pack_id)
    if not pack:
        return None

    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not stripe.api_key:
        logger.error("STRIPE_SECRET_KEY not configured")
        return None

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": pack["price_cents"],
                    "product_data": {
                        "name": f"Proxxy — {pack['label']} ({pack['minutes']} call minutes)",
                        "description": f"{pack['minutes']} minutes of AI phone calls",
                    },
                },
                "quantity": 1,
            }],
            metadata={
                "uid": uid,
                "pack_id": pack_id,
                "minutes": str(pack["minutes"]),
            },
            success_url=return_url + "?payment=success",
            cancel_url=return_url + "?payment=cancelled",
        )
        return session.url
    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        return None


def handle_checkout_webhook(payload: bytes, sig_header: str) -> bool:
    """Process a Stripe webhook event. Returns True if handled successfully."""
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

    if not webhook_secret or not stripe.api_key:
        logger.error("Stripe not configured for webhooks")
        return False

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning(f"Webhook verification failed: {e}")
        return False

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        uid = meta.get("uid")
        minutes = int(meta.get("minutes", 0))

        if uid and minutes > 0:
            balance = add_purchased_minutes(uid, minutes)
            logger.info(f"Credited {minutes} minutes to {uid}. Balance: {balance}")
            return True

    return True  # Return True even for unhandled event types to avoid retries
