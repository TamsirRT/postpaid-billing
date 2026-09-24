"""Routes: sign-in, dashboard, staff roles, students and their contacts."""
import csv
import io
import re as re_mod
import uuid as uuid_mod
from datetime import date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template,
                   request, url_for)

from .auth import AuthError, log_in, log_out, require_role
from .billing_rules import billing_start
from . import format_cents
from .classify import ClassificationError, run_classification
from .contacts import REASON_LABELS, clean_contact_form, csv_safe
from .importer import OrdersFileError, parse_orders_csv
from .repo import ROLES, role_at_least

bp = Blueprint("main", __name__)


def _repo():
    return current_app.extensions["repo"]


def _safe_next(target):
    """Only allow redirects back into this app (no open redirect)."""
    if not target:
        return url_for("main.dashboard")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/"):
        return url_for("main.dashboard")
    return target


@bp.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- sign-in

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", next=request.args.get("next", ""))

    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    if not email or not password:
        flash("Enter your email and password.", "error")
        return render_template("login.html", email=email, next=request.form.get("next", "")), 400
    try:
        user = current_app.extensions["auth"].sign_in(email, password)
    except AuthError as e:
        flash(str(e), "error")
        return render_template("login.html", email=email, next=request.form.get("next", "")), 401

    log_in(user["user_id"], user["email"])
    staff = _repo().touch_staff(user["user_id"], user["email"])
    if not staff or staff.get("role") is None:
        return redirect(url_for("main.pending"))
    return redirect(_safe_next(request.form.get("next")))


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    if len(password) < 12:
        flash("Use a password of at least 12 characters.", "error")
        return render_template("signup.html", email=email), 400
    try:
        current_app.extensions["auth"].sign_up(email, password)
    except AuthError as e:
        flash(str(e), "error")
        return render_template("signup.html", email=email), 400
    flash("Account created. If Supabase asks you to confirm your email, do that, then sign in. "
          "A super admin still has to grant you access.", "info")
    return redirect(url_for("main.login"))


@bp.post("/logout")
def logout():
    log_out()
    return redirect(url_for("main.login"))


@bp.get("/pending")
def pending():
    if not g.get("staff"):
        return redirect(url_for("main.login"))
    if g.staff.get("role"):
        return redirect(url_for("main.dashboard"))
    return render_template("pending.html")


# ---------------------------------------------------------------- dashboard

@bp.get("/")
@require_role("viewer")
def dashboard():
    inst = g.institution
    stats = _repo().dashboard(inst["id"]) if inst else None
    start = billing_start(inst["slug"]) if inst else None
    return render_template("dashboard.html", institution=inst, stats=stats, billing_start=start)


# ---------------------------------------------------------------- staff roles

@bp.get("/admin/staff")
@require_role("super_admin")
def staff_list():
    return render_template("staff.html", staff=_repo().list_staff(), roles=ROLES)


@bp.post("/admin/staff/<user_id>/role")
@require_role("super_admin")
def staff_set_role(user_id):
    role = request.form.get("role") or None
    if role is not None and role not in ROLES:
        abort(400, description="Unknown role.")
    if str(user_id) == str(g.staff["user_id"]):
        flash("You can't change your own role. Ask another super admin.", "error")
        return redirect(url_for("main.staff_list"))

    result = _repo().set_role(user_id, role, g.staff["user_id"], g.staff["email"])
    if not result:
        abort(404)
    flash(f"{result['email']} is now {result['role'] or 'without access'}.", "info")
    return redirect(url_for("main.staff_list"))


# ---------------------------------------------------------------- students & contacts


def _truthy(v):
    return v in (True, "t", "true")


def _reachable(guardians):
    """At least one linked contact can actually be emailed."""
    return any(gd.get("email") and _truthy(gd.get("receives_notices")) for gd in guardians)


def _student_money(inst, sid):
    return {"lunches": _repo().student_lunches(inst["id"], sid),
            "payments": _repo().student_payments(inst["id"], sid),
            "today": datetime.now(ZoneInfo(inst.get("timezone") or "America/New_York")).date().isoformat()}


def _inst_or_redirect():
    """Return the institution, or None after flashing (caller redirects)."""
    if not g.institution:
        flash("Set up the institution first (see README).", "error")
    return g.institution


