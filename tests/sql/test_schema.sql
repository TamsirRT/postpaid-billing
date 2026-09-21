-- tests/sql/test_schema.sql
-- Proves the schema enforces the spec. Run after migrations on a scratch DB:
--   scripts/test_sql.sh
-- Any failed check raises; ON_ERROR_STOP makes psql exit non-zero.
-- Everything runs in one transaction that is rolled back at the end.

\set ON_ERROR_STOP on
\i tests/sql/_helpers.sql

begin;

-- ids ------------------------------------------------------------------------
-- institution  ...0a        staff   ...f1
-- rate periods ...b1 promo, ...b2 fall
-- students     A ...a1, B ...a2
-- check-ins    c...01-06 (A), c...11 (B)
-- payments     e...01 (P1), e...02 (P2)

insert into billing.institutions (id, slug, name, ordering_location_name, cycle_anchor_date)
values ('00000000-0000-0000-0000-00000000000a', 'sacred-heart', 'Sacred Heart School of Glyndon',
        'Sacred Heart School of Glyndon', '2026-08-31');

insert into billing.staff_roles (user_id, email, role, granted_at)
values ('00000000-0000-0000-0000-0000000000f1', 'admin@example.com', 'super_admin', now());

insert into billing.rate_periods (id, institution_id, starts_on, ends_on, price_cents, label) values
 ('00000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-00000000000a', '2026-08-25', '2026-09-12', 500, 'Back-to-school promo'),
 ('00000000-0000-0000-0000-0000000000b2', '00000000-0000-0000-0000-00000000000a', '2026-09-13', '2026-12-19', 750, 'Fall');

-- ------------------------------------------------------------ rate periods
select pg_temp.expect_error($q$
    insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents)
    values ('00000000-0000-0000-0000-00000000000a', '2026-09-10', '2026-09-20', 600)
$q$, '23P01', 'overlapping rate period rejected');

select pg_temp.expect_error($q$
    insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents)
    values ('00000000-0000-0000-0000-00000000000a', '2026-12-19', '2026-12-19', 600)
$q$, '23P01', 'rate period sharing a single boundary day rejected (inclusive ranges)');

select pg_temp.expect_error($q$
    insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents)
    values ('00000000-0000-0000-0000-00000000000a', '2027-01-10', '2027-01-01', 600)
$q$, '23514', 'rate period ending before it starts rejected');

-- ------------------------------------------------------------ check-ins
insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification) values
 ('c0000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000a1', '2026-09-02', 'post_paid'),
 ('c0000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000a1', '2026-09-15', 'post_paid'),
 ('c0000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000a1', '2026-09-16', 'post_paid'),
 ('c0000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000a1', '2026-09-17', 'pre_ordered'),
 ('c0000000-0000-0000-0000-000000000005', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000a1', '2026-12-22', 'post_paid'),
 ('c0000000-0000-0000-0000-000000000011', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000a2', '2026-09-15', 'post_paid');

select pg_temp.expect_error($q$
    insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification)
    values ('c0000000-0000-0000-0000-000000000006', '00000000-0000-0000-0000-00000000000a',
            '00000000-0000-0000-0000-0000000000a1', '2026-09-15', 'post_paid')
$q$, '23505', 'second post-paid check-in for same child and day rejected');

insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification)
values ('c0000000-0000-0000-0000-000000000006', '00000000-0000-0000-0000-00000000000a',
        '00000000-0000-0000-0000-0000000000a1', '2026-09-15', 'duplicate');
select pg_temp.expect_eq(1, 1, 'same-day extra check-in accepted as duplicate');

select pg_temp.expect_error($q$
    insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification, locked_price_cents, locked_at)
    values ('c0000000-0000-0000-0000-000000000007', '00000000-0000-0000-0000-00000000000a',
            '00000000-0000-0000-0000-0000000000a1', '2026-09-18', 'pre_ordered', 500, now())
