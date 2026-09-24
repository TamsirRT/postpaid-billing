"""A thin Stripe client: create Checkout Sessions, read PaymentIntents, verify webhooks.

Card and bank details are entered on Stripe's hosted Checkout page; nothing
sensitive ever reaches this app.
"""
import hashlib
import hmac
import json
import time

import requests


class StripeError(Exception):
    pass


class WebhookSignatureError(Exception):
    pass


def _flatten(params, prefix=""):
    """{'a': {'b': [1]}} -> [('a[b][0]', '1')] : Stripe's form encoding."""
    out = []
    items = params.items() if isinstance(params, dict) else enumerate(params)
    for k, v in items:
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, (dict, list)):
            out.extend(_flatten(v, key))
        elif v is not None:
            out.append((key, "true" if v is True else "false" if v is False else str(v)))
    return out


class StripeClient:
    BASE = "https://api.stripe.com/v1"

    def __init__(self, secret_key, timeout=20):
        self.secret_key, self.timeout = secret_key, timeout

    def _call(self, method, path, params=None, idempotency_key=None):
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            r = requests.request(method, self.BASE + path, headers=headers, timeout=self.timeout,
                                 data=_flatten(params) if method == "POST" and params else None,
                                 params=_flatten(params) if method == "GET" and params else None)
        except requests.RequestException as e:
            raise StripeError(f"Couldn't reach Stripe: {e.__class__.__name__}")
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400:
            msg = (body.get("error") or {}).get("message") or r.text[:300]
            raise StripeError(f"Stripe refused the request ({r.status_code}): {msg}")
        return body

    def create_checkout_session(self, params, idempotency_key):
        return self._call("POST", "/checkout/sessions", params, idempotency_key)

    def retrieve_payment_intent(self, payment_intent_id):
        return self._call("GET", f"/payment_intents/{payment_intent_id}", {"expand": ["latest_charge"]})


def verify_webhook(payload, sig_header, secret, tolerance=300, now=None):
    """Checks Stripe's signature header and returns the parsed event. Raises WebhookSignatureError."""
    if not sig_header or not secret:
        raise WebhookSignatureError("missing signature")
    parts = {}
    for item in sig_header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError):
        raise WebhookSignatureError("no timestamp")
    signed = f"{ts}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in parts.get("v1", [])):
        raise WebhookSignatureError("bad signature")
    if abs((now or time.time()) - ts) > tolerance:
        raise WebhookSignatureError("too old")
    try:
        return json.loads(payload)
    except ValueError:
        raise WebhookSignatureError("not JSON")


def sign_payload(payload, secret, ts=None):
    """For tests: the header Stripe would send."""
    ts = int(ts or time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"
