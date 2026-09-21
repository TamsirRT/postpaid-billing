"""Routes: sign-in, dashboard, staff roles, students and their contacts."""
import csv
import io
from datetime import date
from urllib.parse import urlparse

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template,
                   request, url_for)

from .auth import AuthError, log_in, log_out, require_role
from .billing_rules import billing_start
from .contacts import REASON_LABELS, clean_contact_form, csv_safe
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
                           form={}, errors=[])


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
                               form=request.form, errors=errors), 400
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