$q$, '23514', 'pre-ordered check-in cannot carry a price');

-- ------------------------------------------------------------ live pricing
select pg_temp.expect_eq((select price_cents from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000001'), 500, 'promo date priced at promo rate');
select pg_temp.expect_eq((select rate_label from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000001'), 'Back-to-school promo', 'rate label carried to ledger');
select pg_temp.expect_eq((select status from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000005'), 'needs_rate', 'uncovered date is needs_rate');
select pg_temp.expect_eq((select count(*) from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000004'), 0::bigint, 'pre-ordered check-in not in ledger');
select pg_temp.expect_eq((select open_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), 2000::bigint, 'A owes 500 + 750 + 750; needs-rate excluded');
select pg_temp.expect_eq((select needs_rate_count from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), 1::bigint, 'A has one needs-rate lunch');

update billing.rate_periods set price_cents = 800 where id = '00000000-0000-0000-0000-0000000000b2';
select pg_temp.expect_eq((select open_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), 2100::bigint, 'rate edit reprices unpaid lunches immediately');

-- ------------------------------------------------------------ payments + locks
insert into billing.payments (id, institution_id, student_id, amount_cents, method, recorded_by)
values ('e0000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-00000000000a',
        '00000000-0000-0000-0000-0000000000a1', 1000, 'cash', '00000000-0000-0000-0000-0000000000f1');

select pg_temp.expect_error($q$
    insert into billing.payment_allocations (payment_id, check_in_id, amount_cents)
    values ('e0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000001', 500)
$q$, '23514', 'cannot allocate to a check-in whose price is not locked');

update billing.check_in_billing set locked_price_cents = 500, locked_rate_period_id = '00000000-0000-0000-0000-0000000000b1', locked_at = now()
 where check_in_id = 'c0000000-0000-0000-0000-000000000001';
update billing.check_in_billing set locked_price_cents = 800, locked_rate_period_id = '00000000-0000-0000-0000-0000000000b2', locked_at = now()
 where check_in_id = 'c0000000-0000-0000-0000-000000000002';

insert into billing.payment_allocations (payment_id, check_in_id, amount_cents) values
 ('e0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000001', 500),
 ('e0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000002', 500);

select pg_temp.expect_eq((select status from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000001'), 'paid', 'fully covered lunch is paid');
select pg_temp.expect_eq((select status from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000002'), 'partial', 'half-covered lunch is partial');
select pg_temp.expect_eq((select balance_due_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), 1100::bigint, 'A owes 300 on the partial + 800 open');

update billing.rate_periods set price_cents = 900 where id = '00000000-0000-0000-0000-0000000000b2';
select pg_temp.expect_eq((select price_cents from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000002'), 800, 'partially paid lunch keeps its locked price');
select pg_temp.expect_eq((select price_cents from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000003'), 900, 'unpaid lunch follows the rate change');

select pg_temp.expect_error($q$
    update billing.check_in_billing set locked_price_cents = 900 where check_in_id = 'c0000000-0000-0000-0000-000000000002'
$q$, '23001', 'locked price cannot be changed');

select pg_temp.expect_error($q$
    update billing.check_in_billing set classification = 'pre_ordered', locked_price_cents = null, locked_rate_period_id = null, locked_at = null
     where check_in_id = 'c0000000-0000-0000-0000-000000000002'
$q$, '23001', 'check-in with money applied cannot be reclassified directly');

-- ------------------------------------------------------------ allocation invariants
insert into billing.payments (id, institution_id, student_id, amount_cents, method, recorded_by)
values ('e0000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-00000000000a',
        '00000000-0000-0000-0000-0000000000a1', 1000, 'check', '00000000-0000-0000-0000-0000000000f1');

select pg_temp.expect_error($q$
    insert into billing.payment_allocations (payment_id, check_in_id, amount_cents)
    values ('e0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000002', 400)
$q$, '23514', 'allocation exceeding the lunch price rejected (500 + 400 > 800)');

update billing.check_in_billing set locked_price_cents = 900, locked_rate_period_id = '00000000-0000-0000-0000-0000000000b2', locked_at = now()
 where check_in_id = 'c0000000-0000-0000-0000-000000000003';

select pg_temp.expect_error($q$
    insert into billing.payment_allocations (payment_id, check_in_id, amount_cents)
    values ('e0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000003', 100)
$q$, '23514', 'allocation exceeding the payment rejected (1000 already applied)');

update billing.check_in_billing set locked_price_cents = 900, locked_rate_period_id = '00000000-0000-0000-0000-0000000000b2', locked_at = now()
 where check_in_id = 'c0000000-0000-0000-0000-000000000011';

select pg_temp.expect_error($q$
    insert into billing.payment_allocations (payment_id, check_in_id, amount_cents)
    values ('e0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000011', 100)
$q$, '23514', 'payment for child A cannot pay child B''s lunch');

select pg_temp.expect_error($q$
    insert into billing.payment_allocations (payment_id, check_in_id, amount_cents)
    values ('e0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000004', 100)
$q$, '23514', 'cannot allocate to a pre-ordered check-in');

insert into billing.payment_allocations (payment_id, check_in_id, amount_cents)
values ('e0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000002', 300);

-- A: c1 paid 500/500, c2 paid 800/800, c3 open 900, c5 needs rate
-- paid in 2000, applied 1300 -> credit 700; open 900; due 200
select pg_temp.expect_eq((select open_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), 900::bigint, 'A open after second payment');
select pg_temp.expect_eq((select credit_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), 700::bigint, 'unapplied money shows as credit');
select pg_temp.expect_eq((select balance_due_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), 200::bigint, 'balance due nets credit against open');

-- ------------------------------------------------------------ append-only
select pg_temp.expect_error($q$ update billing.payments set amount_cents = 1 where id = 'e0000000-0000-0000-0000-000000000001' $q$, '23001', 'payments cannot be updated');
select pg_temp.expect_error($q$ delete from billing.payments where id = 'e0000000-0000-0000-0000-000000000001' $q$, '23001', 'payments cannot be deleted');
select pg_temp.expect_error($q$ delete from billing.payment_allocations $q$, '23001', 'allocations cannot be deleted');
select pg_temp.expect_error($q$ delete from billing.check_in_billing where check_in_id = 'c0000000-0000-0000-0000-000000000003' $q$, '23001', 'billed check-ins cannot be deleted');
insert into billing.audit_log (action, entity) values ('test', 'test');
select pg_temp.expect_error($q$ update billing.audit_log set action = 'x' $q$, '23001', 'audit log cannot be edited');

-- ------------------------------------------------------------ waivers
update billing.check_in_billing
   set waived_at = now(), waived_by = '00000000-0000-0000-0000-0000000000f1', waive_reason = 'Field trip, lunch provided'
 where check_in_id = 'c0000000-0000-0000-0000-000000000003';
select pg_temp.expect_eq((select status from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000003'), 'waived', 'waived lunch has waived status');
select pg_temp.expect_eq((select balance_due_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), -700::bigint, 'waiving the only open lunch leaves net credit');

select pg_temp.expect_error($q$
    update billing.check_in_billing set waived_at = now() where check_in_id = 'c0000000-0000-0000-0000-000000000001'
$q$, '23514', 'waiver needs a reason and a staff member');

-- ------------------------------------------------------------ reversals
insert into billing.payment_reversals (payment_id, reason, recorded_by)
values ('e0000000-0000-0000-0000-000000000001', 'Check bounced', '00000000-0000-0000-0000-0000000000f1');
-- P1's 500 + 500 no longer count. c1 open 500 (locked), c2 has 300 of 800 -> open 500.
-- paid in 1000 (P2), applied 300 -> credit 700; open 1000; due 300
select pg_temp.expect_eq((select status from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000001'), 'open', 'reversal reopens the lunch');
select pg_temp.expect_eq((select price_cents from billing.v_check_in_ledger where check_in_id = 'c0000000-0000-0000-0000-000000000001'), 500, 'reopened lunch keeps its locked price');
select pg_temp.expect_eq((select balance_due_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), 300::bigint, 'balance after reversal');

select pg_temp.expect_error($q$
    insert into billing.payment_allocations (payment_id, check_in_id, amount_cents)
    values ('e0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000001', 100)
$q$, '23514', 'reversed payment cannot be allocated');

-- waiving a lunch that has money on it releases that money as credit
update billing.check_in_billing
   set waived_at = now(), waived_by = '00000000-0000-0000-0000-0000000000f1', waive_reason = 'Billed in error'
 where check_in_id = 'c0000000-0000-0000-0000-000000000002';
-- open: c1 500. paid in 1000, applied 0 -> credit 1000; due -500
select pg_temp.expect_eq((select credit_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), 1000::bigint, 'money on a waived lunch returns as credit');
select pg_temp.expect_eq((select balance_due_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a1'), -500::bigint, 'final balance for A');
select pg_temp.expect_eq((select balance_due_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a2'), 900::bigint, 'child B unaffected by A''s payments');

-- ------------------------------------------------------------ payment intents
insert into billing.payment_intents (institution_id, student_id, amount_cents, method, processor_ref)
values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000a2', 900, 'ach', 'cs_test_1');
select pg_temp.expect_eq((select balance_due_cents from billing.v_student_balances where student_id = '00000000-0000-0000-0000-0000000000a2'), 900::bigint, 'pending bank payment does not reduce the balance');
select pg_temp.expect_error($q$
    update billing.payment_intents set status = 'succeeded' where processor_ref = 'cs_test_1'
$q$, '23514', 'intent cannot be marked succeeded without its payment row');

-- ------------------------------------------------------------ review queue
insert into billing.review_items (institution_id, source, raw_reference, service_date, reason)
values ('00000000-0000-0000-0000-00000000000a', 'order', 'uid123|jane doe', '2026-09-15', 'No matching student');
select pg_temp.expect_error($q$
    insert into billing.review_items (institution_id, source, raw_reference, service_date, reason)
    values ('00000000-0000-0000-0000-00000000000a', 'order', 'uid123|jane doe', '2026-09-15', 'No matching student')
$q$, '23505', 'nightly re-run does not duplicate an open review item');
update billing.review_items set status = 'dismissed', resolved_at = now(), resolved_by = '00000000-0000-0000-0000-0000000000f1';
insert into billing.review_items (institution_id, source, raw_reference, service_date, reason)
values ('00000000-0000-0000-0000-00000000000a', 'order', 'uid123|jane doe', '2026-09-15', 'No matching student');
select pg_temp.expect_eq(1, 1, 'same problem can reopen after the old item is closed');

-- ------------------------------------------------------------ staff + guardians
select pg_temp.expect_error($q$
    insert into billing.staff_roles (user_id, email, role) values (gen_random_uuid(), 'x@example.com', 'admin')
$q$, '23514', 'role cannot be set without a grant timestamp');

insert into billing.guardians (institution_id, name, email)
values ('00000000-0000-0000-0000-00000000000a', 'Pat Parent', 'Pat@Example.com');
select pg_temp.expect_error($q$
    insert into billing.guardians (institution_id, name, email)
    values ('00000000-0000-0000-0000-00000000000a', 'Pat Again', 'pat@example.com')
$q$, '23505', 'guardian email unique per school, case-insensitive');

-- ------------------------------------------------------------ security
select pg_temp.expect_eq(
    (select count(*) from pg_tables where schemaname = 'billing' and not rowsecurity),
    0::bigint, 'row-level security enabled on every billing table');

rollback;
