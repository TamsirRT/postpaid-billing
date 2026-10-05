-- 010_refund_review.sql
--
-- A child who checks in on a day whose order was refunded is no longer billed
-- automatically. The export doesn't say why an order was refunded:
--   * the parent cancelled and got their money back, but the child ate anyway
--     -> the lunch is unpaid and should be billed;
--   * MealMode refunded for its own mistake -> the family was already made
--     whole, and billing them would charge twice.
-- So the check-in is held ('refund_hold', not billed) and a review item asks
-- staff to choose: bill it (-> post_paid) or don't (-> 'refunded', never billed).

alter table billing.check_in_billing drop constraint check_in_billing_classification_check;
alter table billing.check_in_billing add constraint check_in_billing_classification_check
    check (classification in ('post_paid', 'pre_ordered', 'duplicate', 'no_lunch', 'excluded',
                              'refund_hold', 'refunded'));

alter table billing.review_items drop constraint review_items_source_check;
alter table billing.review_items add constraint review_items_source_check
    check (source in ('order', 'check_in', 'late_order', 'integrity', 'refund'));
