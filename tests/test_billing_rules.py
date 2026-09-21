"""The classification rules MealMode decided (Sep 21, 2026)."""
import unittest
from datetime import date

from app.billing_rules import BILLING_STARTS_ON, billing_start, classify_check_in

START = date(2026, 8, 31)


def classify(d="2026-09-15", getting_lunch=True, bill_separately=False, has_order=False, already=False, start=START):
    return classify_check_in(date.fromisoformat(d), getting_lunch, bill_separately, has_order, already, start)


class BillingRulesTests(unittest.TestCase):
    def test_sacred_heart_start_is_aug_31_2026(self):
        self.assertEqual(billing_start("sacred-heart"), date(2026, 8, 31))
        self.assertIsNone(billing_start("st-joseph"))
        self.assertEqual(set(BILLING_STARTS_ON), {"sacred-heart"})

    def test_ordinary_check_ins(self):
        self.assertEqual(classify(), ("post_paid", None))
        self.assertEqual(classify(has_order=True), ("pre_ordered", None))

    def test_start_date_boundary(self):
        self.assertEqual(classify("2026-08-31")[0], "post_paid")         # first day counts
        self.assertEqual(classify("2026-08-30"), (None, "before billing start date"))
        self.assertEqual(classify("2025-09-15"), (None, "before billing start date"))   # last year's history

    def test_school_without_start_date_bills_nothing(self):
        self.assertEqual(classify(start=None)[0], None)

    def test_no_lunch_is_never_billed(self):
        self.assertEqual(classify(getting_lunch=False)[0], "no_lunch")

    def test_bill_separately_means_not_included(self):
        self.assertEqual(classify(bill_separately=True),
                         ("excluded", "bill_separately = true (not included)"))
        self.assertEqual(classify(bill_separately=True, has_order=True)[0], "excluded")

    def test_second_check_in_same_day_is_duplicate(self):
        self.assertEqual(classify(already=True)[0], "duplicate")

    def test_rule_precedence(self):
        # before start beats everything; no-lunch beats bill_separately
        self.assertIsNone(classify("2026-08-01", getting_lunch=False, bill_separately=True)[0])
        self.assertEqual(classify(getting_lunch=False, bill_separately=True)[0], "no_lunch")

    def test_nullish_flags_from_the_database_default_to_billable(self):
        # getting_lunch / bill_separately may be NULL in check_ins; NULL is treated as the normal case
        self.assertEqual(classify(getting_lunch=None, bill_separately=None)[0], "post_paid")

    def test_delayed_check_ins_bill_normally(self):
        # Only check_in_date is an input; when the row was recorded can't change the outcome.
        import inspect
        params = inspect.signature(classify_check_in).parameters
        self.assertNotIn("check_in_time", params)
        self.assertEqual(classify("2026-09-02")[0], "post_paid")


if __name__ == "__main__":
    unittest.main()