@bp.get("/students")
@require_role("viewer")
def students():
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    q = (request.args.get("q") or "").strip()
    missing_only = request.args.get("missing") == "1"
    rows = _repo().list_students(inst["id"], q or None, missing_only)
    return render_template("students.html", rows=rows, q=q, missing_only=missing_only)


@bp.get("/students/<uuid:student_id>")
@require_role("viewer")
def student_detail(student_id):
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    student = _repo().get_student(inst["id"], str(student_id))
    if not student:
        abort(404)
    guardians = _repo().student_guardians(inst["id"], str(student_id))
    return render_template("student.html", s=student, guardians=guardians, reachable=_reachable(guardians),
                           form={}, errors=[], **_student_money(inst, str(student_id)))


@bp.post("/students/<uuid:student_id>/guardians")
@require_role("admin")
def student_add_guardian(student_id):
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    sid = str(student_id)
    student = _repo().get_student(inst["id"], sid)
    if not student:
        abort(404)
    values, errors = clean_contact_form(request.form)
    if errors:
        guardians = _repo().student_guardians(inst["id"], sid)
        return render_template("student.html", s=student, guardians=guardians, reachable=_reachable(guardians),
                               form=request.form, errors=errors, **_student_money(inst, sid)), 400
    result = _repo().add_guardian_to_student(inst["id"], sid, values["name"], values["email"], values["phone"],
                                             g.staff["user_id"], g.staff["email"])
    if result and int(result["created"]) == 0:
        flash(f"Linked the existing contact {values['email']}. Edit it to change their name or phone.", "info")
    else:
        flash("Contact added.", "info")
    return redirect(url_for("main.student_detail", student_id=student_id))


@bp.post("/students/<uuid:student_id>/guardians/<uuid:guardian_id>/unlink")
@require_role("admin")
def student_unlink_guardian(student_id, guardian_id):
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    removed = _repo().unlink_guardian(inst["id"], str(guardian_id), str(student_id),
                                      g.staff["user_id"], g.staff["email"])
    flash("Contact removed from this child." if removed else "That contact wasn't linked.", "info")
    return redirect(url_for("main.student_detail", student_id=student_id))


@bp.route("/guardians/<uuid:guardian_id>", methods=["GET", "POST"])
@require_role("viewer")
def guardian_detail(guardian_id):
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    gid = str(guardian_id)
    guardian = _repo().get_guardian(inst["id"], gid)
    if not guardian:
        abort(404)
    children = _repo().guardian_children(inst["id"], gid)
    back = request.values.get("back") or ""

    if request.method == "GET":
        return render_template("guardian.html", gd=guardian, children=children, form=guardian, errors=[], back=back)

    if not role_at_least(g.staff.get("role"), "admin"):
        abort(403)
    values, errors = clean_contact_form(request.form)
    receives = request.form.get("receives_notices") == "on"
    if not errors and values["email"] and _repo().find_guardian_by_email(inst["id"], values["email"], exclude_id=gid):
        errors.append(f"Another contact already uses {values['email']}. Link that contact to the child instead.")
    if errors:
        form = dict(request.form, receives_notices=receives)
        return render_template("guardian.html", gd=guardian, children=children, form=form, errors=errors, back=back), 400
    _repo().update_guardian(inst["id"], gid, values["name"], values["email"], values["phone"], receives,
                            g.staff["user_id"], g.staff["email"])
    flash("Contact saved.", "info")
    target = _safe_next(back) if back else url_for("main.guardian_detail", guardian_id=guardian_id)
    return redirect(target)


@bp.get("/contacts/missing")
@require_role("viewer")
def missing_contacts():
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    rows = _repo().billed_without_contact(inst["id"])
    return render_template("missing_contacts.html", rows=rows, labels=REASON_LABELS)


