-- tests/sql/test_fees_stripe.sql
-- The fee line (price = meal + processing fee) and Stripe payment recording (migration 008).

\set ON_ERROR_STOP on
\i tests/sql/_helpers.sql

begin;

-- meal $7.90 + default fee $0.35; promo Sep 1-5: meal $5.00 with the default fee;
-- event Sep 14-18: meal $6.00 with its own fee $0.00. Child B has lunches Sep 2, 8, 9, 15.
insert into billing.institutions (id, slug, name, ordering_location_name, cycle_anchor_date)
values ('00000000-0000-0000-0000-00000000000a', 'sacred-heart', 'Sacred Heart', 'Sacred Heart', '2026-08-31');
insert into billing.staff_roles (user_id, email, role, granted_at)
values ('00000000-0000-0000-0000-0000000000f1', 'admin@example.com', 'admin', now());
insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents, label) values
 ('00000000-0000-0000-0000-00000000000a', '2026-09-01', '2026-09-05', 500, 'Promo');
insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents, fee_cents, label) values
 ('00000000-0000-0000-0000-00000000000a', '2026-09-14', '2026-09-18', 600, 0, 'Event week');
insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification) values
 ('c4000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000b1', '2026-09-02', 'post_paid'),
 ('c4000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000b1', '2026-09-08', 'post_paid'),
 ('c4000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000b1', '2026-09-09', 'post_paid'),
 ('c4000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000b1', '2026-09-15', 'post_paid');

create temp view b_ledger as
select service_date, meal_cents, fee_cents, price_cents from billing.v_check_in_ledger
 where student_id = '00000000-0000-0000-0000-0000000000b1';
create temp view b_opts as select * from billing.v_payment_options where student_id = '00000000-0000-0000-0000-0000000000b1';

select pg_temp.expect_eq((select string_agg(meal_cents || '+' || fee_cents || '=' || price_cents, ',' order by service_date) from b_ledger),
                         '500+0=500,790+0=790,790+0=790,600+0=600', 'default fee starts at $0: prices unchanged');

update billing.institutions set default_fee_cents = 35;
select pg_temp.expect_eq((select string_agg(meal_cents || '+' || fee_cents || '=' || price_cents, ',' order by service_date) from b_ledger),
                         '500+35=535,790+35=825,790+35=825,600+0=600',
                         'fee added on top; promo uses the default fee; a period can set its own fee');
select pg_temp.expect_eq((select string_agg(lunches || '=' || amount_cents, ',' order by lunches) from b_opts),
                         '1=535,2=1360,3=2185,4=2785', 'whole-lunch pay amounts include the fee');
select pg_temp.expect_error($q$ update billing.institutions set default_fee_cents = -1 $q$, '23514', 'negative fee refused');

-- a payment locks meal and fee together
select pg_temp.expect_eq(
    (select applied_cents from billing.record_offline_payment('00000000-0000-0000-0000-00000000000a',
        '00000000-0000-0000-0000-0000000000b1', 1360, 'cash', now(), '00000000-0000-0000-0000-0000000000f1', null)),
    1360::bigint, 'cash for 2 lunches applied');
select pg_temp.expect_eq((select locked_fee_cents from billing.check_in_billing where check_in_id = 'c4000000-0000-0000-0000-000000000002'),
                         35, 'fee locked with the price');
update billing.institutions set default_fee_cents = 50, default_price_cents = 800;
select pg_temp.expect_eq((select string_agg(meal_cents || '+' || fee_cents || '=' || price_cents, ',' order by service_date) from b_ledger),
                         '500+35=535,790+35=825,800+50=850,600+0=600', 'paid lunches keep meal and fee; unpaid follow the new ones');
select pg_temp.expect_error($q$ update billing.check_in_billing set locked_fee_cents = 0
                                where check_in_id = 'c4000000-0000-0000-0000-000000000002' $q$,
                            '23001', 'a locked fee cannot be edited');
select pg_temp.expect_error($q$ update billing.check_in_billing set locked_fee_cents = 10
                                where check_in_id = 'c4000000-0000-0000-0000-000000000003' $q$,
                            '23514', 'a fee cannot be locked without a locked price');

-- ---------------------------------------------------------------- Stripe
insert into billing.payment_intents (id, institution_id, student_id, amount_cents, processor_ref, lunches)
values ('e4000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-00000000000a',
        '00000000-0000-0000-0000-0000000000b1', 850, 'cs_test_1', 1);
select pg_temp.expect_error($q$ select * from billing.record_stripe_payment('e4000000-0000-0000-0000-000000000001', 'pi_1', 'card', 900) $q$,
                            '23514', 'Stripe amount must match the intent');
select pg_temp.expect_error($q$ select * from billing.record_stripe_payment('e4000000-0000-0000-0000-000000000001', 'pi_1', 'cash', 850) $q$,
                            '23514', 'Stripe payments are card or ach only');
select pg_temp.expect_eq((select created from billing.record_stripe_payment('e4000000-0000-0000-0000-000000000001', 'pi_1', 'card', 850)),
                         true, 'settled Stripe payment recorded');
select pg_temp.expect_eq((select created from billing.record_stripe_payment('e4000000-0000-0000-0000-000000000001', 'pi_1', 'card', 850)),
                         false, 'the same Stripe payment twice records nothing new');
select pg_temp.expect_eq((select count(*) from billing.payments where processor_ref = 'pi_1'), 1::bigint, 'exactly one payment row');
select pg_temp.expect_eq((select status || ':' || (payment_id is not null) from billing.payment_intents
                           where id = 'e4000000-0000-0000-0000-000000000001'), 'succeeded:true', 'intent marked succeeded');
select pg_temp.expect_eq((select string_agg(lunches || '=' || amount_cents, ',' order by lunches) from b_opts),
                         '1=600', 'the Sep 9 lunch is paid; only the event-week lunch is left');
select pg_temp.expect_error($q$ update billing.payment_intents set status = 'refunded' where processor_ref = 'cs_test_1' $q$,
                            '23514', 'intent statuses are limited');

insert into billing.stripe_events (id, type, outcome) values ('evt_1', 'checkout.session.completed', 'recorded');
select pg_temp.expect_error($q$ insert into billing.stripe_events (id, type, outcome) values ('evt_1', 'x', 'y') $q$,
                            '23505', 'a Stripe event is processed once');
select pg_temp.expect_error($q$ delete from billing.stripe_events $q$, '23001', 'Stripe event log is append-only');

rollback;
