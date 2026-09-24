"""Email delivery with a hard safety switch.

  outbox : nothing leaves the app. Messages are stored and viewable under "Emails".
  test   : every message goes to EMAIL_TEST_RECIPIENT. The subject and a banner
           name the parent it was meant for. No parent can receive anything.
  live   : messages go to parents.

The redirect happens here, below every caller, so no statement, receipt, or
future feature can reach a parent unless the app was started with EMAIL_MODE=live.
"""
import html as html_mod

import requests


class DeliveryError(Exception):
    pass


class SendGridBackend:
    URL = "https://api.sendgrid.com/v3/mail/send"

    def __init__(self, api_key, from_email, from_name, timeout=15):
        self.api_key, self.from_email, self.from_name, self.timeout = api_key, from_email, from_name, timeout

    def send(self, to_email, subject, html, text):
        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": self.from_email, "name": self.from_name},
            "subject": subject,
            "content": [{"type": "text/plain", "value": text}, {"type": "text/html", "value": html}],
            "tracking_settings": {"click_tracking": {"enable": False, "enable_text": False}},
        }
        try:
            r = requests.post(self.URL, json=payload, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.api_key}"})
        except requests.RequestException as e:
            raise DeliveryError(f"Couldn't reach SendGrid: {e.__class__.__name__}")
        if r.status_code != 202:
            raise DeliveryError(f"SendGrid refused the message ({r.status_code}): {r.text[:300]}")
        return r.headers.get("X-Message-Id")


class Mailer:
    def __init__(self, mode, test_recipient=None, backend=None):
        if mode not in ("outbox", "test", "live"):
            raise ValueError(f"unknown email mode {mode!r}")
        if mode == "test" and not test_recipient:
            raise ValueError("test mode needs a test recipient")
        if mode in ("test", "live") and backend is None:
            raise ValueError(f"{mode} mode needs an email backend")
        self.mode, self.test_recipient, self.backend = mode, test_recipient, backend

    def prepare(self, intended_email, subject, html, text):
        """What will actually be sent, and where. Pure: sends nothing."""
        if self.mode == "live":
            return intended_email, subject, html, text
        if self.mode == "test":
            banner = ("<div style=\"background:#fff4d6;border:2px solid #c98a00;padding:10px 14px;margin:0 0 16px;"
                      "font-family:Arial,sans-serif;font-size:14px;color:#1a1a1a\"><strong>TEST EMAIL.</strong> "
                      f"In live mode this would go to {html_mod.escape(intended_email)}.</div>")
            return (self.test_recipient, f"[TEST → {intended_email}] {subject}", banner + html,
                    f"TEST EMAIL. In live mode this would go to {intended_email}.\n\n" + text)
        return None, subject, html, text      # outbox: stored only

    def deliver(self, intended_email, subject, html, text):
        to, subject, html, text = self.prepare(intended_email, subject, html, text)
        if to is None:
            return {"delivered_to": None, "subject": subject, "html": html, "text": text, "message_id": None}
        message_id = self.backend.send(to, subject, html, text)
        return {"delivered_to": to, "subject": subject, "html": html, "text": text, "message_id": message_id}


def mailer_from_config(cfg):
    mode = cfg["EMAIL_MODE"]
    backend = None
    if mode in ("test", "live"):
        backend = SendGridBackend(cfg["SENDGRID_API_KEY"], cfg["EMAIL_FROM"], cfg["EMAIL_FROM_NAME"])
    return Mailer(mode, cfg.get("EMAIL_TEST_RECIPIENT") or None, backend)
