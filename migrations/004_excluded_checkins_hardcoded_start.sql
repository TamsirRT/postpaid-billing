-- 004_excluded_checkins_hardcoded_start.sql
--
--   * check_ins.bill_separately = true means "do not include": the check-in is
--     classified 'excluded' and never billed.
--   * The billing start date is hard-coded in app/billing_rules.py, so the
--     institutions.billing_starts_on column from 003 is removed to keep one
--     source of truth.

alter table billing.check_in_billing drop constraint check_in_billing_classification_check;
alter table billing.check_in_billing add constraint check_in_billing_classification_check
    check (classification in ('post_paid', 'pre_ordered', 'duplicate', 'no_lunch', 'excluded'));

alter table billing.institutions drop column billing_starts_on;
