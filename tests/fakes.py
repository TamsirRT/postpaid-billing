"""In-memory stand-ins for the repo and Supabase Auth, for web-layer tests."""
import uuid

from app.auth import AuthError


class FakeAuth:
    def __init__(self):
        self.users = {}  # email -> (user_id, password)

    def add(self, email, password):
        uid = str(uuid.uuid4())
        self.users[email.lower()] = (uid, password)
        return uid

    def sign_in(self, email, password):
        rec = self.users.get(email.lower())
        if not rec or rec[1] != password:
            raise AuthError("Email or password is incorrect.")
        return {"user_id": rec[0], "email": email}

    def sign_up(self, email, password):
        if email.lower() in self.users:
            raise AuthError("User already registered")
        self.add(email, password)
        return True


class FakeRepo:
    def __init__(self, institution=True):
        self.staff = {}  # user_id -> dict
        self.audit_log = []
        self.institution = {
            "id": "inst-1", "slug": "sacred-heart", "name": "Sacred Heart School of Glyndon",
            "auto_send_enabled": False, "default_price_cents": 790, "timezone": "America/New_York",
            "ordering_location_name": "Sacred Heart School of Glyndon", "ordering_module_name": "Order",
        } if institution else None
        self.dashboard_row = {
            "students_owing": 0, "outstanding_cents": 0, "oldest_unpaid_date": None,
            "last_run_at": None, "orders_known_through": None, "open_review_items": 0,
            "current_cycle_status": None, "billed_without_contact": 0,
        }
        self.students = {}    # id -> row (as public.students, never with pin)
        self.balances = {}    # student_id -> cents due
        self.guardians = {}   # id -> row
        self.links = set()    # (guardian_id, student_id)
        self.roster_import_result = {"roster_students": 0, "with_valid_email": 0,
                                     "guardians_created": 0, "links_created": 0}

    def get_institution(self, slug):
        return self.institution if self.institution and self.institution["slug"] == slug else None

    def touch_staff(self, user_id, email):
        row = self.staff.setdefault(user_id, {"user_id": user_id, "email": email, "role": None})
        row["email"] = email
        return dict(row)

    def get_staff(self, user_id):
        row = self.staff.get(user_id)
        return dict(row) if row else None

    def list_staff(self):
        return [dict(r, last_seen_at="today") for r in self.staff.values()]

    def set_role(self, target, role, actor, actor_email):
        row = self.staff.get(target)
        if not row:
            return None
        before = row["role"]
        row["role"] = role
        self.audit_log.append({"actor": actor, "target": target, "before": before, "after": role})
        return dict(row)

    def find_staff_by_email(self, email):
        return next((dict(r) for r in self.staff.values() if r["email"].lower() == email.lower()), None)

    def dashboard(self, institution_id):
        return dict(self.dashboard_row)

    # ------------------------------------------------------------ audit
    def audit(self, institution_id, actor, actor_email, action, entity, entity_id=None, after=None):
        self.audit_log.append({"actor": actor, "action": action, "entity": entity, "after": after})

    # ------------------------------------------------------------ students
    def add_student(self, first, last, grade="3", home_room="Smith", balance=0):
        sid = str(uuid.uuid4())
        self.students[sid] = {"id": sid, "first_name": first, "last_name": last, "grade_level": grade,
                              "home_room": home_room, "roster_email": None, "roster_phone": None}
        self.balances[sid] = balance
        return sid

    def _reachable(self, sid):
        return sum(1 for (gid, s) in self.links if s == sid
                   and self.guardians[gid]["email"] and self.guardians[gid]["receives_notices"])

    def _count(self, sid):
        return sum(1 for (_, s) in self.links if s == sid)

    def list_students(self, institution_id, query=None, missing_only=False):
        out = []
        for sid, st in self.students.items():
            name = f"{st['first_name']} {st['last_name']}".lower()
            if query and query.lower() not in name:
                continue
            if missing_only and not (self.balances[sid] > 0 and self._reachable(sid) == 0):
                continue
            out.append(dict(st, balance_due_cents=self.balances[sid], unpaid_count=1 if self.balances[sid] else 0,
                            guardian_count=self._count(sid), reachable_count=self._reachable(sid)))
        return out

    def get_student(self, institution_id, student_id):
        st = self.students.get(student_id)
        if not st:
            return None
        return dict(st, balance_due_cents=self.balances[student_id], open_cents=self.balances[student_id],
                    credit_cents=0, unpaid_count=1 if self.balances[student_id] else 0, oldest_unpaid_date=None)

    # ------------------------------------------------------------ guardians
    def student_guardians(self, institution_id, student_id):
        return [dict(self.guardians[gid]) for (gid, s) in sorted(self.links) if s == student_id]

    def get_guardian(self, institution_id, guardian_id):
        gd = self.guardians.get(guardian_id)
        return dict(gd) if gd else None

    def guardian_children(self, institution_id, guardian_id):
        return [dict(self.students[s]) for (gid, s) in self.links if gid == guardian_id]

    def find_guardian_by_email(self, institution_id, email, exclude_id=None):
        for gid, gd in self.guardians.items():
            if gd["email"] and gd["email"].lower() == email.lower() and gid != exclude_id:
                return dict(gd)
        return None

    def add_guardian_to_student(self, institution_id, student_id, name, email, phone, actor, actor_email):
        existing = self.find_guardian_by_email(institution_id, email) if email else None
        if existing:
            gid, created = existing["id"], 0
        else:
            gid, created = str(uuid.uuid4()), 1
            self.guardians[gid] = {"id": gid, "name": name, "email": email, "phone": phone,
                                   "receives_notices": True, "source": "manual"}
        self.links.add((gid, student_id))
        self.audit_log.append({"actor": actor, "action": "link_guardian", "target": student_id})
        return {"guardian_id": gid, "created": created, "linked": 1, "audited": 1}

    def update_guardian(self, institution_id, guardian_id, name, email, phone, receives_notices, actor, actor_email):
        gd = self.guardians.get(guardian_id)
        if not gd:
            return None
        before = dict(gd)
        gd.update(name=name, email=email, phone=phone, receives_notices=receives_notices)
        self.audit_log.append({"actor": actor, "action": "update_guardian", "before": before, "after": dict(gd)})
        return dict(gd, audited=1)

    def unlink_guardian(self, institution_id, guardian_id, student_id, actor, actor_email):
        if (guardian_id, student_id) in self.links:
            self.links.discard((guardian_id, student_id))
            self.audit_log.append({"actor": actor, "action": "unlink_guardian"})
            return 1
        return 0

    def import_roster_contacts(self, institution_id, actor, actor_email):
        self.audit_log.append({"actor": actor, "action": "import_roster_contacts"})
        return dict(self.roster_import_result, audited=1)

    def billed_without_contact(self, institution_id):
        rows = []
        for sid, st in self.students.items():
            if self.balances[sid] > 0 and self._reachable(sid) == 0:
                reason = "no_contact" if self._count(sid) == 0 else "no_email"
                rows.append(dict(st, balance_due_cents=self.balances[sid], unpaid_count=1,
                                 oldest_unpaid_date="2026-09-15", reason=reason))
        return rows

    # ------------------------------------------------------------ phase 1 (web-layer stand-ins; real SQL is tested in test_phase1.py)
    def student_lunches(self, institution_id, student_id):
        return []

    def student_payments(self, institution_id, student_id):
        return []
