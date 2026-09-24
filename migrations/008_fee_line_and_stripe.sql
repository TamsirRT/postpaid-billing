-- 008_fee_line_and_stripe.sql
--
-- 1. Fee line. A lunch's price = meal price + payment-processing fee, added on
--    top and shown to parents as its own line. The fee is part of the price for
--    every lunch however it is paid (cash and check included); it is NOT a
--    surcharge on online payments. The school has a default fee (starts at $0);
--    a special-price period can set its own fee or leave it NULL to use the
--    default. When money lands on a lunch, the fee locks together with the price.
--
-- 2. Stripe. Every webhook event is recorded once (retries are harmless), and a
--    payment intent remembers its Checkout Session and PaymentIntent ids, how
--    many lunches it was for, and when it was refunded or disputed.

-- ---------------------------------------------------------------- fee line
alter table billing.institutions
    add column default_fee_cents integer not null default 0 check (default_fee_cents between 0 and 10000);

alter table billing.rate_periods
    add column fee_cents integer check (fee_cents between 0 and 10000);   -- NULL = use the school's default fee

alter table billing.check_in_billing
    add column locked_fee_cents integer check (locked_fee_cents >= 0),
    add constraint check_in_billing_fee_within_price check (locked_fee_cents is null or locked_fee_cents <= locked_price_cents),
    add constraint check_in_billing_fee_only_when_locked check (locked_fee_cents is null or locked_at is not null);

-- Existing columns keep their order and meaning (price_cents = what the lunch
-- costs in total); meal_cents and fee_cents are appended.
create or replace view billing.v_check_in_ledger as
select
    cb.check_in_id,
    cb.institution_id,
    cb.student_id,
    cb.service_date,
    cb.locked_at,
    cb.waived_at,
    cb.waive_reason,
    coalesce(cb.locked_price_cents, cur.meal_cents + cur.fee_cents)             as price_cents,
    coalesce(cb.locked_rate_period_id, live.id)                                 as rate_period_id,
    coalesce(rp.label, case when rp.id is not null then 'Special rate' else 'Standard rate' end) as rate_label,
    (cb.locked_at is not null)                                                  as price_locked,
    coalesce(alloc.allocated_cents, 0)::bigint                                  as allocated_cents,
    case
        when cb.waived_at is not null                                                        then 'waived'
        when coalesce(cb.locked_price_cents, cur.meal_cents + cur.fee_cents) is null         then 'needs_rate'
        when coalesce(alloc.allocated_cents, 0)
             >= coalesce(cb.locked_price_cents, cur.meal_cents + cur.fee_cents)              then 'paid'
        when coalesce(alloc.allocated_cents, 0) > 0                                          then 'partial'
        else 'open'
    end                                                                         as status,
    case
        when cb.waived_at is not null                                                        then 0
        when coalesce(cb.locked_price_cents, cur.meal_cents + cur.fee_cents) is null         then 0
        else greatest(coalesce(cb.locked_price_cents, cur.meal_cents + cur.fee_cents)
                      - coalesce(alloc.allocated_cents, 0), 0)
    end::bigint                                                                 as open_cents,
    case when cb.locked_at is not null then 'locked'
         when live.id is not null      then 'rate_period'
         else 'default' end                                                     as price_source,
    -- the two parts of price_cents
    case when cb.locked_at is not null then cb.locked_price_cents - coalesce(cb.locked_fee_cents, 0)
         else cur.meal_cents end                                                as meal_cents,
    case when cb.locked_at is not null then coalesce(cb.locked_fee_cents, 0)
         else cur.fee_cents end                                                 as fee_cents
from billing.check_in_billing cb
join billing.institutions i on i.id = cb.institution_id
left join lateral (
    select r.id, r.price_cents, r.fee_cents
      from billing.rate_periods r
     where r.institution_id = cb.institution_id
       and cb.service_date between r.starts_on and r.ends_on
) live on cb.locked_at is null
cross join lateral (
    select coalesce(live.price_cents, i.default_price_cents)  as meal_cents,
           coalesce(live.fee_cents, i.default_fee_cents)      as fee_cents
) cur
left join billing.rate_periods rp
       on rp.id = coalesce(cb.locked_rate_period_id, live.id)
left join lateral (
    select sum(a.amount_cents) as allocated_cents
      from billing.payment_allocations a
     where a.check_in_id = cb.check_in_id
       and not exists (select 1 from billing.payment_reversals r where r.payment_id = a.payment_id)
) alloc on true
where cb.classification = 'post_paid';

-- The fee locks with the price.
create or replace function billing.protect_locked_price() returns trigger
language plpgsql as $$
begin
    if old.locked_at is not null and (
           new.locked_price_cents is distinct from old.locked_price_cents
        or new.locked_fee_cents is distinct from old.locked_fee_cents
        or new.locked_rate_period_id is distinct from old.locked_rate_period_id
        or new.locked_at is distinct from old.locked_at) then
        raise exception 'price of check-in % is locked', old.check_in_id using errcode = 'restrict_violation';
    end if;
    if old.locked_at is not null and new.classification <> old.classification then
        raise exception 'check-in % has money applied; reclassify through review', old.check_in_id using errcode = 'restrict_violation';
    end if;
    return new;
