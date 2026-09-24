-- tests/sql/test_payments.sql
-- Default price, offline payments, and billing.apply_credit (migration 005).

\set ON_ERROR_STOP on
\i tests/sql/_helpers.sql

begin;

-- institution ...0a (default $7.90), staff ...f1, promo Sep 1-5 at $5.00
-- child S = ...e1 : L1 Sep 2 (promo), L2 Sep 8, L3 Sep 9
-- child T = ...e2 : M1 Sep 8, M2 Sep 9
insert into billing.institutions (id, slug, name, ordering_location_name, cycle_anchor_date)
values ('00000000-0000-0000-0000-00000000000a', 'sacred-heart', 'Sacred Heart', 'Sacred Heart', '2026-08-31');
insert into billing.staff_roles (user_id, email, role, granted_at)
values ('00000000-0000-0000-0000-0000000000f1', 'admin@example.com', 'admin', now());
insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents, label)
values ('00000000-0000-0000-0000-00000000000a', '2026-09-01', '2026-09-05', 500, 'Back-to-school');

select pg_temp.expect_eq((select default_price_cents from billing.institutions), 790, 'default price starts at $7.90');

insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification) values
 ('c1000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', '2026-09-02', 'post_paid'),
 ('c1000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', '2026-09-08', 'post_paid'),
 ('c1000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', '2026-09-09', 'post_paid'),
 ('c2000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e2', '2026-09-08', 'post_paid'),
 ('c2000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e2', '2026-09-09', 'post_paid');

create temp view s_bal as select * from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000e1';
create temp view s_led as select * from billing.v_check_in_ledger where student_id = '00000000-0000-0000-0000-0000000000e1';

select pg_temp.expect_eq((select string_agg(price_cents || ':' || price_source, ',' order by service_date) from s_led),
                         '500:rate_period,790:default,790:default', 'promo date uses the rate period, others the default');
select pg_temp.expect_eq((select balance_due_cents from s_bal), 2080::bigint, 'S owes 5.00 + 7.90 + 7.90');

-- ------------------------------------------------------------ offline payment, oldest first
select pg_temp.expect_eq(
    (select applied_cents from billing.record_offline_payment(
        '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', 1000, 'cash',
        '2026-09-10 12:00-04', '00000000-0000-0000-0000-0000000000f1', 'paid at front desk')),
    1000::bigint, '$10 cash is fully applied');
select pg_temp.expect_eq((select string_agg(status || ':' || allocated_cents, ',' order by service_date) from s_led),
                         'paid:500,partial:500,open:0', 'oldest lunch first: $5 promo lunch paid, $5 onto the next');
select pg_temp.expect_eq((select string_agg(price_locked::text, ',' order by service_date) from s_led),
                         'true,true,false', 'price locks only on lunches money landed on');
select pg_temp.expect_eq((select balance_due_cents from s_bal), 1080::bigint, 'S owes 2.90 + 7.90');

-- ------------------------------------------------------------ changing the default price
update billing.institutions set default_price_cents = 900;
select pg_temp.expect_eq((select string_agg(price_cents::text, ',' order by service_date) from s_led),
                         '500,790,900', 'default change reprices only the untouched lunch');
select pg_temp.expect_eq((select balance_due_cents from s_bal), 1190::bigint, 'S owes 2.90 + 9.00');

-- ------------------------------------------------------------ overpayment becomes credit
select pg_temp.expect_eq(
    (select applied_cents from billing.record_offline_payment(
        '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', 2000, 'check',
        '2026-09-11 12:00-04', '00000000-0000-0000-0000-0000000000f1', 'check #1041')),
    1190::bigint, '$20 check: $11.90 applied');
select pg_temp.expect_eq((select credit_cents from s_bal), 810::bigint, 'the rest is credit');
select pg_temp.expect_eq((select balance_due_cents from s_bal), -810::bigint, 'net credit shows as negative balance');

-- ------------------------------------------------------------ credit applies to the next lunch
insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification)
values ('c1000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', '2026-09-10', 'post_paid');
select pg_temp.expect_eq(billing.apply_credit('00000000-0000-0000-0000-0000000000e1'), 810::bigint, 'credit goes to the new lunch');
select pg_temp.expect_eq((select balance_due_cents from s_bal), 90::bigint, 'S owes the last 90 cents');
select pg_temp.expect_eq(billing.apply_credit('00000000-0000-0000-0000-0000000000e1'), 0::bigint, 'running it again does nothing');

-- ------------------------------------------------------------ waiver releases money
update billing.check_in_billing
   set waived_at = now(), waived_by = '00000000-0000-0000-0000-0000000000f1', waive_reason = 'Field trip'
 where check_in_id = 'c1000000-0000-0000-0000-000000000001';
select pg_temp.expect_eq(billing.apply_credit('00000000-0000-0000-0000-0000000000e1'), 90::bigint, 'released $5 covers the last 90 cents');
select pg_temp.expect_eq((select credit_cents from s_bal), 410::bigint, 'and $4.10 stays as credit');
select pg_temp.expect_eq((select count(*) from s_led where status in ('open', 'partial')), 0::bigint, 'S has nothing open');

-- the case a one-row-per-(payment, lunch) rule would have broken:
-- Q partly covers M2; waiving M1 releases Q's money back onto M2
select pg_temp.expect_eq(
    (select applied_cents from billing.record_offline_payment(
        '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e2', 1000, 'zoho',
        '2026-09-12 12:00-04', '00000000-0000-0000-0000-0000000000f1', null)),
    1000::bigint, 'T pays $10: $9.00 on M1, $1.00 on M2');
update billing.check_in_billing
   set waived_at = now(), waived_by = '00000000-0000-0000-0000-0000000000f1', waive_reason = 'Billed in error'
 where check_in_id = 'c2000000-0000-0000-0000-000000000001';
select pg_temp.expect_eq(billing.apply_credit('00000000-0000-0000-0000-0000000000e2'), 800::bigint,
                         'released $9.00: $8.00 lands back on M2, same payment, second allocation row');
select pg_temp.expect_eq((select status from billing.v_check_in_ledger where check_in_id = 'c2000000-0000-0000-0000-000000000002'),
                         'paid', 'M2 now paid');
select pg_temp.expect_eq((select credit_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000e2'),
                         100::bigint, 'T keeps $1.00 credit');

-- ------------------------------------------------------------ guards
select pg_temp.expect_error($q$
    select * from billing.record_offline_payment('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1',
                                                 500, 'card', now(), '00000000-0000-0000-0000-0000000000f1', null)
$q$, '23514', 'card payments cannot be recorded as offline');

select pg_temp.expect_error($q$
    select * from billing.record_offline_payment('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1',
                                                 0, 'cash', now(), '00000000-0000-0000-0000-0000000000f1', null)
$q$, '23514', 'zero-dollar payment rejected');

select pg_temp.expect_error($q$ update billing.institutions set default_price_cents = -1 $q$, '23514', 'negative default price rejected');

-- reversed payment's money is not re-applied
insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification)
values ('c2000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e2', '2026-09-10', 'post_paid');
insert into billing.payment_reversals (payment_id, reason, recorded_by)
select id, 'Zoho payment failed', '00000000-0000-0000-0000-0000000000f1' from billing.payments where method = 'zoho';
select pg_temp.expect_eq(billing.apply_credit('00000000-0000-0000-0000-0000000000e2'), 0::bigint, 'reversed payment is never applied');
select pg_temp.expect_eq((select balance_due_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000e2'),
                         1800::bigint, 'T owes M2 and M3 again ($9.00 each) after the reversal');

rollback;
