"""Phase 2 SQL: statements, receipts, the email log, and parent portal lookups. Mixed into Repo."""
import json


class NotifyRepoMixin:
    # ================================================================ statements
    # One row per guardian who can be emailed and has at least one child owing.
    SQL_STATEMENT_CANDIDATES = """
        select r.guardian_id, r.guardian_name, r.email,
               count(*)                    as children_owing,
               sum(r.balance_due_cents)    as total_due_cents,
               (select max(n.created_at) from billing.notifications n
                 where n.guardian_id = r.guardian_id and n.kind in ('statement', 'manual_individual', 'manual_global')
                   and n.mode = %(mode)s and n.status in ('queued', 'sent')) as last_statement_at
          from billing.v_statement_recipients r
         where r.institution_id = %(iid)s
         group by r.guardian_id, r.guardian_name, r.email
        having sum(r.balance_due_cents) >= %(min_cents)s
         order by lower(coalesce(r.guardian_name, r.email))
    """

    def statement_candidates(self, institution_id, mode, min_cents=1):
        return self.db.fetch_all(self.SQL_STATEMENT_CANDIDATES, {"iid": institution_id, "mode": mode,
                                                                 "min_cents": min_cents})

    SQL_GUARDIAN_OWING_CHILDREN = """
        select r.student_id, s.first_name, s.last_name, r.balance_due_cents
          from billing.v_statement_recipients r
          join public.students s on s.id = r.student_id
         where r.institution_id = %(iid)s and r.guardian_id = %(gid)s
         order by lower(s.first_name)
    """

    def guardian_owing_children(self, institution_id, guardian_id):
        return self.db.fetch_all(self.SQL_GUARDIAN_OWING_CHILDREN, {"iid": institution_id, "gid": guardian_id})

    SQL_UNPAID_LUNCHES = """
        select service_date, rate_label, price_cents, allocated_cents, open_cents, meal_cents, fee_cents
          from billing.v_check_in_ledger
         where institution_id = %(iid)s and student_id = %(sid)s and status in ('open', 'partial')
         order by service_date
    """

    def unpaid_lunches(self, institution_id, student_id):
        return self.db.fetch_all(self.SQL_UNPAID_LUNCHES, {"iid": institution_id, "sid": student_id})

    SQL_RECENT_STATEMENT = """
        select exists (
            select 1 from billing.notifications
             where guardian_id = %(gid)s and mode = %(mode)s
               and kind in ('statement', 'manual_individual', 'manual_global')
               and status in ('queued', 'sent')
               and created_at > now() - make_interval(hours => %(hours)s)
        ) as recent
    """

    def statement_sent_recently(self, guardian_id, mode, hours=24):
        v = self.db.fetch_one(self.SQL_RECENT_STATEMENT, {"gid": guardian_id, "mode": mode, "hours": hours})["recent"]
        return v in (True, "t", "true")

    # ================================================================ email log
    SQL_INSERT_NOTIFICATION = """
        insert into billing.notifications
               (institution_id, guardian_id, kind, balance_cents_at_send, status, sent_by, mode,
                intended_email, subject, body_html, body_text)
        values (%(iid)s, %(gid)s, %(kind)s, %(balances)s::jsonb, 'queued', %(actor)s, %(mode)s,
                %(intended)s, %(subject)s, %(html)s, %(text)s)
        returning id
    """

    def insert_notification(self, institution_id, guardian_id, kind, balances, actor, mode, intended_email,
                            subject, html, text):
        return self.db.fetch_one(self.SQL_INSERT_NOTIFICATION, {
            "iid": institution_id, "gid": guardian_id, "kind": kind, "balances": json.dumps(balances),
            "actor": actor, "mode": mode, "intended": intended_email, "subject": subject, "html": html,
            "text": text})["id"]

    SQL_FINISH_NOTIFICATION = """
        update billing.notifications
           set status = %(status)s, delivered_to = %(to)s, sendgrid_message_id = %(mid)s, error = %(error)s,
               subject = %(subject)s, body_html = %(html)s, body_text = %(text)s,
               sent_at = case when %(status)s = 'sent' then now() end
         where id = %(nid)s
        returning id
    """

    def finish_notification(self, notification_id, status, delivered_to, message_id, error, subject, html, text):
        self.db.fetch_one(self.SQL_FINISH_NOTIFICATION, {
            "nid": notification_id, "status": status, "to": delivered_to, "mid": message_id, "error": error,
            "subject": subject, "html": html, "text": text})

    SQL_LIST_NOTIFICATIONS = """
        select n.id, n.kind, n.mode, n.status, n.intended_email, n.delivered_to, n.subject, n.created_at,
               n.sent_at, n.error, g.name as guardian_name, s.email as sent_by_email
          from billing.notifications n
          join billing.guardians g on g.id = n.guardian_id
          left join billing.staff_roles s on s.user_id = n.sent_by
         where n.institution_id = %(iid)s
         order by n.created_at desc
         limit %(limit)s
    """

    def list_notifications(self, institution_id, limit=200):
        return self.db.fetch_all(self.SQL_LIST_NOTIFICATIONS, {"iid": institution_id, "limit": limit})

    SQL_GET_NOTIFICATION = """
        select n.id, n.kind, n.mode, n.status, n.intended_email, n.delivered_to, n.subject, n.body_html,
               n.body_text, n.created_at, n.sent_at, n.error, n.balance_cents_at_send, g.name as guardian_name
          from billing.notifications n
          join billing.guardians g on g.id = n.guardian_id
         where n.institution_id = %(iid)s and n.id = %(nid)s
    """

    def get_notification(self, institution_id, notification_id):
        return self.db.fetch_one(self.SQL_GET_NOTIFICATION, {"iid": institution_id, "nid": notification_id})

    # ================================================================ receipts
    SQL_PAYMENT_FOR_RECEIPT = """
        select p.id, p.student_id, p.amount_cents, p.method, p.received_at,
               s.first_name, s.last_name,
               coalesce((select json_agg(json_build_object('service_date', cb.service_date, 'amount_cents', a.amount_cents)
                                        order by cb.service_date)
                           from billing.payment_allocations a
                           join billing.check_in_billing cb on cb.check_in_id = a.check_in_id
                          where a.payment_id = p.id), '[]'::json) as covered,
               coalesce(b.balance_due_cents, 0) as balance_due_cents
          from billing.payments p
          join public.students s on s.id = p.student_id
          left join billing.v_student_balances b on b.student_id = p.student_id and b.institution_id = p.institution_id
         where p.id = %(pid)s and p.institution_id = %(iid)s
    """

    def payment_for_receipt(self, institution_id, payment_id):
        return self.db.fetch_one(self.SQL_PAYMENT_FOR_RECEIPT, {"iid": institution_id, "pid": payment_id})

    SQL_REACHABLE_GUARDIANS_OF = """
        select g.id, g.name, g.email
          from billing.guardian_students gs
          join billing.guardians g on g.id = gs.guardian_id
         where gs.institution_id = %(iid)s and gs.student_id = %(sid)s
           and g.email is not null and g.receives_notices
    """

    def reachable_guardians_of(self, institution_id, student_id):
        return self.db.fetch_all(self.SQL_REACHABLE_GUARDIANS_OF, {"iid": institution_id, "sid": student_id})

    # ================================================================ portal
    SQL_ENSURE_TOKEN_HASH = """
        update billing.guardians
           set portal_token_hash = decode(%(hash)s, 'hex'), token_issued_at = now()
         where id = %(gid)s and token_version = %(version)s
           and portal_token_hash is distinct from decode(%(hash)s, 'hex')
        returning id
    """

    def ensure_portal_token_hash(self, guardian_id, version, hash_hex):
        self.db.fetch_all(self.SQL_ENSURE_TOKEN_HASH, {"gid": guardian_id, "version": version, "hash": hash_hex})

    SQL_GUARDIAN_BY_TOKEN = """
        select g.id, g.institution_id, g.name, g.email, g.token_version
          from billing.guardians g
         where g.portal_token_hash = decode(%(hash)s, 'hex')
    """

    def guardian_by_token_hash(self, hash_hex):
        return self.db.fetch_one(self.SQL_GUARDIAN_BY_TOKEN, {"hash": hash_hex})

    SQL_GUARDIAN_TOKEN_VERSION = "select token_version from billing.guardians where id = %(gid)s and institution_id = %(iid)s"

    def guardian_token_version(self, institution_id, guardian_id):
        row = self.db.fetch_one(self.SQL_GUARDIAN_TOKEN_VERSION, {"iid": institution_id, "gid": guardian_id})
        return int(row["token_version"]) if row else None

    SQL_ROTATE_TOKEN = """
        with upd as (
            update billing.guardians
               set token_version = token_version + 1,
                   portal_token_hash = decode(%(hash)s, 'hex'), token_issued_at = now()
             where id = %(gid)s and institution_id = %(iid)s and token_version = %(from_version)s
            returning id, token_version
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'rotate_portal_link', 'guardians', upd.id::text,
                   jsonb_build_object('token_version', upd.token_version)
              from upd
            returning id
        )
        select count(*) as rotated from upd
    """

    def rotate_portal_token(self, institution_id, guardian_id, from_version, new_hash_hex, actor, actor_email):
        return int(self.db.fetch_one(self.SQL_ROTATE_TOKEN, {
            "iid": institution_id, "gid": guardian_id, "from_version": from_version, "hash": new_hash_hex,
            "actor": actor, "actor_email": actor_email})["rotated"])

    # Every child linked to this guardian, owing or not.
    SQL_PORTAL_CHILDREN = """
        select s.id, s.first_name, s.last_name, s.grade_level,
               coalesce(b.balance_due_cents, 0) as balance_due_cents,
               coalesce(b.credit_cents, 0)      as credit_cents
          from billing.guardian_students gs
          join public.students s on s.id = gs.student_id
          left join billing.v_student_balances b on b.student_id = s.id and b.institution_id = gs.institution_id
         where gs.guardian_id = %(gid)s and gs.institution_id = %(iid)s
         order by lower(s.first_name)
    """

    def portal_children(self, institution_id, guardian_id):
        return self.db.fetch_all(self.SQL_PORTAL_CHILDREN, {"iid": institution_id, "gid": guardian_id})

    # What a parent sees per day: pre-ordered, post-paid (with price/status), checked in without lunch.
    SQL_PORTAL_LUNCHES = """
        select cb.service_date, cb.classification, l.price_cents, l.rate_label, l.status, l.allocated_cents,
               l.price_locked, l.meal_cents, l.fee_cents
          from billing.check_in_billing cb
          left join billing.v_check_in_ledger l on l.check_in_id = cb.check_in_id
         where cb.institution_id = %(iid)s and cb.student_id = %(sid)s
           and cb.classification in ('pre_ordered', 'post_paid', 'no_lunch')
         order by cb.service_date desc
         limit 400
    """

    def portal_lunches(self, institution_id, student_id):
        return self.db.fetch_all(self.SQL_PORTAL_LUNCHES, {"iid": institution_id, "sid": student_id})

    SQL_PORTAL_PAYMENTS = """
        select p.amount_cents, p.method, p.received_at,
               exists (select 1 from billing.payment_reversals r where r.payment_id = p.id) as reversed
          from billing.payments p
         where p.institution_id = %(iid)s and p.student_id = %(sid)s
         order by p.received_at desc
    """

    def portal_payments(self, institution_id, student_id):
        return self.db.fetch_all(self.SQL_PORTAL_PAYMENTS, {"iid": institution_id, "sid": student_id})

    SQL_STATEMENT_GUARDIAN = """
        select id, name, email, receives_notices, token_version
          from billing.guardians where id = %(gid)s and institution_id = %(iid)s
    """

    def statement_guardian(self, institution_id, guardian_id):
        return self.db.fetch_one(self.SQL_STATEMENT_GUARDIAN, {"iid": institution_id, "gid": guardian_id})
