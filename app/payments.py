"""Rules for what a PARENT may pay online (phase 3 checkout uses this).

Parents pay for whole lunches, oldest first, each at its own price: the only
allowed amounts are the running totals in billing.v_payment_options. The
database enforces the same rule on billing.payment_intents, so this check exists
to give the parent a clear message before anything reaches Stripe.
Staff-recorded payments (cash, check, Zoho) are deliberately not restricted.
"""


class PaymentAmountError(Exception):
    """The amount isn't allowed. The message is safe to show a parent."""


def lunches_for_amount(options, amount_cents):
    """options: rows from Repo.payment_options (oldest first). Returns how many lunches the amount pays."""
    if not options:
        raise PaymentAmountError("There's nothing to pay right now.")
    for opt in options:
        if int(opt["amount_cents"]) == int(amount_cents):
            return int(opt["lunches"])
    raise PaymentAmountError("Prices or lunches changed since this page loaded. Reload the page and choose again.")


def option_for_lunches(options, lunches):
    """The amount to charge for paying `lunches` oldest lunches."""
    for opt in options:
        if int(opt["lunches"]) == int(lunches):
            return int(opt["amount_cents"])
    raise PaymentAmountError("Choose how many lunches to pay for.")