@bp.get("/contacts/missing.csv")
@require_role("viewer")
def missing_contacts_csv():
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    rows = _repo().billed_without_contact(inst["id"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Last name", "First name", "Grade", "Homeroom", "Balance due",
                "Unpaid lunches", "Oldest unpaid lunch", "Contact problem"])
    for r in rows:
        cents = int(r["balance_due_cents"])
        w.writerow([csv_safe(r["last_name"]), csv_safe(r["first_name"]), csv_safe(r["grade_level"]),
                    csv_safe(r["home_room"]), f"{cents / 100:.2f}", r["unpaid_count"],
                    r["oldest_unpaid_date"] or "", REASON_LABELS.get(r["reason"], r["reason"])])
    _repo().audit(inst["id"], g.staff["user_id"], g.staff["email"], "export_missing_contacts",
                  "v_billed_without_contact", after={"rows": len(rows)})
    filename = f"missing-contacts-{inst['slug']}-{date.today().isoformat()}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-store"})


@bp.post("/contacts/import-roster")
@require_role("admin")
def import_roster():
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    r = _repo().import_roster_contacts(inst["id"], g.staff["user_id"], g.staff["email"])
    skipped = int(r["roster_students"]) - int(r["with_valid_email"])
    flash(f"Roster import: {r['guardians_created']} new contacts, {r['links_created']} new links to children. "
          f"{skipped} of {r['roster_students']} students have no usable email in the roster and were skipped.", "info")
    return redirect(url_for("main.missing_contacts"))


# ================================================================ phase 1: orders, classification, review

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _inst_tz(inst):
    return ZoneInfo(inst.get("timezone") or "America/New_York")


def _run_and_flash(inst):
    try:
        c = run_classification(_repo(), inst, g.staff["user_id"], g.staff["email"])
    except ClassificationError as e:
        flash(str(e), "error")
        return None
    cl = c["classified"]
    late = c["late_orders"]
    parts = [f"{cl.get('post_paid', 0)} post-paid", f"{cl.get('pre_ordered', 0)} pre-ordered",
             f"{cl.get('no_lunch', 0)} no lunch", f"{cl.get('excluded', 0)} excluded",
             f"{cl.get('duplicate', 0)} duplicate"]
    msg = "Classified new check-ins: " + ", ".join(parts) + "."
    if late["to_pre_ordered"] or late["to_post_paid"]:
        msg += f" Re-sorted {late['to_pre_ordered'] + late['to_post_paid']} earlier check-in(s) after order changes."
    if c["match"]["review_items_opened"] or c["unknown_student_review_items"] or late["flagged"]:
        msg += " New items are waiting in the review queue."
    flash(msg, "info")
    return c


@bp.route("/orders", methods=["GET", "POST"])
@require_role("viewer")
def orders():
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    tz = _inst_tz(inst)
    if request.method == "GET":
        return render_template("orders.html", batches=_repo().recent_batches(inst["id"]),
                               now_local=datetime.now(tz).strftime("%Y-%m-%dT%H:%M"))
    if not role_at_least(g.staff.get("role"), "admin"):
        abort(403)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose the ALL ORDERS CSV file to upload.", "error")
        return redirect(url_for("main.orders"))
    data = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        flash("That file is over 20 MB. Is it the right export?", "error")
        return redirect(url_for("main.orders"))
    try:
        exported_local = datetime.strptime(request.form.get("exported_at", ""), "%Y-%m-%dT%H:%M")
    except ValueError:
        flash("Enter when the export was downloaded.", "error")
        return redirect(url_for("main.orders"))
    exported_at = exported_local.replace(tzinfo=tz)
    if exported_at > datetime.now(tz):
        flash("The export time can't be in the future.", "error")
        return redirect(url_for("main.orders"))
    try:
        parsed = parse_orders_csv(data, inst["ordering_location_name"], inst["ordering_module_name"])
    except OrdersFileError as e:
        flash(str(e), "error")
        return redirect(url_for("main.orders"))
    res = _repo().import_orders(inst["id"], upload.filename[:200], exported_at.isoformat(), parsed["rows"],
                                g.staff["user_id"], g.staff["email"])
    st = parsed["stats"]
    flash(f"Imported {upload.filename}: {st['school_rows']} {inst['name']} order lines "
          f"({res['inserted']} new, {st['school_rows'] - res['inserted']} already on file), "
          f"orders dated {st['first_date']} to {st['last_date']}.", "info")
    _run_and_flash(inst)
    return redirect(url_for("main.orders"))


@bp.post("/classify")
@require_role("admin")
def classify_now():
    inst = _inst_or_redirect()
    if inst:
        _run_and_flash(inst)
    return redirect(_safe_next(request.form.get("back")))