end;
$$;

-- Same as 005, plus locking the fee.
create or replace function billing.apply_credit(p_student uuid) returns bigint
language plpgsql as $$
declare
    pay        record;
    lunch      record;
    remaining  bigint;
    take       bigint;
    total      bigint := 0;
begin
    perform pg_advisory_xact_lock(hashtextextended('billing.apply_credit:' || p_student::text, 0));

    for pay in
        select p.id,
               p.amount_cents - coalesce((
                   select sum(a.amount_cents)
                     from billing.payment_allocations a
                     join billing.check_in_billing cb on cb.check_in_id = a.check_in_id
                    where a.payment_id = p.id and cb.waived_at is null), 0) as left_cents
          from billing.payments p
         where p.student_id = p_student
           and not exists (select 1 from billing.payment_reversals r where r.payment_id = p.id)
         order by p.received_at, p.id
    loop
        remaining := pay.left_cents;
        continue when remaining <= 0;

        for lunch in
            select l.check_in_id, l.price_cents, l.fee_cents, l.rate_period_id, l.open_cents, l.price_locked
              from billing.v_check_in_ledger l
             where l.student_id = p_student
               and l.status in ('open', 'partial')
             order by l.service_date, l.check_in_id
        loop
            exit when remaining <= 0;
            if not lunch.price_locked then
                update billing.check_in_billing
                   set locked_price_cents = lunch.price_cents,
                       locked_fee_cents = lunch.fee_cents,
                       locked_rate_period_id = lunch.rate_period_id,
                       locked_at = now()
                 where check_in_id = lunch.check_in_id;
            end if;
            take := least(remaining, lunch.open_cents);
            insert into billing.payment_allocations (payment_id, check_in_id, amount_cents)
            values (pay.id, lunch.check_in_id, take);
            remaining := remaining - take;
            total := total + take;
        end loop;
    end loop;
    return total;
end;
$$;

-- ---------------------------------------------------------------- Stripe
alter table billing.payment_intents
    add column lunches               integer check (lunches > 0),
    add column checkout_url          text,
    add column stripe_payment_intent text unique,
    add column failure_reason        text,
    add column refunded_cents        integer not null default 0 check (refunded_cents >= 0),
    add column disputed_at           timestamptz,
    add column needs_attention       text;          -- set when staff must look (e.g. a partial refund)

-- 'processing' = a bank payment was submitted and is waiting to clear.
alter table billing.payment_intents drop constraint payment_intents_status_check;
alter table billing.payment_intents
    add constraint payment_intents_status_check
    check (status in ('pending', 'processing', 'succeeded', 'failed', 'canceled'));

-- One row per Stripe event we acted on. A retried delivery hits the primary key and is ignored.
create table billing.stripe_events (
    id           text primary key,                 -- evt_...
    type         text not null,
    received_at  timestamptz not null default now(),
    outcome      text not null
);
alter table billing.stripe_events enable row level security;

create trigger stripe_events_append_only
    before update or delete on billing.stripe_events
    for each row execute function billing.reject_mutation();

-- Record a settled Stripe payment for an intent and apply it, in one transaction.
-- Idempotent: payments.processor_ref (the Stripe PaymentIntent id) is unique, so
-- a second call for the same payment records nothing and returns the first one.
create function billing.record_stripe_payment(
    p_intent uuid, p_stripe_payment_intent text, p_method text, p_amount_cents integer
) returns table (payment_id uuid, created boolean, applied_cents bigint)
language plpgsql as $$
declare
    it   billing.payment_intents%rowtype;
    pid  uuid;
begin
    select * into it from billing.payment_intents where id = p_intent for update;
    if not found then
        raise exception 'payment intent % not found', p_intent using errcode = 'no_data_found';
    end if;
    if p_method not in ('card', 'ach') then
        raise exception 'Stripe payments are card or ach (got %)', p_method using errcode = 'check_violation';
    end if;
    if p_amount_cents <> it.amount_cents then
        raise exception 'Stripe amount % does not match intent amount %', p_amount_cents, it.amount_cents
            using errcode = 'check_violation';
    end if;

    select p.id into pid from billing.payments p where p.processor_ref = p_stripe_payment_intent;
    if pid is not null then
        return query select pid, false, 0::bigint;
        return;
    end if;

    insert into billing.payments (institution_id, student_id, paid_by_guardian_id, amount_cents, method,
                                  processor_ref, note)
    values (it.institution_id, it.student_id, it.guardian_id, it.amount_cents, p_method, p_stripe_payment_intent,
            'Online payment for ' || coalesce(it.lunches::text, '?') || ' lunch(es)')
    returning id into pid;

    update billing.payment_intents
       set status = 'succeeded', payment_id = pid, method = p_method,
           stripe_payment_intent = p_stripe_payment_intent, failure_reason = null, updated_at = now()
     where id = p_intent;

    return query select pid, true, billing.apply_credit(it.student_id);
end;
$$;
