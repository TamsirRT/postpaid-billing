"""Online payments through Stripe Checkout.

  start_checkout : parent picks N lunches -> intent row (the database checks the
                   amount) -> Stripe Checkout Session -> parent goes to Stripe.
  handle_event   : Stripe webhooks move the intent along. Money is recorded only
                   when Stripe says it has arrived: at once for cards, after a few
                   business days for bank payments. Refunds and disputes made in
                   Stripe reverse the payment in the app.

Every handler is safe to run twice; Stripe retries deliveries.
"""
from .stripe_client import StripeError

PAYMENT_METHOD_TYPES = ["card", "us_bank_account"]


class CheckoutError(Exception):
    pass


def _stripe(app):
    return app.extensions.get("stripe")


def payments_enabled(app):
    return _stripe(app) is not None


def start_checkout(app, repo, institution, guardian, student, lunches, portal_link):
    """Returns the Stripe Checkout URL. portal_link is the parent's own page (absolute URL)."""
    client = _stripe(app)
    if client is None:
        raise CheckoutError("Online payment isn't available yet.")
    sid = str(student["id"])
    if repo.processing_intents(institution["id"], sid):
        raise CheckoutError("A bank payment for this child is still clearing. You can pay for more lunches once it clears.")
    options = {int(o["lunches"]): o for o in repo.payment_options(institution["id"], sid)}
    option = options.get(int(lunches))
    if option is None:
        raise CheckoutError("That amount isn't available any more. The page has been refreshed with current amounts.")
    amount = int(option["amount_cents"])
    try:
        intent_id = repo.create_payment_intent(institution["id"], sid, str(guardian["id"]), amount, int(lunches))
    except Exception:     # the database's whole-lunch guard (prices changed a moment ago)
        raise CheckoutError("Prices changed while you were paying. Please choose again.")

    first = min(options.values(), key=lambda o: int(o["lunches"]))["service_date"]
    last = option["service_date"]
    count = f"{lunches} lunch{'es' if int(lunches) != 1 else ''}"
    child = f"{student['first_name']} {student['last_name']}"
    params = {
        "mode": "payment",
        "payment_method_types": PAYMENT_METHOD_TYPES,
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": amount,
                "product_data": {
                    "name": f"{institution['name']} lunches for {child}",
                    "description": f"{count}, {_d(first)}" + (f" to {_d(last)}" if last != first else "")
                                   + ". Includes the payment-processing amount shown on your statement.",
                },
            },
        }],
        "client_reference_id": intent_id,
        "metadata": {"intent_id": intent_id, "student_id": sid, "institution_id": str(institution["id"])},
        "payment_intent_data": {
            "description": f"{count} for {child}",
            "metadata": {"intent_id": intent_id, "student_id": sid},
        },
        "success_url": f"{portal_link}?paid={intent_id}",
        "cancel_url": f"{portal_link}?canceled=1",
    }
    try:
        session = client.create_checkout_session(params, idempotency_key=f"checkout-{intent_id}")
    except StripeError as e:
        repo.set_intent_status(intent_id, "canceled", ["pending"], reason=str(e)[:500])
        raise CheckoutError("We couldn't reach the payment service. Nothing was charged. Please try again in a few minutes.")
    repo.attach_checkout(intent_id, session["id"], session["url"])
    return session["url"]


def _d(value):
    from . import format_when
    return format_when(value)


# ---------------------------------------------------------------- webhooks
def handle_event(app, repo, mailer, institution, event):
    """Returns a short outcome string (logged). Raises on a transient failure so Stripe retries."""
    etype, obj = event.get("type", ""), (event.get("data") or {}).get("object") or {}
    if etype.startswith("checkout.session."):
        return _checkout_event(app, repo, mailer, institution, etype, obj)
    if etype == "charge.refunded":
        return _refund(repo, obj)
    if etype == "charge.dispute.created":
        return _dispute_created(repo, obj)
    if etype == "charge.dispute.closed":
        return _dispute_closed(repo, obj)
    return "ignored"