@bp.get("/review")
@require_role("viewer")
def review():
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    items = _repo().open_review_items(inst["id"])
    students = _repo().student_picklist() if role_at_least(g.staff.get("role"), "admin") else []
    return render_template("review.html", items=items, students=students)


@bp.post("/review/<uuid:item_id>/match")
@require_role("admin")
def review_match(item_id):
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    student_id = request.form.get("student_id") or ""
    try:
        student_id = str(uuid_mod.UUID(student_id))
    except ValueError:
        flash("Pick a student from the list.", "error")
        return redirect(url_for("main.review"))
    if not _repo().get_student(inst["id"], student_id):
        abort(404)
    res = _repo().resolve_order_item(inst["id"], str(item_id), student_id, g.staff["user_id"], g.staff["email"])
    if not res["resolved"]:
        flash("That item was already handled.", "info")
        return redirect(url_for("main.review"))
    flash(f"Matched. {res['orders_matched']} order line(s) now belong to that student; future orders under this "
          "name are matched automatically.", "info")
    _run_and_flash(inst)
    return redirect(url_for("main.review"))


@bp.post("/review/<uuid:item_id>/dismiss")
@require_role("admin")
def review_dismiss(item_id):
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    note = (request.form.get("note") or "").strip()[:500] or None
    n = _repo().dismiss_review_item(inst["id"], str(item_id), note, g.staff["user_id"], g.staff["email"])
    flash("Dismissed." if n else "That item was already handled.", "info")
    return redirect(url_for("main.review"))


# ================================================================ phase 1: rates
def _parse_dollars(raw):
    """'7.90', '$7.9', '8' -> cents; None if invalid."""
    text = (raw or "").strip().lstrip("$").strip()
    if not re_mod.fullmatch(r"\d{1,4}(\.\d{1,2})?", text):
        return None
    whole, _, frac = text.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


@bp.get("/rates")
@require_role("viewer")
def rates():
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    return render_template("rates.html", periods=_repo().rate_periods(inst["id"]), errors=[], form={})


@bp.post("/rates/default")
@require_role("super_admin")
def rates_default():
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    cents = _parse_dollars(request.form.get("price"))
    if cents is None:
        flash("Enter a price like 7.90.", "error")
        return redirect(url_for("main.rates"))
    r = _repo().set_default_price(inst["id"], cents, g.staff["user_id"], g.staff["email"])
    if r["old_cents"] == r["new_cents"]:
        flash("Price unchanged.", "info")
    else:
        delta = (r["new_cents"] - r["old_cents"]) * r["lunches"]
        flash(f"Standard price changed from {format_cents(r['old_cents'])} to {format_cents(r['new_cents'])}. "
              f"{r['lunches']} unpaid lunch(es) for {r['students']} student(s) now cost "
              f"{'more' if delta > 0 else 'less'}: {format_cents(abs(delta))} in total. Paid lunches keep their price.",
              "info")
    return redirect(url_for("main.rates"))


@bp.post("/rates/periods")
@require_role("super_admin")
def rates_add_period():
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    f = request.form
    errors = []
    try:
        starts, ends = date.fromisoformat(f.get("starts_on", "")), date.fromisoformat(f.get("ends_on", ""))
        if ends < starts:
            errors.append("The end date is before the start date.")
    except ValueError:
        starts = ends = None
        errors.append("Enter both dates.")
    cents = _parse_dollars(f.get("price"))
    if cents is None:
        errors.append("Enter a price like 5.00.")
    label = (f.get("label") or "").strip()[:80] or None
    if not errors:
        clash = _repo().find_overlapping_period(inst["id"], starts, ends)
        if clash:
            errors.append(f"That overlaps the existing period {clash['starts_on']} to {clash['ends_on']}. "
                          "Periods can't overlap.")
    if errors:
        return render_template("rates.html", periods=_repo().rate_periods(inst["id"]), errors=errors, form=f), 400
    r = _repo().add_rate_period(inst["id"], starts, ends, cents, label, g.staff["user_id"], g.staff["email"])
    flash(f"Added {format_cents(cents)} for {starts} to {ends}. {r['unpaid_lunches_affected']} unpaid lunch(es) in "
          "that range now use this price.", "info")
    return redirect(url_for("main.rates"))


