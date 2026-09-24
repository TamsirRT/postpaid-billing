-- 005_default_price_and_allocation.sql
--
--   * Every school has a default price per post-paid meal, settable in the app
--     (Sacred Heart starts at $7.90). Rate periods override it for date ranges
--     (promos). So a lunch always has a price; 'needs_rate' only happens if a
--     school's default is ever removed.
--   * billing.apply_credit(student) applies a child's unapplied money to their
--     oldest open lunches, locking each lunch's price as money lands on it.
--     Used after a payment, after a waiver releases money, and after new lunches
--     are billed to a child who has credit.

alter table billing.institutions
    add column default_price_cents integer not null default 790 check (default_price_cents >= 0);

-- ---------------------------------------------------------------- ledger: default price
create or replace view billing.v_check_in_ledger as
select
    cb.check_in_id,
    cb.institution_id,
    cb.student_id,
    cb.service_date,
    cb.locked_at,
    cb.waived_at,
    cb.waive_reason,
    coalesce(cb.locked_price_cents, live.price_cents, i.default_price_cents)   as price_cents,
    coalesce(cb.locked_rate_period_id, live.id)                                 as rate_period_id,
    coalesce(rp.label, case when rp.id is not null then 'Special rate' else 'Standard rate' end) as rate_label,
    (cb.locked_at is not null)                                                  as price_locked,
    coalesce(alloc.allocated_cents, 0)::bigint                                  as allocated_cents,
    case
        when cb.waived_at is not null                                                            then 'waived'
        when coalesce(cb.locked_price_cents, live.price_cents, i.default_price_cents) is null    then 'needs_rate'
        when coalesce(alloc.allocated_cents, 0)
             >= coalesce(cb.locked_price_cents, live.price_cents, i.default_price_cents)         then 'paid'
        when coalesce(alloc.allocated_cents, 0) > 0                                              then 'partial'
        else 'open'
    end                                                                         as status,
    case
        when cb.waived_at is not null                                                            then 0
        when coalesce(cb.locked_price_cents, live.price_cents, i.default_price_cents) is null    then 0
        else greatest(coalesce(cb.locked_price_cents, live.price_cents, i.default_price_cents)
                      - coalesce(alloc.allocated_cents, 0), 0)
    end::bigint                                                                 as open_cents,
    case when cb.locked_at is not null then 'locked'
         when live.id is not null      then 'rate_period'
         else 'default' end                                                     as price_source
from billing.check_in_billing cb
join billing.institutions i on i.id = cb.institution_id
left join lateral (
    select r.id, r.price_cents
      from billing.rate_periods r
     where r.institution_id = cb.institution_id
       and cb.service_date between r.starts_on and r.ends_on
) live on cb.locked_at is null
left join billing.rate_periods rp
       on rp.id = coalesce(cb.locked_rate_period_id, live.id)
left join lateral (
    select sum(a.amount_cents) as allocated_cents
      from billing.payment_allocations a
     where a.check_in_id = cb.check_in_id
       and not exists (select 1 from billing.payment_reversals r where r.payment_id = a.payment_id)
) alloc on true
where cb.classification = 'post_paid';

-- A payment can land on the same lunch twice: e.g. a waiver elsewhere releases
-- part of a payment, and that money flows back to a lunch the payment already
-- partly covers. Allocation rows are append-only, so this is just a second row.
alter table billing.payment_allocations drop constraint payment_allocations_payment_id_check_in_id_key;

-- ---------------------------------------------------------------- allocation trigger
-- Money sitting on a WAIVED lunch is released back to the child as credit, so
-- it no longer counts against the payment when that money is re-applied.
create or replace function billing.check_allocation() returns trigger
language plpgsql as $$
declare
    c   billing.check_in_billing%rowtype;
    p   billing.payments%rowtype;
    already_on_check_in  bigint;
    already_from_payment bigint;
begin
    select * into c from billing.check_in_billing where check_in_id = new.check_in_id for update;
    select * into p from billing.payments where id = new.payment_id for update;

    if c.classification <> 'post_paid' then
        raise exception 'check-in % is %, not post_paid', c.check_in_id, c.classification using errcode = 'check_violation';
    end if;
    if c.waived_at is not null then
        raise exception 'check-in % is waived', c.check_in_id using errcode = 'check_violation';
    end if;
    if c.locked_price_cents is null then
        raise exception 'check-in % has no locked price; lock it before allocating', c.check_in_id using errcode = 'check_violation';
    end if;
    if p.student_id <> c.student_id then
        raise exception 'payment % is for a different child than check-in %', p.id, c.check_in_id using errcode = 'check_violation';
    end if;
    if exists (select 1 from billing.payment_reversals r where r.payment_id = p.id) then
        raise exception 'payment % has been reversed', p.id using errcode = 'check_violation';
    end if;

    select coalesce(sum(a.amount_cents), 0) into already_on_check_in
      from billing.payment_allocations a
     where a.check_in_id = new.check_in_id
       and not exists (select 1 from billing.payment_reversals r where r.payment_id = a.payment_id);
    if already_on_check_in + new.amount_cents > c.locked_price_cents then
        raise exception 'allocation would exceed price of check-in % (% + % > %)',
            c.check_in_id, already_on_check_in, new.amount_cents, c.locked_price_cents using errcode = 'check_violation';
    end if;

    select coalesce(sum(a.amount_cents), 0) into already_from_payment
      from billing.payment_allocations a
      join billing.check_in_billing cb on cb.check_in_id = a.check_in_id
     where a.payment_id = new.payment_id
       and cb.waived_at is null;
    if already_from_payment + new.amount_cents > p.amount_cents then
        raise exception 'allocation would exceed payment % (% + % > %)',
            p.id, already_from_payment, new.amount_cents, p.amount_cents using errcode = 'check_violation';
    end if;

    return new;
end;
$$;

-- ---------------------------------------------------------------- apply credit
-- Oldest money first, onto oldest open lunches first. A lunch's price locks the
-- moment money lands on it. Returns the cents applied. Safe to call any time;
-- does nothing if the child has no credit or no open lunches.
create function billing.apply_credit(p_student uuid) returns bigint
language plpgsql as $$
declare
    pay        record;
    lunch      record;
    remaining  bigint;
    take       bigint;
    total      bigint := 0;
begin
    -- one allocation run per child at a time (two parents paying at once)
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
            select l.check_in_id, l.price_cents, l.rate_period_id, l.open_cents, l.price_locked
              from billing.v_check_in_ledger l
             where l.student_id = p_student
               and l.status in ('open', 'partial')
             order by l.service_date, l.check_in_id
        loop
            exit when remaining <= 0;
            if not lunch.price_locked then
                update billing.check_in_billing
                   set locked_price_cents = lunch.price_cents,
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

-- Record a payment made outside Stripe (cash, check, Zoho) and apply it.
create function billing.record_offline_payment(
    p_institution uuid, p_student uuid, p_amount_cents integer, p_method text,
    p_received_at timestamptz, p_recorded_by uuid, p_note text
) returns table (payment_id uuid, applied_cents bigint)
language plpgsql as $$
declare
    pid uuid;
begin
    if p_method not in ('cash', 'check', 'zoho', 'other') then
        raise exception 'offline payments must be cash, check, zoho, or other (got %)', p_method
            using errcode = 'check_violation';
    end if;
    insert into billing.payments (institution_id, student_id, amount_cents, method, received_at, recorded_by, note)
    values (p_institution, p_student, p_amount_cents, p_method, p_received_at, p_recorded_by, p_note)
    returning id into pid;
    return query select pid, billing.apply_credit(p_student);
end;
$$;
