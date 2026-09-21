-- 002_balance_views.sql
-- Balances are derived, never stored. These views are the single definition
-- of "what is owed"; every screen, email, and export reads from them.
--
-- Price rule (spec, Pricing):
--   * unpaid post-paid check-in  -> live price from the rate period covering its date
--   * any money applied          -> the locked price, never repriced
--   * no rate period covers date -> NULL price, status 'needs_rate', excluded from balance
--
-- Allocations from a reversed payment don't count. Allocations sitting on a
-- WAIVED check-in are released: they count as credit, not as paid lunches.

create view billing.v_check_in_ledger as
select
    cb.check_in_id,
    cb.institution_id,
    cb.student_id,
    cb.service_date,
    cb.locked_at,
    cb.waived_at,
    cb.waive_reason,
    coalesce(cb.locked_price_cents, live.price_cents)      as price_cents,
    coalesce(cb.locked_rate_period_id, live.id)            as rate_period_id,
    rp.label                                               as rate_label,
    (cb.locked_at is not null)                             as price_locked,
    coalesce(alloc.allocated_cents, 0)::bigint             as allocated_cents,
    case
        when cb.waived_at is not null                                         then 'waived'
        when coalesce(cb.locked_price_cents, live.price_cents) is null        then 'needs_rate'
        when coalesce(alloc.allocated_cents, 0)
             >= coalesce(cb.locked_price_cents, live.price_cents)             then 'paid'
        when coalesce(alloc.allocated_cents, 0) > 0                           then 'partial'
        else 'open'
    end                                                    as status,
    case
        when cb.waived_at is not null                                         then 0
        when coalesce(cb.locked_price_cents, live.price_cents) is null        then 0
        else greatest(coalesce(cb.locked_price_cents, live.price_cents)
                      - coalesce(alloc.allocated_cents, 0), 0)
    end::bigint                                            as open_cents
from billing.check_in_billing cb
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

comment on view billing.v_check_in_ledger is
    'One row per post-paid check-in with its effective price, amount applied, status, and open amount.';


create view billing.v_student_balances as
with charges as (
    select institution_id, student_id,
           sum(open_cents)::bigint                                as open_cents,
           count(*) filter (where status in ('open', 'partial'))  as unpaid_count,
           count(*) filter (where status = 'needs_rate')          as needs_rate_count,
           min(service_date) filter (where status in ('open', 'partial')) as oldest_unpaid_date
      from billing.v_check_in_ledger
     group by institution_id, student_id
),
money_in as (
    select p.institution_id, p.student_id, sum(p.amount_cents)::bigint as paid_in_cents
      from billing.payments p
     where not exists (select 1 from billing.payment_reversals r where r.payment_id = p.id)
     group by p.institution_id, p.student_id
),
applied as (
    -- money that is actually covering a live (non-waived) lunch
    select cb.institution_id, cb.student_id, sum(a.amount_cents)::bigint as applied_cents
      from billing.payment_allocations a
      join billing.check_in_billing cb on cb.check_in_id = a.check_in_id
     where cb.waived_at is null
       and not exists (select 1 from billing.payment_reversals r where r.payment_id = a.payment_id)
     group by cb.institution_id, cb.student_id
),
students as (
    select institution_id, student_id from charges
    union
    select institution_id, student_id from money_in
)
select
    s.institution_id,
    s.student_id,
    coalesce(c.open_cents, 0)                                             as open_cents,
    coalesce(m.paid_in_cents, 0) - coalesce(ap.applied_cents, 0)          as credit_cents,
    coalesce(c.open_cents, 0)
      - (coalesce(m.paid_in_cents, 0) - coalesce(ap.applied_cents, 0))    as balance_due_cents,
    coalesce(c.unpaid_count, 0)                                           as unpaid_count,
    coalesce(c.needs_rate_count, 0)                                       as needs_rate_count,
    c.oldest_unpaid_date
from students s
left join charges  c  using (institution_id, student_id)
left join money_in m  using (institution_id, student_id)
left join applied  ap using (institution_id, student_id);

comment on view billing.v_student_balances is
    'Per child: open_cents owed on lunches, credit_cents paid but not yet applied, balance_due_cents = open - credit (negative = net credit).';
