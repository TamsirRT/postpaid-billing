"""Phase 3 SQL: the fee line, Stripe payment intents, webhook bookkeeping. Mixed into Repo.

Every write here is safe to repeat: Stripe retries webhooks, and a retry must
never record a payment twice or reverse one twice.
"""
import uuid


class PayRepoMixin:
    # ================================================================ fee line
    SQL_DEFAULT_FEE_IMPACT = """
        select count(*) as lunches, count(distinct l.student_id) as students
          from billing.v_check_in_ledger l
          left join billing.rate_periods r on r.id = l.rate_period_id
         where l.institution_id = %(iid)s and l.status in ('open', 'partial') and not l.price_locked
           and r.fee_cents is null
    """

    SQL_SET_DEFAULT_FEE = """
        with before as (select id, default_fee_cents from billing.institutions where id = %(iid)s),
        upd as (
            update billing.institutions set default_fee_cents = %(fee)s where id = %(iid)s
            returning id, default_fee_cents
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, before, after)
            select upd.id, %(actor)s, %(actor_email)s, 'set_default_fee', 'institutions', upd.id::text,
                   jsonb_build_object('default_fee_cents', before.default_fee_cents),
                   jsonb_build_object('default_fee_cents', upd.default_fee_cents)
              from upd join before using (id)
            returning id
        )
        select (select default_fee_cents from before) as old_cents, upd.default_fee_cents as new_cents from upd
    """

    def set_default_fee(self, institution_id, fee_cents, actor, actor_email):
        impact = self.db.fetch_one(self.SQL_DEFAULT_FEE_IMPACT, {"iid": institution_id})
        row = self.db.fetch_one(self.SQL_SET_DEFAULT_FEE, {"iid": institution_id, "fee": fee_cents,
                                                           "actor": actor, "actor_email": actor_email})
        return {"old_cents": int(row["old_cents"]), "new_cents": int(row["new_cents"]),
                "lunches": int(impact["lunches"]), "students": int(impact["students"])}

    # ================================================================ starting a payment
    SQL_CREATE_INTENT = """
        insert into billing.payment_intents (institution_id, student_id, guardian_id, amount_cents, lunches,
                                             processor_ref, status)
        values (%(iid)s, %(sid)s, %(gid)s, %(amount)s, %(lunches)s, %(ref)s, 'pending')
        returning id
    """

    def create_payment_intent(self, institution_id, student_id, guardian_id, amount_cents, lunches):
        """The database refuses any amount that isn't a whole number of lunches, oldest first."""
        return str(self.db.fetch_one(self.SQL_CREATE_INTENT, {
            "iid": institution_id, "sid": student_id, "gid": guardian_id, "amount": amount_cents,
            "lunches": lunches, "ref": f"new:{uuid.uuid4()}"})["id"])

    SQL_ATTACH_CHECKOUT = """
        update billing.payment_intents set processor_ref = %(sess)s, checkout_url = %(url)s, updated_at = now()
         where id = %(id)s and status = 'pending'
        returning id
    """

    def attach_checkout(self, intent_id, session_id, url):
        self.db.fetch_all(self.SQL_ATTACH_CHECKOUT, {"id": intent_id, "sess": session_id, "url": url})

    SQL_INTENT = """
        select i.id, i.institution_id, i.student_id, i.guardian_id, i.amount_cents, i.lunches, i.status,
               i.processor_ref, i.stripe_payment_intent, i.payment_id, i.method, i.created_at
          from billing.payment_intents i where i.id = %(id)s
    """

    def get_payment_intent(self, intent_id):
        try:
            uuid.UUID(str(intent_id))
        except ValueError:
            return None
        return self.db.fetch_one(self.SQL_INTENT, {"id": str(intent_id)})

    SQL_SET_INTENT_STATUS = """
        update billing.payment_intents
           set status = %(status)s, failure_reason = %(reason)s,
               stripe_payment_intent = coalesce(%(pi)s, stripe_payment_intent),
               method = coalesce(%(method)s, method), updated_at = now()
         where id = %(id)s and status = any(%(from)s::text[])
        returning id
    """

    def set_intent_status(self, intent_id, status, from_statuses, reason=None, stripe_pi=None, method=None):
        """Moves an intent forward only from the given statuses (a late or repeated event changes nothing)."""
        rows = self.db.fetch_all(self.SQL_SET_INTENT_STATUS, {
            "id": intent_id, "status": status, "from": "{" + ",".join(from_statuses) + "}", "reason": reason,
            "pi": stripe_pi, "method": method})
        return len(rows)

    SQL_PROCESSING_FOR = """
        select id, amount_cents, lunches, created_at
          from billing.payment_intents
         where institution_id = %(iid)s and student_id = %(sid)s and status = 'processing'
         order by created_at
    """

    def processing_intents(self, institution_id, student_id):
        return self.db.fetch_all(self.SQL_PROCESSING_FOR, {"iid": institution_id, "sid": student_id})

    # ================================================================ settling
    SQL_RECORD_STRIPE = """
        select payment_id, created, applied_cents
          from billing.record_stripe_payment(%(id)s::uuid, %(pi)s, %(method)s, %(amount)s::int)
    """

    def record_stripe_payment(self, intent_id, stripe_pi, method, amount_cents):
        row = self.db.fetch_one(self.SQL_RECORD_STRIPE, {"id": intent_id, "pi": stripe_pi, "method": method,
                                                         "amount": amount_cents})
        return {"payment_id": str(row["payment_id"]), "created": row["created"] in (True, "t", "true"),
                "applied_cents": int(row["applied_cents"])}

    # ================================================================ refunds and disputes
    SQL_PAYMENT_BY_REF = """
        select p.id, p.institution_id, p.student_id, p.amount_cents,
               exists (select 1 from billing.payment_reversals r where r.payment_id = p.id) as reversed
          from billing.payments p where p.processor_ref = %(pi)s
    """

    def payment_by_processor_ref(self, stripe_pi):
        return self.db.fetch_one(self.SQL_PAYMENT_BY_REF, {"pi": stripe_pi})

    SQL_REVERSE_STRIPE = """
        with rev as (
            insert into billing.payment_reversals (payment_id, reason, processor_ref)
            select p.id, %(reason)s, %(ref)s from billing.payments p where p.id = %(pid)s and p.processor_ref is not null
            on conflict (payment_id) do nothing
            returning payment_id
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select p.institution_id, null, 'stripe', 'reverse_payment', 'payments', rev.payment_id::text,
                   jsonb_build_object('reason', %(reason)s::text, 'stripe_ref', %(ref)s::text)
              from rev join billing.payments p on p.id = rev.payment_id
            returning id
        )
        select count(*) as reversed from rev
    """

    def reverse_stripe_payment(self, payment, reason, stripe_ref):
        n = int(self.db.fetch_one(self.SQL_REVERSE_STRIPE, {"pid": str(payment["id"]), "reason": reason,
                                                            "ref": stripe_ref})["reversed"])
        if n:   # other credit (if any) may cover the reopened lunches
            self.db.fetch_one("select billing.apply_credit(%(sid)s::uuid) as applied", {"sid": payment["student_id"]})
        return n

    SQL_NOTE_ON_INTENT = """
        update billing.payment_intents
           set refunded_cents = greatest(refunded_cents, coalesce(%(refunded)s, 0)),
               disputed_at = case when %(disputed)s then coalesce(disputed_at, now()) else disputed_at end,
               needs_attention = coalesce(%(attention)s, needs_attention), updated_at = now()
         where stripe_payment_intent = %(pi)s
        returning id
    """

    def note_on_intent(self, stripe_pi, refunded_cents=None, disputed=False, attention=None):
        self.db.fetch_all(self.SQL_NOTE_ON_INTENT, {"pi": stripe_pi, "refunded": refunded_cents,
                                                    "disputed": bool(disputed), "attention": attention})

    SQL_CLEAR_ATTENTION = """
        with upd as (
            update billing.payment_intents set needs_attention = null, updated_at = now()
             where id = %(id)s and institution_id = %(iid)s and needs_attention is not null
            returning id, needs_attention
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id)
            select %(iid)s, %(actor)s, %(actor_email)s, 'clear_payment_attention', 'payment_intents', upd.id::text
              from upd
            returning id
        )
        select count(*) as cleared from upd
    """

    def clear_intent_attention(self, institution_id, intent_id, actor, actor_email):
        return int(self.db.fetch_one(self.SQL_CLEAR_ATTENTION, {"iid": institution_id, "id": intent_id,
                                                                "actor": actor, "actor_email": actor_email})["cleared"])

    # ================================================================ webhook log
    def stripe_event_seen(self, event_id):
        return self.db.fetch_one("select 1 as seen from billing.stripe_events where id = %(id)s", {"id": event_id}) is not None

    def log_stripe_event(self, event_id, event_type, outcome):
        self.db.fetch_all("insert into billing.stripe_events (id, type, outcome) values (%(id)s, %(t)s, %(o)s) "
                          "on conflict (id) do nothing returning id", {"id": event_id, "t": event_type, "o": outcome[:500]})

    # ================================================================ staff view
    SQL_ONLINE_PAYMENTS = """
        select i.id, i.student_id, s.first_name, s.last_name, g.name as guardian_name, i.amount_cents, i.lunches,
               i.method, i.status, i.failure_reason, i.stripe_payment_intent, i.refunded_cents, i.disputed_at,
               i.needs_attention, i.created_at, i.updated_at,
               exists (select 1 from billing.payment_reversals r where r.payment_id = i.payment_id) as reversed
          from billing.payment_intents i
          join public.students s on s.id = i.student_id
          left join billing.guardians g on g.id = i.guardian_id
         where i.institution_id = %(iid)s
           and (i.status <> 'pending' or i.created_at > now() - interval '2 days')
         order by (i.needs_attention is not null) desc, i.created_at desc
         limit %(limit)s
    """

    def online_payments(self, institution_id, limit=300):
        return self.db.fetch_all(self.SQL_ONLINE_PAYMENTS, {"iid": institution_id, "limit": limit})
