"""All SQL the app runs, in one place.

Every write that needs an audit entry does it in the SAME statement (a
data-modifying CTE), so the change and its audit row commit or fail together.
"""

import json

from .repo_billing import BillingRepoMixin
from .repo_notify import NotifyRepoMixin
from .repo_pay import PayRepoMixin

ROLES = ("viewer", "admin", "super_admin")
ROLE_RANK = {None: 0, "viewer": 1, "admin": 2, "super_admin": 3}


class Repo(BillingRepoMixin, NotifyRepoMixin, PayRepoMixin):
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------- institutions
    SQL_GET_INSTITUTION = """
        select id, slug, name, ordering_location_name, ordering_module_name,
               cycle_anchor_date, cycle_length_days, auto_send_enabled, timezone, default_price_cents, default_fee_cents
          from billing.institutions
         where slug = %(slug)s
    """

    def get_institution(self, slug):
        return self.db.fetch_one(self.SQL_GET_INSTITUTION, {"slug": slug})

    SQL_CREATE_INSTITUTION = """
        insert into billing.institutions (slug, name, ordering_location_name, ordering_module_name, cycle_anchor_date)
        values (%(slug)s, %(name)s, %(location)s, %(module)s, %(anchor)s)
        returning id, slug, name
    """

    def create_institution(self, slug, name, location, module, anchor):
        return self.db.fetch_one(self.SQL_CREATE_INSTITUTION, {
            "slug": slug, "name": name, "location": location, "module": module, "anchor": anchor,
        })

    # -------------------------------------------------------------------- staff
    SQL_TOUCH_STAFF = """
        insert into billing.staff_roles (user_id, email)
        values (%(user_id)s, %(email)s)
        on conflict (user_id) do update
           set email = excluded.email, last_seen_at = now()
        returning user_id, email, role
    """

    def touch_staff(self, user_id, email):
        """Record a sign-in. First sign-in creates the row with no role (no access)."""
        return self.db.fetch_one(self.SQL_TOUCH_STAFF, {"user_id": user_id, "email": email})

    SQL_GET_STAFF = "select user_id, email, role from billing.staff_roles where user_id = %(user_id)s"

    def get_staff(self, user_id):
        return self.db.fetch_one(self.SQL_GET_STAFF, {"user_id": user_id})

    SQL_LIST_STAFF = """
        select user_id, email, role, granted_at, first_seen_at, last_seen_at
          from billing.staff_roles
         order by (role is not null), lower(email)
    """

    def list_staff(self):
        return self.db.fetch_all(self.SQL_LIST_STAFF)

    SQL_SET_ROLE = """
        with before as (
            select user_id, email, role from billing.staff_roles where user_id = %(target)s
        ),
        upd as (
            update billing.staff_roles
               set role       = %(role)s,
                   granted_by = case when %(role)s::text is null then null else %(actor)s::uuid end,
                   granted_at = case when %(role)s::text is null then null else now() end
             where user_id = %(target)s
            returning user_id, email, role
        ),
        audit as (
            insert into billing.audit_log (actor, actor_email, action, entity, entity_id, before, after)
            select %(actor)s, %(actor_email)s, 'set_role', 'staff_roles', upd.user_id::text,
                   jsonb_build_object('role', before.role), jsonb_build_object('role', upd.role)
              from upd join before using (user_id)
            returning id
        )
        select upd.user_id, upd.email, upd.role, (select count(*) from audit) as audited
          from upd
    """

    def set_role(self, target_user_id, role, actor_user_id, actor_email):
        if role not in ROLES and role is not None:
            raise ValueError(f"unknown role {role!r}")
        return self.db.fetch_one(self.SQL_SET_ROLE, {
            "target": target_user_id, "role": role, "actor": actor_user_id, "actor_email": actor_email,
        })

    SQL_FIND_STAFF_BY_EMAIL = """
        select user_id, email, role from billing.staff_roles where lower(email) = lower(%(email)s)
    """

    def find_staff_by_email(self, email):
        return self.db.fetch_one(self.SQL_FIND_STAFF_BY_EMAIL, {"email": email})

    # ---------------------------------------------------------------- dashboard
    SQL_DASHBOARD = """
        select
          (select count(*) from billing.v_student_balances
            where institution_id = %(iid)s and balance_due_cents > 0)                    as students_owing,
          (select coalesce(sum(balance_due_cents), 0) from billing.v_student_balances
            where institution_id = %(iid)s and balance_due_cents > 0)                    as outstanding_cents,
          (select min(oldest_unpaid_date) from billing.v_student_balances
            where institution_id = %(iid)s)                                              as oldest_unpaid_date,
          (select max(finished_at) from billing.reconciliation_runs
            where institution_id = %(iid)s and status = 'succeeded')                     as last_run_at,
          (select max(exported_at) from billing.import_batches
            where institution_id = %(iid)s)                                              as orders_known_through,
          (select count(*) from billing.review_items
            where institution_id = %(iid)s and status = 'open')                          as open_review_items,
          (select status from billing.billing_cycles
            where institution_id = %(iid)s order by period_start desc limit 1)          as current_cycle_status,
          (select count(*) from billing.v_billed_without_contact
            where institution_id = %(iid)s)                                              as billed_without_contact
    """

    def dashboard(self, institution_id):
        return self.db.fetch_one(self.SQL_DASHBOARD, {"iid": institution_id})

    # ------------------------------------------------------------ audit
    SQL_AUDIT = """
        insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
        values (%(iid)s, %(actor)s, %(actor_email)s, %(action)s, %(entity)s, %(entity_id)s, %(after)s::jsonb)
        returning id
    """

    def audit(self, institution_id, actor, actor_email, action, entity, entity_id=None, after=None):
        return self.db.fetch_one(self.SQL_AUDIT, {
            "iid": institution_id, "actor": actor, "actor_email": actor_email, "action": action,
            "entity": entity, "entity_id": entity_id, "after": json.dumps(after) if after is not None else None,
        })

    # ------------------------------------------------------------ students
    # public.students belongs to the check-in app: read only, and `pin` is
    # never selected anywhere in billing.
    SQL_LIST_STUDENTS = """
        select s.id, s.first_name, s.last_name, s.grade_level, s.home_room,
               coalesce(b.balance_due_cents, 0)  as balance_due_cents,
               coalesce(b.unpaid_count, 0)       as unpaid_count,
               coalesce(cs.guardian_count, 0)    as guardian_count,
               coalesce(cs.reachable_count, 0)   as reachable_count
          from public.students s
          left join billing.v_student_balances b
                 on b.student_id = s.id and b.institution_id = %(iid)s
          left join billing.v_student_contact_status cs
                 on cs.student_id = s.id and cs.institution_id = %(iid)s
         where (%(pattern)s::text is null
                or s.first_name ilike %(pattern)s
                or s.last_name  ilike %(pattern)s
                or (s.first_name || ' ' || s.last_name) ilike %(pattern)s)
           and (not %(missing_only)s
                or (coalesce(b.balance_due_cents, 0) > 0 and coalesce(cs.reachable_count, 0) = 0))
         order by lower(s.last_name), lower(s.first_name)
         limit 1000
    """

    def list_students(self, institution_id, query=None, missing_only=False):
        pattern = None
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
        return self.db.fetch_all(self.SQL_LIST_STUDENTS, {
            "iid": institution_id, "pattern": pattern, "missing_only": bool(missing_only),
        })

    SQL_GET_STUDENT = """
        select s.id, s.first_name, s.last_name, s.grade_level, s.home_room,
               s.email as roster_email, s.phone as roster_phone,
               coalesce(b.balance_due_cents, 0) as balance_due_cents,
               coalesce(b.open_cents, 0)        as open_cents,
               coalesce(b.credit_cents, 0)      as credit_cents,
               coalesce(b.unpaid_count, 0)      as unpaid_count,
               b.oldest_unpaid_date
          from public.students s
          left join billing.v_student_balances b
                 on b.student_id = s.id and b.institution_id = %(iid)s
         where s.id = %(sid)s
    """

    def get_student(self, institution_id, student_id):
        return self.db.fetch_one(self.SQL_GET_STUDENT, {"iid": institution_id, "sid": student_id})

    # ------------------------------------------------------------ guardians
    SQL_STUDENT_GUARDIANS = """
        select g.id, g.name, g.email, g.phone, g.receives_notices, g.source
          from billing.guardian_students gs
          join billing.guardians g on g.id = gs.guardian_id
         where gs.student_id = %(sid)s and gs.institution_id = %(iid)s
         order by (g.email is null), lower(coalesce(g.name, g.email, g.phone))
    """

    def student_guardians(self, institution_id, student_id):
        return self.db.fetch_all(self.SQL_STUDENT_GUARDIANS, {"iid": institution_id, "sid": student_id})

    SQL_GET_GUARDIAN = """
        select id, name, email, phone, receives_notices, source, updated_at
          from billing.guardians
         where id = %(gid)s and institution_id = %(iid)s
    """

    def get_guardian(self, institution_id, guardian_id):
        return self.db.fetch_one(self.SQL_GET_GUARDIAN, {"iid": institution_id, "gid": guardian_id})

    SQL_GUARDIAN_CHILDREN = """
        select s.id, s.first_name, s.last_name, s.grade_level
          from billing.guardian_students gs
          join public.students s on s.id = gs.student_id
         where gs.guardian_id = %(gid)s and gs.institution_id = %(iid)s
         order by lower(s.last_name), lower(s.first_name)
    """

    def guardian_children(self, institution_id, guardian_id):
        return self.db.fetch_all(self.SQL_GUARDIAN_CHILDREN, {"iid": institution_id, "gid": guardian_id})

    SQL_FIND_GUARDIAN_BY_EMAIL = """
        select id, name, email from billing.guardians
         where institution_id = %(iid)s and email is not null and lower(email) = lower(%(email)s)
           and (%(exclude)s::uuid is null or id <> %(exclude)s::uuid)
    """

    def find_guardian_by_email(self, institution_id, email, exclude_id=None):
        return self.db.fetch_one(self.SQL_FIND_GUARDIAN_BY_EMAIL,
                                 {"iid": institution_id, "email": email, "exclude": exclude_id})

    SQL_ADD_GUARDIAN_TO_STUDENT = """
        with existing as (
            select id from billing.guardians
             where institution_id = %(iid)s and %(email)s::text is not null and lower(email) = lower(%(email)s)
        ),
        created as (
            insert into billing.guardians (institution_id, name, email, phone)
            select %(iid)s, %(name)s, %(email)s, %(phone)s
             where not exists (select 1 from existing)
            returning id
        ),
        chosen as (
            select id from existing union all select id from created
        ),
        link as (
            insert into billing.guardian_students (guardian_id, student_id, institution_id)
            select id, %(sid)s, %(iid)s from chosen
            on conflict do nothing
            returning guardian_id
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'link_guardian', 'guardian_students', %(sid)s::text,
                   jsonb_build_object('guardian_id', chosen.id, 'created', exists (select 1 from created),
                                      'name', %(name)s::text, 'email', %(email)s::text, 'phone', %(phone)s::text)
              from chosen
            returning id
        )
        select chosen.id as guardian_id,
               (select count(*) from created) as created,
               (select count(*) from link)    as linked,
               (select count(*) from audit)   as audited
          from chosen
    """

    def add_guardian_to_student(self, institution_id, student_id, name, email, phone, actor, actor_email):
        """Link a guardian to a child. Reuses an existing guardian with the same email."""
        return self.db.fetch_one(self.SQL_ADD_GUARDIAN_TO_STUDENT, {
            "iid": institution_id, "sid": student_id, "name": name, "email": email, "phone": phone,
            "actor": actor, "actor_email": actor_email,
        })

    SQL_UPDATE_GUARDIAN = """
        with before as (
            select id, name, email, phone, receives_notices
              from billing.guardians where id = %(gid)s and institution_id = %(iid)s
        ),
        upd as (
            update billing.guardians
               set name = %(name)s, email = %(email)s, phone = %(phone)s,
                   receives_notices = %(receives)s, updated_at = now()
             where id = %(gid)s and institution_id = %(iid)s
            returning id, name, email, phone, receives_notices
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, before, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'update_guardian', 'guardians', upd.id::text,
                   to_jsonb(before), to_jsonb(upd)
              from upd join before using (id)
            returning id
        )
        select upd.*, (select count(*) from audit) as audited from upd
    """

    def update_guardian(self, institution_id, guardian_id, name, email, phone, receives_notices, actor, actor_email):
        return self.db.fetch_one(self.SQL_UPDATE_GUARDIAN, {
            "iid": institution_id, "gid": guardian_id, "name": name, "email": email, "phone": phone,
            "receives": bool(receives_notices), "actor": actor, "actor_email": actor_email,
        })

    SQL_UNLINK_GUARDIAN = """
        with del as (
            delete from billing.guardian_students
             where guardian_id = %(gid)s and student_id = %(sid)s and institution_id = %(iid)s
            returning guardian_id, student_id
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, before)
            select %(iid)s, %(actor)s, %(actor_email)s, 'unlink_guardian', 'guardian_students', del.student_id::text,
                   jsonb_build_object('guardian_id', del.guardian_id)
              from del
            returning id
        )
        select (select count(*) from del) as removed
    """

    def unlink_guardian(self, institution_id, guardian_id, student_id, actor, actor_email):
        row = self.db.fetch_one(self.SQL_UNLINK_GUARDIAN, {
            "iid": institution_id, "gid": guardian_id, "sid": student_id,
            "actor": actor, "actor_email": actor_email,
        })
        return int(row["removed"]) if row else 0

    # Copies usable contacts from the school roster into billing. Junk values
    # ('redacted', blanks, malformed) are skipped, never stored. Re-running is
    # harmless: existing guardians are matched by email and links are unique.
    SQL_IMPORT_ROSTER_CONTACTS = r"""
        with roster as (
            select s.id as student_id,
                   lower(btrim(s.email)) as email,
                   case when length(regexp_replace(coalesce(s.phone, ''), '\D', '', 'g')) between 10 and 15
                        then btrim(s.phone) end as phone
              from public.students s
             where btrim(coalesce(s.email, '')) ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'
        ),
        per_email as (
            select email, max(phone) as phone from roster group by email
        ),
        created as (
            insert into billing.guardians (institution_id, email, phone, source)
            select %(iid)s, pe.email, pe.phone, 'roster'
              from per_email pe
             where not exists (
                   select 1 from billing.guardians g
                    where g.institution_id = %(iid)s and g.email is not null and lower(g.email) = pe.email)
            returning id, email
        ),
        all_guardians as (
            select id, lower(email) as email from billing.guardians
             where institution_id = %(iid)s and email is not null
            union all
            select id, email from created
        ),
        linked as (
            insert into billing.guardian_students (guardian_id, student_id, institution_id)
            select ag.id, r.student_id, %(iid)s
              from roster r join all_guardians ag on ag.email = r.email
            on conflict do nothing
            returning guardian_id
        ),
        counts as (
            select (select count(*) from public.students)  as roster_students,
                   (select count(*) from roster)           as with_valid_email,
                   (select count(*) from created)          as guardians_created,
                   (select count(*) from linked)           as links_created
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'import_roster_contacts', 'guardians', to_jsonb(counts)
              from counts
            returning id
        )
        select counts.*, (select count(*) from audit) as audited from counts
    """

    def import_roster_contacts(self, institution_id, actor, actor_email):
        return self.db.fetch_one(self.SQL_IMPORT_ROSTER_CONTACTS, {
            "iid": institution_id, "actor": actor, "actor_email": actor_email,
        })

    # ------------------------------------------------------------ missing contacts
    SQL_BILLED_WITHOUT_CONTACT = """
        select s.id, s.first_name, s.last_name, s.grade_level, s.home_room,
               m.balance_due_cents, m.unpaid_count, m.oldest_unpaid_date, m.reason
          from billing.v_billed_without_contact m
          left join public.students s on s.id = m.student_id
         where m.institution_id = %(iid)s
         order by lower(s.grade_level), lower(s.last_name), lower(s.first_name)
    """

    def billed_without_contact(self, institution_id):
        return self.db.fetch_all(self.SQL_BILLED_WITHOUT_CONTACT, {"iid": institution_id})


def role_at_least(role, needed):
    return ROLE_RANK.get(role, 0) >= ROLE_RANK[needed]