def _intent_for_session(repo, institution, session):
    intent_id = (session.get("metadata") or {}).get("intent_id") or session.get("client_reference_id")
    intent = repo.get_payment_intent(intent_id) if intent_id else None
    if not intent:
        return None, "unknown intent"
    if str(intent["institution_id"]) != str(institution["id"]):
        return None, "other institution"
    if intent["processor_ref"] != session.get("id"):
        return None, "session mismatch"
    return intent, None


def _checkout_event(app, repo, mailer, institution, etype, session):
    intent, problem = _intent_for_session(repo, institution, session)
    if problem:
        return problem
    pi = session.get("payment_intent")
    if etype == "checkout.session.expired":
        n = repo.set_intent_status(intent["id"], "canceled", ["pending"], reason="Checkout expired or abandoned")
        return "expired" if n else "no change"
    if etype == "checkout.session.async_payment_failed":
        n = repo.set_intent_status(intent["id"], "failed", ["pending", "processing"],
                                   reason="The bank payment didn't go through", stripe_pi=pi)
        return "failed" if n else "no change"
    if etype not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return "ignored"
    if not pi:
        return "no payment intent"
    if session.get("payment_status") != "paid":
        # bank payment submitted; money arrives in a few business days
        n = repo.set_intent_status(intent["id"], "processing", ["pending"], stripe_pi=pi, method="ach")
        return "processing" if n else "no change"
    method = _method_of(app, pi)
    result = repo.record_stripe_payment(str(intent["id"]), pi, method, int(session.get("amount_total") or 0))
    if result["created"]:
        from .notify import send_receipts
        try:
            send_receipts(app.config, repo, mailer, institution, result["payment_id"], None)
        except Exception:      # a receipt problem must never make Stripe retry a recorded payment
            app.logger.exception("receipt for Stripe payment %s failed", pi)
        return f"recorded {method}"
    return "already recorded"


def _method_of(app, pi):
    """'card' or 'ach', from the charge Stripe made."""
    data = _stripe(app).retrieve_payment_intent(pi)
    charge = data.get("latest_charge") or {}
    kind = ((charge.get("payment_method_details") or {}).get("type")) if isinstance(charge, dict) else None
    if kind is None:
        kind = (data.get("payment_method_types") or ["card"])[0]
    return "ach" if kind == "us_bank_account" else "card"


def _refund(repo, charge):
    pi = charge.get("payment_intent")
    payment = repo.payment_by_processor_ref(pi) if pi else None
    if not payment:
        return "refund for unknown payment"
    refunded, total = int(charge.get("amount_refunded") or 0), int(charge.get("amount") or 0)
    if refunded >= total:
        repo.note_on_intent(pi, refunded_cents=refunded)
        n = repo.reverse_stripe_payment(payment, "Refunded in Stripe", charge.get("id"))
        return "reversed (refund)" if n else "already reversed"
    from . import format_cents
    repo.note_on_intent(pi, refunded_cents=refunded, attention=(
        f"Partly refunded in Stripe ({format_cents(refunded)} of {format_cents(total)}). The app did not change the "
        "balance: record the kept amount as an offline payment and reverse this one, or refund the rest in Stripe."))
    return "partial refund flagged"


def _dispute_created(repo, dispute):
    pi = dispute.get("payment_intent")
    payment = repo.payment_by_processor_ref(pi) if pi else None
    if not payment:
        return "dispute for unknown payment"
    reason = (dispute.get("reason") or "").replace("_", " ")
    repo.note_on_intent(pi, disputed=True, attention=(
        f"Disputed by the payer{f' ({reason})' if reason else ''}. Stripe took the money back and the app reopened "
        "the lunches. Respond in Stripe if the charge was valid."))
    n = repo.reverse_stripe_payment(payment, "Disputed in Stripe" + (f": {reason}" if reason else ""), dispute.get("id"))
    return "reversed (dispute)" if n else "already reversed"


def _dispute_closed(repo, dispute):
    pi = dispute.get("payment_intent")
    if dispute.get("status") == "won" and pi and repo.payment_by_processor_ref(pi):
        repo.note_on_intent(pi, attention=(
            "Dispute won: Stripe returned the money. The payment stays reversed in the app; record it again as an "
            "offline payment (method: other) so the lunches are paid."))
        return "dispute won flagged"
    return "ignored"
