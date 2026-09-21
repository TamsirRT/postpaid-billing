-- tests/sql/test_contacts.sql
-- Contact rules from migration 003: children who owe money but can't be
-- emailed are listed for the school and are NEVER handed to statement sending.

\set ON_ERROR_STOP on
\i tests/sql/_helpers.sql

begin;

-- ids: institution ...0a, staff ...f1, children K1..K6 = ...d1..d6, guardians G1,G3,G4,G5 = ...91,93,94,95
insert into billing.institutions (id, slug, name, ordering_location_name, cycle_anchor_date)
values ('00000000-0000-0000-0000-00000000000a', 'sacred-heart', 'Sacred Heart School of Glyndon',
        'Sacred Heart School of Glyndon', '2026-08-31');
insert into billing.staff_roles (user_id, email, role, granted_at)
values ('00000000-0000-0000-0000-0000000000f1', 'admin@example.com', 'admin', now());
insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents)
values ('00000000-0000-0000-0000-00000000000a', '2026-09-01', '2026-09-30', 750);

-- K1..K4 and K6 owe one lunch each; K5 owes nothing
insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification)
select gen_random_uuid(), '00000000-0000-0000-0000-00000000000a', k::uuid, '2026-09-15', 'post_paid'
  from unnest(array['00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-0000000000d2',
                    '00000000-0000-0000-0000-0000000000d3', '00000000-0000-0000-0000-0000000000d4',
                    '00000000-0000-0000-0000-0000000000d6']) k;

-- ---------------------------------------------------------------- validation
select pg_temp.expect_error($q$
    insert into billing.guardians (institution_id, email) values ('00000000-0000-0000-0000-00000000000a', 'redacted')
$q$, '23514', 'placeholder email like "redacted" rejected');

select pg_temp.expect_error($q$
    insert into billing.guardians (institution_id, phone) values ('00000000-0000-0000-0000-00000000000a', 'redacted')
$q$, '23514', 'placeholder phone rejected');

select pg_temp.expect_error($q$
    insert into billing.guardians (institution_id, name) values ('00000000-0000-0000-0000-00000000000a', 'No Contact')
$q$, '23514', 'guardian with neither email nor phone rejected');

select pg_temp.expect_error($q$
    insert into billing.guardians (institution_id, email) values ('00000000-0000-0000-0000-00000000000a', ' padded@example.com')
$q$, '23514', 'untrimmed email rejected');

insert into billing.guardians (id, institution_id, name, email) values
 ('00000000-0000-0000-0000-000000000091', '00000000-0000-0000-0000-00000000000a', 'Gia One', 'gia@example.com');
insert into billing.guardians (id, institution_id, phone) values
 ('00000000-0000-0000-0000-000000000093', '00000000-0000-0000-0000-00000000000a', '410-555-0101');
insert into billing.guardians (id, institution_id, email, receives_notices) values
 ('00000000-0000-0000-0000-000000000094', '00000000-0000-0000-0000-00000000000a', 'optout@example.com', false);
insert into billing.guardians (id, institution_id, phone) values
 ('00000000-0000-0000-0000-000000000095', '00000000-0000-0000-0000-00000000000a', '(443) 555-0199');
select pg_temp.expect_eq(1, 1, 'two phone-only guardians coexist (NULL emails do not collide)');

select pg_temp.expect_error($q$
    insert into billing.guardians (institution_id, email) values ('00000000-0000-0000-0000-00000000000a', 'GIA@example.com')
$q$, '23505', 'same email twice rejected, case-insensitive');

insert into billing.guardian_students (guardian_id, student_id, institution_id) values
 ('00000000-0000-0000-0000-000000000091', '00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-00000000000a'),
 ('00000000-0000-0000-0000-000000000093', '00000000-0000-0000-0000-0000000000d3', '00000000-0000-0000-0000-00000000000a'),
 ('00000000-0000-0000-0000-000000000094', '00000000-0000-0000-0000-0000000000d4', '00000000-0000-0000-0000-00000000000a'),
 ('00000000-0000-0000-0000-000000000091', '00000000-0000-0000-0000-0000000000d6', '00000000-0000-0000-0000-00000000000a'),
 ('00000000-0000-0000-0000-000000000095', '00000000-0000-0000-0000-0000000000d6', '00000000-0000-0000-0000-00000000000a');

