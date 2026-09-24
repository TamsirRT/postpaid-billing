-- tests/sql/test_meal_increments.sql
-- Parents pay for whole lunches, oldest first, at each lunch's own price (migration 006).

\set ON_ERROR_STOP on
\i tests/sql/_helpers.sql

begin;

-- default $7.90; promo Sep 1-5 at $5.00. Child K = ...k1 : lunches Sep 2 (promo), Sep 8, Sep 9, Sep 10
insert into billing.institutions (id, slug, name, ordering_location_name, cycle_anchor_date)
values ('00000000-0000-0000-0000-00000000000a', 'sacred-heart', 'Sacred Heart', 'Sacred Heart', '2026-08-31');
insert into billing.staff_roles (user_id, email, role, granted_at)
values ('00000000-0000-0000-0000-0000000000f1', 'admin@example.com', 'admin', now());
insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents, label)
values ('00000000-0000-0000-0000-00000000000a', '2026-09-01', '2026-09-05', 500, 'Back-to-school');
insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification) values
 ('c3000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000b1', '2026-09-02', 'post_paid'),
 ('c3000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000b1', '2026-09-08', 'post_paid'),
 ('c3000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000b1', '2026-09-09', 'post_paid'),
 ('c3000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000b1', '2026-09-10', 'post_paid');

create temp view k_opts as select * from billing.v_payment_options where student_id = '00000000-0000-0000-0000-0000000000b1';

select pg_temp.expect_eq((select string_agg(lunches || '=' || amount_cents, ',' order by lunches) from k_opts),
                         '1=500,2=1290,3=2080,4=2870', 'running totals mix the promo and standard prices, oldest first');
select pg_temp.expect_eq(billing.meal_increment_lunches('00000000-0000-0000-0000-0000000000b1', 1290), 2, '$12.90 pays exactly 2 lunches');
select pg_temp.expect_eq(billing.meal_increment_lunches('00000000-0000-0000-0000-0000000000b1', 790), null::int,
                         '$7.90 is not allowed: the oldest lunch costs $5.00, so it must be paid first');
select pg_temp.expect_eq(billing.meal_increment_lunches('00000000-0000-0000-0000-0000000000b1', 1000), null::int, '$10.00 is not a whole-lunch amount');
select pg_temp.expect_eq(billing.meal_increment_lunches('00000000-0000-0000-0000-0000000000b1', 2870), 4, 'the full balance is always allowed');

-- online payments: only allowed amounts can even be started
select pg_temp.expect_error($q$
    insert into billing.payment_intents (institution_id, student_id, amount_cents, method, processor_ref)
    values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000b1', 1000, 'card', 'cs_bad')
$q$, '23514', 'online payment for $10.00 refused');
insert into billing.payment_intents (institution_id, student_id, amount_cents, method, processor_ref)
values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000b1', 1290, 'card', 'cs_ok');
select pg_temp.expect_eq(1, 1, 'online payment for 2 lunches ($12.90) accepted');

-- staff can still record any amount; a $10 check leaves the 2nd lunch part-paid
select pg_temp.expect_eq(
    (select applied_cents from billing.record_offline_payment('00000000-0000-0000-0000-00000000000a',
        '00000000-0000-0000-0000-0000000000b1', 1000, 'check', now(), '00000000-0000-0000-0000-0000000000f1', null)),
    1000::bigint, 'staff record a $10 check (not a whole-lunch amount)');
select pg_temp.expect_eq((select string_agg(lunches || '=' || amount_cents, ',' order by lunches) from k_opts),
                         '1=290,2=1080,3=1870', 'after it, the part-paid lunch leads with its $2.90 remainder');

-- price changes flow through to unpaid lunches only
update billing.institutions set default_price_cents = 850;
select pg_temp.expect_eq((select string_agg(lunches || '=' || amount_cents, ',' order by lunches) from k_opts),
                         '1=290,2=1140,3=1990', 'standard price $8.50: locked part-paid lunch unchanged, others repriced');

-- waived lunches drop out of the list
update billing.check_in_billing set waived_at = now(), waived_by = '00000000-0000-0000-0000-0000000000f1', waive_reason = 'Field trip'
 where check_in_id = 'c3000000-0000-0000-0000-000000000003';
select pg_temp.expect_eq((select string_agg(lunches || '=' || amount_cents, ',' order by lunches) from k_opts),
                         '1=290,2=1140', 'waived lunch is not payable');

-- nothing owed -> no allowed amounts
select pg_temp.expect_eq(billing.meal_increment_lunches('00000000-0000-0000-0000-0000000000c9', 790), null::int, 'child who owes nothing cannot start a payment');

rollback;
