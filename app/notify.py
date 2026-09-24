"""Composing and sending statements and receipts.

Every message is written to billing.notifications BEFORE delivery (status
queued) with its full content, then marked sent or failed. So "what did we tell
this family, and when" is always answerable, including for failures.
"""
import json

from flask import render_template

from .mailer import DeliveryError
from .portal import portal_token, token_hash_hex

FINE_PRINT = ("Unpaid post-paid lunches are billed at the current rate for their date and may change until paid. "
              "Paid lunches are locked at the price paid.")


def portal_url(app_config, repo, institution, guardian):
    version = int(guardian["token_version"])
    token = portal_token(app_config["PORTAL_SECRET"], guardian["id"], version)
    repo.ensure_portal_token_hash(guardian["id"], version, token_hash_hex(token))
    return f"{app_config['PUBLIC_BASE_URL']}/p/{token}"


def _deliver(repo, mailer, institution, guardian, kind, balances, actor, subject, html, text):
    nid = repo.insert_notification(institution["id"], guardian["id"], kind, balances, actor, mailer.mode,
                                   guardian["email"], subject, html, text)
    try:
        out = mailer.deliver(guardian["email"], subject, html, text)
    except DeliveryError as e:
        repo.finish_notification(nid, "failed", None, None, str(e)[:1000], subject, html, text)
        return {"id": nid, "status": "failed", "error": str(e)}
    repo.finish_notification(nid, "sent", out["delivered_to"], out["message_id"], None,
                             out["subject"], out["html"], out["text"])
    return {"id": nid, "status": "sent", "delivered_to": out["delivered_to"]}


def compose_statement(app_config, repo, institution, guardian):
    """Returns None if nothing is owed. Otherwise subject/html/text and the balances stated."""
    children = repo.guardian_owing_children(institution["id"], guardian["id"])
    if not children:
        return None
    for c in children:
        c["lunches"] = repo.unpaid_lunches(institution["id"], c["student_id"])
    total = sum(int(c["balance_due_cents"]) for c in children)
    names = " & ".join(c["first_name"] for c in children)
    ctx = {"institution": institution, "guardian": guardian, "children": children, "total_cents": total,
           "portal_url": portal_url(app_config, repo, institution, guardian), "fine_print": FINE_PRINT,
           "support_email": app_config.get("SUPPORT_EMAIL")}
    return {
        "subject": f"Lunch balance for {names}: {_dollars(total)}",
        "html": render_template("email/statement.html", **ctx),
        "text": render_template("email/statement.txt", **ctx),
        "balances": {str(c["student_id"]): int(c["balance_due_cents"]) for c in children},
    }


def send_statement(app_config, repo, mailer, institution, guardian_id, actor, kind="manual_individual",
                   override_recent=False):
    guardian = repo.statement_guardian(institution["id"], guardian_id)
    if not guardian or not guardian["email"] or guardian["receives_notices"] not in (True, "t", "true"):
        return {"status": "skipped", "reason": "no email or opted out"}
    if not override_recent and repo.statement_sent_recently(guardian_id, mailer.mode):
        return {"status": "skipped", "reason": "statement sent in the last 24 hours"}
    msg = compose_statement(app_config, repo, institution, guardian)
    if msg is None:
        return {"status": "skipped", "reason": "nothing owed"}
    return _deliver(repo, mailer, institution, guardian, kind, msg["balances"], actor,
                    msg["subject"], msg["html"], msg["text"])


def send_statements(app_config, repo, mailer, institution, guardian_ids, actor, override_recent=False):
    counts = {"sent": 0, "failed": 0, "skipped_recent": 0, "skipped_other": 0}
    failures = []
    for gid in guardian_ids:
        r = send_statement(app_config, repo, mailer, institution, gid, actor, "manual_global", override_recent)
        if r["status"] == "sent":
            counts["sent"] += 1
        elif r["status"] == "failed":
            counts["failed"] += 1
            failures.append(r["error"])
        elif "24 hours" in r.get("reason", ""):
            counts["skipped_recent"] += 1
        else:
            counts["skipped_other"] += 1
    counts["first_error"] = failures[0] if failures else None
    return counts


def send_receipts(app_config, repo, mailer, institution, payment_id, actor):
    p = repo.payment_for_receipt(institution["id"], payment_id)
    if not p:
        return []
    results = []
    for guardian in repo.reachable_guardians_of(institution["id"], p["student_id"]):
        g = repo.statement_guardian(institution["id"], guardian["id"])
        covered = p["covered"]
        if isinstance(covered, str):
            covered = json.loads(covered)
        ctx = {"institution": institution, "guardian": g, "p": p, "covered": covered,
               "portal_url": portal_url(app_config, repo, institution, g), "support_email": app_config.get("SUPPORT_EMAIL")}
        subject = f"Payment received for {p['first_name']}: {_dollars(int(p['amount_cents']))}"
        results.append(_deliver(repo, mailer, institution, g, "receipt",
                                {str(p["student_id"]): int(p["balance_due_cents"])}, actor, subject,
                                render_template("email/receipt.html", **ctx), render_template("email/receipt.txt", **ctx)))
    return results


def _dollars(cents):
    from . import format_cents
    return format_cents(cents)