@bp.post("/rates/periods/<uuid:period_id>/delete")
@require_role("super_admin")
def rates_delete_period(period_id):
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    n = _repo().delete_rate_period(inst["id"], str(period_id), g.staff["user_id"], g.staff["email"])
    flash("Removed. Unpaid lunches in that range are back on the standard price." if n else
          "That period can't be removed: payments have already been applied at its price.", "info" if n else "error")
    return redirect(url_for("main.rates"))


# ================================================================ phase 1: waivers and offline payments
@bp.post("/students/<uuid:student_id>/lunches/<uuid:check_in_id>/waive")
@require_role("admin")
def student_waive(student_id, check_in_id):
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    reason = (request.form.get("reason") or "").strip()[:300]
    if not reason:
        flash("Give a reason for waiving the lunch.", "error")
        return redirect(url_for("main.student_detail", student_id=student_id))
    r = _repo().waive_lunch(inst["id"], str(student_id), str(check_in_id), reason, g.staff["user_id"], g.staff["email"])
    if not r["waived"]:
        flash("That lunch can't be waived (already waived, or not a post-paid lunch).", "error")
    else:
        msg = "Lunch waived."
        released, moved = r["released_cents"], r["reapplied_cents"]
        if released:
            msg += f" The {format_cents(released)} already paid toward it was released:"
            parts = []
            if moved:
                parts.append(f"{format_cents(moved)} went to other unpaid lunches")
            if released - moved > 0:
                parts.append(f"{format_cents(released - moved)} is kept as credit for future lunches")
            msg += " " + " and ".join(parts) + "."
        flash(msg, "info")
    return redirect(url_for("main.student_detail", student_id=student_id))


@bp.post("/students/<uuid:student_id>/payments")
@require_role("admin")
def student_record_payment(student_id):
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    f = request.form
    cents = _parse_dollars(f.get("amount"))
    method = f.get("method")
    errors = []
    if not cents:
        errors.append("Enter an amount like 15.80.")
    if method not in ("cash", "check", "zoho", "other"):
        errors.append("Choose how it was paid.")
    try:
        received = datetime.strptime(f.get("received_on", ""), "%Y-%m-%d").replace(hour=12, tzinfo=_inst_tz(inst))
        if received.date() > datetime.now(_inst_tz(inst)).date():
            errors.append("The date received can't be in the future.")
    except ValueError:
        errors.append("Enter the date the payment was received.")
    note = (f.get("note") or "").strip()[:300] or None
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("main.student_detail", student_id=student_id))
    if not _repo().get_student(inst["id"], str(student_id)):
        abort(404)
    r = _repo().record_offline_payment(inst["id"], str(student_id), cents, method, received.isoformat(), note,
                                       g.staff["user_id"], g.staff["email"])
    left = cents - r["applied_cents"]
    flash(f"Recorded {format_cents(cents)} ({method}). {format_cents(r['applied_cents'])} applied to unpaid lunches, "
          f"oldest first" + (f"; {format_cents(left)} kept as credit for future lunches." if left else "."), "info")
    return redirect(url_for("main.student_detail", student_id=student_id))


@bp.post("/students/<uuid:student_id>/payments/<uuid:payment_id>/reverse")
@require_role("admin")
def student_reverse_payment(student_id, payment_id):
    inst = _inst_or_redirect()
    if not inst:
        return redirect(url_for("main.dashboard"))
    reason = (request.form.get("reason") or "").strip()[:300]
    if not reason:
        flash("Give a reason for reversing the payment.", "error")
        return redirect(url_for("main.student_detail", student_id=student_id))
    r = _repo().reverse_payment(inst["id"], str(student_id), str(payment_id), reason,
                                g.staff["user_id"], g.staff["email"])
    if r["reversed"]:
        flash("Payment reversed. The lunches it paid are unpaid again" +
              (f"; {format_cents(r['reapplied_cents'])} of other credit was applied to them." if r["reapplied_cents"]
               else "."), "info")
    else:
        flash("That payment can't be reversed here (already reversed, or an online payment).", "error")
    return redirect(url_for("main.student_detail", student_id=student_id))