-- ---------------------------------------------------------------- who gets skipped
select pg_temp.expect_eq(
    (select string_agg(right(student_id::text, 2) || ':' || reason, ',' order by student_id) from billing.v_billed_without_contact),
    'd2:no_contact,d3:no_email,d4:notices_off',
    'billed children without a usable email are listed with the reason');

select pg_temp.expect_eq(
    (select string_agg(right(guardian_id::text, 2) || '>' || right(student_id::text, 2), ',' order by student_id) from billing.v_statement_recipients),
    '91>d1,91>d6',
    'statement recipients are only guardians with an email who accept notices');

select pg_temp.expect_eq(
    (select count(*) from billing.v_statement_recipients where student_id in
        ('00000000-0000-0000-0000-0000000000d2', '00000000-0000-0000-0000-0000000000d3', '00000000-0000-0000-0000-0000000000d4')),
    0::bigint, 'children without contact never reach statement sending');

select pg_temp.expect_eq(
    (select count(*) from billing.v_billed_without_contact where student_id = '00000000-0000-0000-0000-0000000000d5'),
    0::bigint, 'child who owes nothing is not flagged even with no contact');

select pg_temp.expect_eq(
    (select count(*) from billing.v_billed_without_contact where student_id = '00000000-0000-0000-0000-0000000000d6'),
    0::bigint, 'shared child is covered if any one guardian has an email');

-- paying off the balance removes the child from the school list
insert into billing.payments (institution_id, student_id, amount_cents, method, recorded_by)
values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000d2', 750, 'cash', '00000000-0000-0000-0000-0000000000f1');
select pg_temp.expect_eq(
    (select count(*) from billing.v_billed_without_contact where student_id = '00000000-0000-0000-0000-0000000000d2'),
    0::bigint, 'child drops off the list once nothing is due');

-- ---------------------------------------------------------------- hard stop on sending
select pg_temp.expect_error($q$
    insert into billing.notifications (institution_id, guardian_id, kind)
    values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-000000000093', 'statement')
$q$, '23514', 'database refuses a notification to a guardian with no email');

insert into billing.notifications (institution_id, guardian_id, kind)
values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-000000000091', 'statement');
select pg_temp.expect_eq(1, 1, 'notification to a guardian with an email is accepted');

-- ---------------------------------------------------------------- no-lunch check-ins
insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification, classification_note)
values (gen_random_uuid(), '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000d1', '2026-09-16', 'no_lunch', 'getting_lunch = false');
select pg_temp.expect_eq(
    (select count(*) from billing.v_check_in_ledger where service_date = '2026-09-16'),
    0::bigint, 'check-in with getting_lunch = false is never billed');

insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification, classification_note)
values (gen_random_uuid(), '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000d1', '2026-09-17', 'excluded', 'bill_separately = true');
select pg_temp.expect_eq(
    (select count(*) from billing.v_check_in_ledger where service_date = '2026-09-17'),
    0::bigint, 'check-in marked bill_separately is never billed');

select pg_temp.expect_error($q$
    update billing.check_in_billing set locked_price_cents = 750, locked_at = now() where service_date = '2026-09-17'
$q$, '23514', 'an excluded check-in cannot carry a price');

select pg_temp.expect_eq(
    (select count(*) from information_schema.columns
      where table_schema = 'billing' and table_name = 'institutions' and column_name = 'billing_starts_on'),
    0::bigint, 'billing start lives in code only (no competing database column)');

select pg_temp.expect_eq(
    (select count(*) from pg_tables where schemaname = 'billing' and not rowsecurity),
    0::bigint, 'row-level security still on for every billing table');

rollback;
