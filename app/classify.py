"""The classification run: orders -> students, then every new check-in -> how it's billed.

Safe to run any number of times. Each step only touches what isn't done yet:
already-classified check-ins are skipped, mappings are reused, review items
are deduplicated, and credit is only applied where there's credit and an open lunch.
"""
from collections import defaultdict
from datetime import date

from .billing_rules import billing_start, classify_check_in
from .names import student_name_key


class ClassificationError(Exception):
    """Can't run. The message is safe to show staff."""


def _as_date(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _as_bool(v):
    if v in (True, "t", "true", "True"):
        return True
    if v in (False, "f", "false", "False"):
        return False
    return None


def roster_name_keys(repo):
    keys = defaultdict(list)
    for s in repo.student_names():
        key = student_name_key(s["first_name"], s["last_name"])
        if key:
            keys[key].append(str(s["id"]))
    return dict(keys)


def run_classification(repo, institution, actor=None, actor_email="system"):
    """Match orders, classify check-ins, reconcile late orders, apply credit. Returns counts."""
    start = billing_start(institution["slug"])
    if start is None:
        raise ClassificationError("No billing start date is set for this school in app/billing_rules.py.")
    known_through = repo.orders_known_through(institution["id"], institution.get("timezone") or "America/New_York")
    if known_through is None:
        raise ClassificationError("Import an orders export first. Without one, every check-in would look like "
                                  "it had no order.")
    known_through = _as_date(known_through)
    if known_through <= start:
        raise ClassificationError(f"The newest orders export was taken on or before {start}. "
                                  "Import a newer export first.")

    run_id = repo.start_run(institution["id"], start, known_through, actor)
    counts = {}
    try:
        counts["match"] = repo.match_orders(institution["id"], roster_name_keys(repo), start)

        check_ins, post_paid_days, order_days = repo.classification_inputs(institution["id"], start, known_through)
        billed = {(str(r["student_id"]), _as_date(r["service_date"])) for r in post_paid_days}
        ordered = {(str(r["student_id"]), _as_date(r["service_date"])) for r in order_days}

        rows, unknown = [], []
        for c in check_ins:
            sid, d = str(c["student_id"]), _as_date(c["check_in_date"])
            if not _as_bool(c["known_student"]):
                unknown.append({"student_id": sid, "service_date": d.isoformat()})
                continue
            cls, note = classify_check_in(d, _as_bool(c["getting_lunch"]), _as_bool(c["bill_separately"]),
                                          (sid, d) in ordered, (sid, d) in billed, start)
            if cls is None:
                continue
            if cls == "post_paid":
                billed.add((sid, d))
            rows.append({"check_in_id": str(c["id"]), "student_id": sid, "service_date": d.isoformat(),
                         "classification": cls, "note": note})

        counts["classified"] = repo.insert_classified(institution["id"], run_id, rows)
        counts["unknown_student_review_items"] = repo.review_unknown_students(institution["id"], run_id, unknown)
        counts["late_orders"] = repo.reconcile_late_orders(institution["id"], run_id, start)
        counts["credit_applied_cents"] = repo.apply_all_credit(institution["id"])
        counts["window"] = {"from": start.isoformat(), "before": known_through.isoformat()}
    except Exception as e:
        repo.finish_run(run_id, "failed", counts, str(e)[:2000], actor_email)
        raise
    repo.finish_run(run_id, "succeeded", counts, None, actor_email)
    return counts
