"""Phase 1 SQL: orders import, matching, classification, review, rates, waivers, offline payments.

Mixed into app.repo.Repo. Same rules as there: every SQL string is a class
constant, audit rows are written in the same statement as the change where
possible, and `pin` is never selected.
"""
import json


class BillingRepoMixin:
    # ================================================================ orders import
    SQL_IMPORT_ORDERS = """
        with batch as (
            insert into billing.import_batches (institution_id, filename, uploaded_by, exported_at, row_count)
            values (%(iid)s, %(filename)s, %(actor)s, %(exported_at)s, %(row_count)s)
            returning id
        ),
        ins as (
            insert into billing.imported_orders
                   (batch_id, institution_id, external_order_id, ordering_user_id, raw_user_name, name_key,
                    service_date, product_name, is_refunded, source_row_hash)
            select (select id from batch), %(iid)s, r.external_order_id, r.ordering_user_id, r.raw_user_name,
                   r.name_key, r.service_date, r.product_name, r.is_refunded, r.source_row_hash
              from jsonb_to_recordset(%(rows)s::jsonb) as r(
                   external_order_id text, ordering_user_id text, raw_user_name text, name_key text,
                   service_date date, product_name text, is_refunded boolean, source_row_hash text)
            on conflict (source_row_hash) do nothing
            returning 1
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'import_orders', 'import_batches', batch.id::text,
                   jsonb_build_object('filename', %(filename)s::text, 'rows', %(row_count)s::int,
                                      'new_rows', (select count(*) from ins))
              from batch
            returning id
        )
        select (select id from batch) as batch_id, (select count(*) from ins) as inserted
    """

    # A refund can arrive in a later export than the order it cancels.
    SQL_MARK_REFUNDED = """
        with upd as (
            update billing.imported_orders
               set is_refunded = true
             where institution_id = %(iid)s
               and not is_refunded
               and external_order_id in (select jsonb_array_elements_text(%(ids)s::jsonb))
            returning 1
        )
        select count(*) as marked from upd
    """

    def import_orders(self, institution_id, filename, exported_at, rows, actor, actor_email):
        res = self.db.fetch_one(self.SQL_IMPORT_ORDERS, {
            "iid": institution_id, "filename": filename, "exported_at": exported_at, "row_count": len(rows),
            "rows": json.dumps(rows), "actor": actor, "actor_email": actor_email,
        })
        refunded = sorted({r["external_order_id"] for r in rows if r["is_refunded"]})
        marked = self.db.fetch_one(self.SQL_MARK_REFUNDED, {"iid": institution_id, "ids": json.dumps(refunded)})
        return {"batch_id": res["batch_id"], "inserted": int(res["inserted"]), "refund_marked": int(marked["marked"])}

    SQL_RECENT_BATCHES = """
        select b.id, b.filename, b.uploaded_at, b.exported_at, b.row_count, s.email as uploaded_by_email,
               (select count(*) from billing.imported_orders o where o.batch_id = b.id) as new_rows
          from billing.import_batches b
          left join billing.staff_roles s on s.user_id = b.uploaded_by
         where b.institution_id = %(iid)s
         order by b.uploaded_at desc
         limit 20
    """

    def recent_batches(self, institution_id):
        return self.db.fetch_all(self.SQL_RECENT_BATCHES, {"iid": institution_id})

    # ================================================================ matching
    SQL_STUDENT_NAMES = "select id, first_name, last_name from public.students"

    def student_names(self):
        return self.db.fetch_all(self.SQL_STUDENT_NAMES)

    # keys: [{"key": "ava|lopez", "student_id": "...", "n": 1}, ...] (n = students sharing the key)
    SQL_MATCH_ORDERS = """
        with keys as (
            select k.key, k.student_id, k.n
              from jsonb_to_recordset(%(keys)s::jsonb) as k(key text, student_id uuid, n int)
        ),
        pairs as (
            select distinct o.ordering_user_id, o.name_key
              from billing.imported_orders o
             where o.institution_id = %(iid)s and o.student_id is null and o.name_key is not null
               and not exists (select 1 from billing.ordering_user_map m
                                where m.institution_id = o.institution_id
                                  and m.ordering_user_id = o.ordering_user_id and m.name_key = o.name_key)
        ),
        auto as (
            insert into billing.ordering_user_map (institution_id, ordering_user_id, name_key, student_id)
            select %(iid)s, p.ordering_user_id, p.name_key, k.student_id
              from pairs p join keys k on k.key = p.name_key and k.n = 1
            on conflict do nothing
            returning ordering_user_id, name_key, student_id
        ),
        maps as (
            select ordering_user_id, name_key, student_id from billing.ordering_user_map where institution_id = %(iid)s
            union all
            select ordering_user_id, name_key, student_id from auto
        ),
        upd as (
            update billing.imported_orders o
               set student_id = maps.student_id
              from maps
             where o.institution_id = %(iid)s and o.student_id is null
               and o.ordering_user_id = maps.ordering_user_id and o.name_key = maps.name_key
            returning 1
        ),
        unresolved as (
            select p.ordering_user_id, p.name_key,
                   (select count(*) from keys k where k.key = p.name_key) as candidates,
                   (select min(o.service_date) from billing.imported_orders o
                     where o.institution_id = %(iid)s and o.ordering_user_id = p.ordering_user_id
                       and o.name_key = p.name_key and not o.is_refunded and o.service_date >= %(start)s) as first_date
              from pairs p
             where not exists (select 1 from auto a where a.ordering_user_id = p.ordering_user_id and a.name_key = p.name_key)
        ),
        review as (
            insert into billing.review_items (institution_id, source, raw_reference, service_date, reason)
            select %(iid)s, 'order', u.ordering_user_id || '|' || u.name_key, u.first_date,
                   case when u.candidates = 0 then 'No student in the roster has this name'
                        else 'Name matches ' || u.candidates || ' students' end
              from unresolved u
             where u.first_date is not null          -- only names with billable-period orders need a person
            on conflict do nothing
            returning 1
        )
        select (select count(*) from auto)       as auto_mapped,
               (select count(*) from upd)        as orders_matched,
               (select count(*) from unresolved) as unresolved_pairs,
               (select count(*) from review)     as review_items_opened
    """

    def match_orders(self, institution_id, name_keys, start):
        """name_keys: {key: [student_id, ...]} built from the roster in Python (same normalizer as orders)."""
        keys = []
        for key, ids in name_keys.items():
            for sid in ids:
                keys.append({"key": key, "student_id": sid, "n": len(ids)})
        row = self.db.fetch_one(self.SQL_MATCH_ORDERS, {"iid": institution_id, "keys": json.dumps(keys), "start": start})
        return {k: int(v) for k, v in row.items()}

    # ================================================================ classification
    SQL_KNOWN_THROUGH = """
        select (max(exported_at) at time zone %(tz)s)::date as known_through
          from billing.import_batches where institution_id = %(iid)s
    """

    def orders_known_through(self, institution_id, tz):
        row = self.db.fetch_one(self.SQL_KNOWN_THROUGH, {"iid": institution_id, "tz": tz})
        return row["known_through"] if row else None

    SQL_START_RUN = """
        insert into billing.reconciliation_runs (institution_id, period_start, period_end, started_by)
        values (%(iid)s, %(start)s, %(end)s, %(actor)s)
        returning id
    """

    SQL_FINISH_RUN = """
        with upd as (
            update billing.reconciliation_runs
               set status = %(status)s, finished_at = now(), counts = %(counts)s::jsonb, error = %(error)s
             where id = %(run)s
            returning id, institution_id, started_by
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select upd.institution_id, upd.started_by, %(actor_email)s, 'classification_run', 'reconciliation_runs',
                   upd.id::text, %(counts)s::jsonb
              from upd
            returning id
        )
        select (select count(*) from audit) as audited
    """

    def start_run(self, institution_id, start, end, actor):
        return self.db.fetch_one(self.SQL_START_RUN, {"iid": institution_id, "start": start, "end": end,
                                                      "actor": actor})["id"]

    def finish_run(self, run_id, status, counts, error, actor_email):
        self.db.fetch_one(self.SQL_FINISH_RUN, {"run": run_id, "status": status, "counts": json.dumps(counts),
                                                "error": error, "actor_email": actor_email})

    # Check-ins not yet classified, in the billable window, oldest first.
    SQL_UNCLASSIFIED_CHECK_INS = """
        select c.id, c.student_id, c.check_in_date, c.getting_lunch, c.bill_separately,
               (s.id is not null) as known_student
          from public.check_ins c
          left join public.students s on s.id = c.student_id
         where c.check_in_date >= %(start)s and c.check_in_date < %(end)s
           and not exists (select 1 from billing.check_in_billing b where b.check_in_id = c.id)
         order by c.check_in_date, c.check_in_time nulls last, c.id
    """

    SQL_POST_PAID_DAYS = """
        select student_id, service_date from billing.check_in_billing
         where institution_id = %(iid)s and classification = 'post_paid'
           and service_date >= %(start)s and service_date < %(end)s
    """

    SQL_ORDER_DAYS = """
        select distinct student_id, service_date from billing.imported_orders
         where institution_id = %(iid)s and not is_refunded and student_id is not null
           and service_date >= %(start)s and service_date < %(end)s
    """

    def classification_inputs(self, institution_id, start, end):
        p = {"iid": institution_id, "start": start, "end": end}
        return (self.db.fetch_all(self.SQL_UNCLASSIFIED_CHECK_INS, p),
                self.db.fetch_all(self.SQL_POST_PAID_DAYS, p),
                self.db.fetch_all(self.SQL_ORDER_DAYS, p))

    SQL_INSERT_CLASSIFIED = """
        with ins as (
            insert into billing.check_in_billing
                   (check_in_id, institution_id, student_id, service_date, classification, classification_note, run_id)
            select r.check_in_id, %(iid)s, r.student_id, r.service_date, r.classification, r.note, %(run)s
              from jsonb_to_recordset(%(rows)s::jsonb) as r(
                   check_in_id uuid, student_id uuid, service_date date, classification text, note text)
            on conflict (check_in_id) do nothing
            returning classification
        )
        select classification, count(*) as n from ins group by classification
    """

    def insert_classified(self, institution_id, run_id, rows):
        if not rows:
            return {}
        out = self.db.fetch_all(self.SQL_INSERT_CLASSIFIED, {"iid": institution_id, "run": run_id,
                                                             "rows": json.dumps(rows)})
        return {r["classification"]: int(r["n"]) for r in out}

    SQL_UNKNOWN_STUDENT_REVIEW = """
        with ins as (
            insert into billing.review_items (institution_id, run_id, source, raw_reference, service_date, reason)
            select %(iid)s, %(run)s, 'check_in', r.student_id, r.service_date,
                   'Check-in is for a student_id that is not in the students table'
              from jsonb_to_recordset(%(rows)s::jsonb) as r(student_id text, service_date date)
            on conflict do nothing
            returning 1
        )
        select count(*) as n from ins
    """

    def review_unknown_students(self, institution_id, run_id, rows):
        if not rows:
            return 0
        return int(self.db.fetch_one(self.SQL_UNKNOWN_STUDENT_REVIEW, {
            "iid": institution_id, "run": run_id, "rows": json.dumps(rows)})["n"])

    # Orders that arrive, or get refunded, after a day was classified.
    SQL_RECONCILE_LATE_ORDERS = """
        with has_order as (
            select cb.check_in_id, cb.student_id, cb.service_date, cb.classification, cb.locked_at, cb.waived_at,
                   exists (select 1 from billing.imported_orders o
                            where o.institution_id = cb.institution_id and o.student_id = cb.student_id
                              and o.service_date = cb.service_date and not o.is_refunded) as ordered
              from billing.check_in_billing cb
             where cb.institution_id = %(iid)s and cb.service_date >= %(start)s
               and cb.classification in ('post_paid', 'pre_ordered')
        ),
        to_pre as (
            update billing.check_in_billing cb
               set classification = 'pre_ordered', classification_note = 'Order found in a later import'
              from has_order h
             where cb.check_in_id = h.check_in_id and h.classification = 'post_paid' and h.ordered
               and h.locked_at is null and h.waived_at is null
            returning cb.check_in_id
        ),
        to_post as (
            -- at most one per child per day, and only if that day has no billed lunch yet
            update billing.check_in_billing cb
               set classification = 'post_paid', classification_note = 'Order was refunded in a later import'
              from (select distinct on (student_id, service_date) check_in_id
                      from has_order
                     where classification = 'pre_ordered' and not ordered
                     order by student_id, service_date, check_in_id) h
             where cb.check_in_id = h.check_in_id
               and not exists (select 1 from billing.check_in_billing o
                                where o.student_id = cb.student_id and o.service_date = cb.service_date
                                  and o.classification = 'post_paid')
            returning cb.check_in_id
        ),
        flagged as (
            insert into billing.review_items (institution_id, run_id, source, raw_reference, service_date, reason)
            select %(iid)s, %(run)s, 'late_order', h.check_in_id::text, h.service_date,
                   'Billed as post-paid and money was applied, but an order for this day is now on file'
              from has_order h
             where h.classification = 'post_paid' and h.ordered and h.locked_at is not null and h.waived_at is null
            on conflict do nothing
            returning 1
        )
        select (select count(*) from to_pre)  as to_pre_ordered,
               (select count(*) from to_post) as to_post_paid,
               (select count(*) from flagged) as flagged
    """

    def reconcile_late_orders(self, institution_id, run_id, start):
        row = self.db.fetch_one(self.SQL_RECONCILE_LATE_ORDERS, {"iid": institution_id, "run": run_id, "start": start})
        return {k: int(v) for k, v in row.items()}

    SQL_APPLY_ALL_CREDIT = """
        select coalesce(sum(billing.apply_credit(student_id)), 0) as applied
          from billing.v_student_balances
         where institution_id = %(iid)s and credit_cents > 0 and open_cents > 0
    """

    def apply_all_credit(self, institution_id):
        return int(self.db.fetch_one(self.SQL_APPLY_ALL_CREDIT, {"iid": institution_id})["applied"])

    SQL_LAST_RUN = """
        select id, status, started_at, finished_at, period_start, period_end, counts, error
          from billing.reconciliation_runs
         where institution_id = %(iid)s
         order by started_at desc limit 1
    """

    def last_run(self, institution_id):
        return self.db.fetch_one(self.SQL_LAST_RUN, {"iid": institution_id})

    # ================================================================ review queue
    SQL_OPEN_REVIEW = """
        select r.id, r.source, r.raw_reference, r.service_date, r.reason, r.created_at,
               split_part(r.raw_reference, '|', 1) as ordering_user_id,
               (select o.raw_user_name from billing.imported_orders o
                 where r.source = 'order' and o.institution_id = r.institution_id
                   and o.ordering_user_id || '|' || o.name_key = r.raw_reference
                 order by o.service_date desc limit 1) as sample_name,
               (select count(*) from billing.imported_orders o
                 where r.source = 'order' and o.institution_id = r.institution_id
                   and o.ordering_user_id || '|' || o.name_key = r.raw_reference and not o.is_refunded) as order_count
          from billing.review_items r
         where r.institution_id = %(iid)s and r.status = 'open'
         order by r.source, r.service_date nulls last, r.created_at
    """

    def open_review_items(self, institution_id):
        return self.db.fetch_all(self.SQL_OPEN_REVIEW, {"iid": institution_id})

    SQL_RESOLVE_ORDER_ITEM = """
        with item as (
            select id, raw_reference from billing.review_items
             where id = %(item)s and institution_id = %(iid)s and status = 'open' and source = 'order'
        ),
        pair as (
            select split_part(raw_reference, '|', 1) as uid,
                   substr(raw_reference, length(split_part(raw_reference, '|', 1)) + 2) as key
              from item
        ),
        m as (
            insert into billing.ordering_user_map (institution_id, ordering_user_id, name_key, student_id, mapped_by)
            select %(iid)s, pair.uid, pair.key, %(sid)s, %(actor)s from pair
            on conflict (institution_id, ordering_user_id, name_key) do nothing
            returning ordering_user_id, name_key
        ),
        upd as (
            update billing.imported_orders o set student_id = %(sid)s
              from pair
             where o.institution_id = %(iid)s and o.ordering_user_id = pair.uid and o.name_key = pair.key
               and o.student_id is null
            returning 1
        ),
        res as (
            update billing.review_items set status = 'resolved', resolved_student_id = %(sid)s,
                   resolved_by = %(actor)s, resolved_at = now()
             where id = (select id from item)
            returning id
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'resolve_review_match', 'review_items', res.id::text,
                   jsonb_build_object('student_id', %(sid)s::text, 'orders_matched', (select count(*) from upd))
              from res
            returning id
        )
        select (select count(*) from res) as resolved, (select count(*) from upd) as orders_matched
    """

    def resolve_order_item(self, institution_id, item_id, student_id, actor, actor_email):
        row = self.db.fetch_one(self.SQL_RESOLVE_ORDER_ITEM, {
            "iid": institution_id, "item": item_id, "sid": student_id, "actor": actor, "actor_email": actor_email})
        return {k: int(v) for k, v in row.items()}

    SQL_DISMISS_ITEM = """
        with res as (
            update billing.review_items set status = 'dismissed', resolved_by = %(actor)s, resolved_at = now()
             where id = %(item)s and institution_id = %(iid)s and status = 'open'
            returning id, source, raw_reference
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'dismiss_review_item', 'review_items', res.id::text,
                   jsonb_build_object('source', res.source, 'reference', res.raw_reference, 'note', %(note)s::text)
              from res
            returning id
        )
        select count(*) as dismissed from res
    """

    def dismiss_review_item(self, institution_id, item_id, note, actor, actor_email):
        return int(self.db.fetch_one(self.SQL_DISMISS_ITEM, {
            "iid": institution_id, "item": item_id, "note": note, "actor": actor, "actor_email": actor_email})["dismissed"])

    SQL_STUDENT_PICKLIST = """
        select id, first_name, last_name, grade_level from public.students
         order by lower(last_name), lower(first_name)
    """

    def student_picklist(self):
        return self.db.fetch_all(self.SQL_STUDENT_PICKLIST)

    # ================================================================ rates
    SQL_RATES = """
        select r.id, r.starts_on, r.ends_on, r.price_cents, r.fee_cents, r.label, r.created_at,
               (select count(*) from billing.check_in_billing cb
                 where cb.institution_id = r.institution_id and cb.classification = 'post_paid'
                   and cb.service_date between r.starts_on and r.ends_on) as lunches,
               (select count(*) from billing.check_in_billing cb where cb.locked_rate_period_id = r.id) as locked_lunches
          from billing.rate_periods r
         where r.institution_id = %(iid)s
         order by r.starts_on
    """

    def rate_periods(self, institution_id):
        return self.db.fetch_all(self.SQL_RATES, {"iid": institution_id})

    # How many unpaid lunches a default-price change would touch (for the confirmation message).
    SQL_DEFAULT_PRICE_IMPACT = """
        select count(*) as lunches, count(distinct student_id) as students
          from billing.v_check_in_ledger
         where institution_id = %(iid)s and price_source = 'default' and status in ('open', 'partial')
    """

    SQL_SET_DEFAULT_PRICE = """
        with before as (select id, default_price_cents from billing.institutions where id = %(iid)s),
        upd as (
            update billing.institutions set default_price_cents = %(price)s where id = %(iid)s
            returning id, default_price_cents
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, before, after)
            select upd.id, %(actor)s, %(actor_email)s, 'set_default_price', 'institutions', upd.id::text,
                   jsonb_build_object('default_price_cents', before.default_price_cents),
                   jsonb_build_object('default_price_cents', upd.default_price_cents)
              from upd join before using (id)
            returning id
        )
        select (select default_price_cents from before) as old_cents, upd.default_price_cents as new_cents from upd
    """

    def set_default_price(self, institution_id, price_cents, actor, actor_email):
        impact = self.db.fetch_one(self.SQL_DEFAULT_PRICE_IMPACT, {"iid": institution_id})
        row = self.db.fetch_one(self.SQL_SET_DEFAULT_PRICE, {"iid": institution_id, "price": price_cents,
                                                             "actor": actor, "actor_email": actor_email})
        return {"old_cents": int(row["old_cents"]), "new_cents": int(row["new_cents"]),
                "lunches": int(impact["lunches"]), "students": int(impact["students"])}

    SQL_OVERLAPPING_PERIOD = """
        select starts_on, ends_on from billing.rate_periods
         where institution_id = %(iid)s and daterange(starts_on, ends_on, '[]') && daterange(%(s)s::date, %(e)s::date, '[]')
         limit 1
    """

    SQL_ADD_PERIOD = """
        with ins as (
            insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents, fee_cents, label, created_by)
            values (%(iid)s, %(s)s, %(e)s, %(price)s, %(fee)s, %(label)s, %(actor)s)
            returning id, starts_on, ends_on, price_cents, fee_cents, label
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'add_rate_period', 'rate_periods', ins.id::text, to_jsonb(ins)
              from ins
            returning id
        )
        select ins.id,
               (select count(*) from billing.check_in_billing cb
                 where cb.institution_id = %(iid)s and cb.classification = 'post_paid' and cb.locked_at is null
                   and cb.waived_at is null and cb.service_date between %(s)s and %(e)s) as unpaid_lunches_affected
          from ins
    """

    def find_overlapping_period(self, institution_id, starts_on, ends_on):
        return self.db.fetch_one(self.SQL_OVERLAPPING_PERIOD, {"iid": institution_id, "s": starts_on, "e": ends_on})

    def add_rate_period(self, institution_id, starts_on, ends_on, price_cents, label, actor, actor_email, fee_cents=None):
        return self.db.fetch_one(self.SQL_ADD_PERIOD, {
            "iid": institution_id, "s": starts_on, "e": ends_on, "price": price_cents, "fee": fee_cents, "label": label,
            "actor": actor, "actor_email": actor_email})

    SQL_DELETE_PERIOD = """
        with del as (
            delete from billing.rate_periods r
             where r.id = %(pid)s and r.institution_id = %(iid)s
               and not exists (select 1 from billing.check_in_billing cb where cb.locked_rate_period_id = r.id)
            returning id, starts_on, ends_on, price_cents, fee_cents, label
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, before)
            select %(iid)s, %(actor)s, %(actor_email)s, 'delete_rate_period', 'rate_periods', del.id::text, to_jsonb(del)
              from del
            returning id
        )
        select count(*) as deleted from del
    """

    def delete_rate_period(self, institution_id, period_id, actor, actor_email):
        return int(self.db.fetch_one(self.SQL_DELETE_PERIOD, {
            "iid": institution_id, "pid": period_id, "actor": actor, "actor_email": actor_email})["deleted"])

    # ================================================================ student history, waivers, payments
    SQL_STUDENT_LUNCHES = """
        select cb.check_in_id, cb.service_date, cb.classification, cb.classification_note,
               l.price_cents, l.rate_label, l.price_locked, l.allocated_cents, l.open_cents, l.status,
               cb.waive_reason, l.meal_cents, l.fee_cents
          from billing.check_in_billing cb
          left join billing.v_check_in_ledger l on l.check_in_id = cb.check_in_id
         where cb.institution_id = %(iid)s and cb.student_id = %(sid)s
         order by cb.service_date desc
    """

    def student_lunches(self, institution_id, student_id):
        return self.db.fetch_all(self.SQL_STUDENT_LUNCHES, {"iid": institution_id, "sid": student_id})

    SQL_STUDENT_PAYMENTS = """
        select p.id, p.amount_cents, p.method, p.received_at, p.note, s.email as recorded_by_email,
               (select r.reason from billing.payment_reversals r where r.payment_id = p.id) as reversed_reason
          from billing.payments p
          left join billing.staff_roles s on s.user_id = p.recorded_by
         where p.institution_id = %(iid)s and p.student_id = %(sid)s
         order by p.received_at desc
    """

    def student_payments(self, institution_id, student_id):
        return self.db.fetch_all(self.SQL_STUDENT_PAYMENTS, {"iid": institution_id, "sid": student_id})

    SQL_WAIVE = """
        with before as (
            select allocated_cents from billing.v_check_in_ledger where check_in_id = %(cid)s
        ),
        upd as (
            update billing.check_in_billing
               set waived_at = now(), waived_by = %(actor)s, waive_reason = %(reason)s
             where check_in_id = %(cid)s and institution_id = %(iid)s and student_id = %(sid)s
               and classification = 'post_paid' and waived_at is null
            returning check_in_id, service_date
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'waive_lunch', 'check_in_billing', upd.check_in_id::text,
                   jsonb_build_object('service_date', upd.service_date, 'reason', %(reason)s::text)
              from upd
            returning id
        )
        select (select count(*) from upd) as waived,
               coalesce((select allocated_cents from before), 0) as released_cents
    """

    SQL_APPLY_CREDIT = "select billing.apply_credit(%(sid)s::uuid) as applied"

    def waive_lunch(self, institution_id, student_id, check_in_id, reason, actor, actor_email):
        row = self.db.fetch_one(self.SQL_WAIVE, {
            "iid": institution_id, "sid": student_id, "cid": check_in_id, "reason": reason,
            "actor": actor, "actor_email": actor_email})
        waived = int(row["waived"])
        released = int(row["released_cents"]) if waived else 0
        applied = int(self.db.fetch_one(self.SQL_APPLY_CREDIT, {"sid": student_id})["applied"]) if waived else 0
        return {"waived": waived, "released_cents": released, "reapplied_cents": applied}

    SQL_RECORD_PAYMENT = """
        with pay as (
            select * from billing.record_offline_payment(%(iid)s, %(sid)s, %(amount)s, %(method)s,
                                                         %(received)s, %(actor)s, %(note)s)
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'record_payment', 'payments', pay.payment_id::text,
                   jsonb_build_object('student_id', %(sid)s::text, 'amount_cents', %(amount)s::int,
                                      'method', %(method)s::text, 'applied_cents', pay.applied_cents)
              from pay
            returning id
        )
        select pay.payment_id, pay.applied_cents from pay
    """

    def record_offline_payment(self, institution_id, student_id, amount_cents, method, received_at, note,
                               actor, actor_email):
        row = self.db.fetch_one(self.SQL_RECORD_PAYMENT, {
            "iid": institution_id, "sid": student_id, "amount": amount_cents, "method": method,
            "received": received_at, "note": note, "actor": actor, "actor_email": actor_email})
        return {"payment_id": row["payment_id"], "applied_cents": int(row["applied_cents"])}

    # A mistaken or bounced payment is reversed, never deleted: its allocations stop
    # counting, the lunches it paid reopen, and the reversal is on record.
    SQL_REVERSE_PAYMENT = """
        with rev as (
            insert into billing.payment_reversals (payment_id, reason, recorded_by)
            select p.id, %(reason)s, %(actor)s
              from billing.payments p
             where p.id = %(pid)s and p.institution_id = %(iid)s and p.student_id = %(sid)s
               and p.processor_ref is null            -- Stripe payments are reversed through Stripe (phase 3)
            on conflict (payment_id) do nothing
            returning payment_id
        ),
        audit as (
            insert into billing.audit_log (institution_id, actor, actor_email, action, entity, entity_id, after)
            select %(iid)s, %(actor)s, %(actor_email)s, 'reverse_payment', 'payments', rev.payment_id::text,
                   jsonb_build_object('reason', %(reason)s::text)
              from rev
            returning id
        )
        select count(*) as reversed from rev
    """

    def reverse_payment(self, institution_id, student_id, payment_id, reason, actor, actor_email):
        n = int(self.db.fetch_one(self.SQL_REVERSE_PAYMENT, {
            "iid": institution_id, "sid": student_id, "pid": payment_id, "reason": reason,
            "actor": actor, "actor_email": actor_email})["reversed"])
        # other credit (if any) may now cover the reopened lunches
        applied = int(self.db.fetch_one(self.SQL_APPLY_CREDIT, {"sid": student_id})["applied"]) if n else 0
        return {"reversed": n, "reapplied_cents": applied}

    # ================================================================ parent payment amounts
    # Parents pay for whole lunches, oldest first. Credit is applied first so the
    # options always reflect what is really still owed.
    SQL_PAYMENT_OPTIONS = """
        select lunches, service_date, rate_label, open_cents, amount_cents
          from billing.v_payment_options
         where institution_id = %(iid)s and student_id = %(sid)s
         order by lunches
    """

    def payment_options(self, institution_id, student_id):
        self.db.fetch_one(self.SQL_APPLY_CREDIT, {"sid": student_id})
        return self.db.fetch_all(self.SQL_PAYMENT_OPTIONS, {"iid": institution_id, "sid": student_id})

    # ================================================================ v1.4 comparison
    SQL_POST_PAID_IN_RANGE = """
        select l.student_id, l.service_date, l.price_cents
          from billing.v_check_in_ledger l
         where l.institution_id = %(iid)s and l.service_date between %(s)s and %(e)s
    """

    def post_paid_in_range(self, institution_id, start, end):
        return self.db.fetch_all(self.SQL_POST_PAID_IN_RANGE, {"iid": institution_id, "s": start, "e": end})

    SQL_CLASSIFICATIONS_FOR = """
        select cb.student_id, cb.service_date, cb.classification, cb.classification_note
          from billing.check_in_billing cb
         where cb.institution_id = %(iid)s and cb.service_date between %(s)s and %(e)s
    """

    def classifications_in_range(self, institution_id, start, end):
        return self.db.fetch_all(self.SQL_CLASSIFICATIONS_FOR, {"iid": institution_id, "s": start, "e": end})
