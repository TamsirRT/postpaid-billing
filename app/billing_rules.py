"""Billing rules decided by MealMode. Hard-coded on purpose: changing one is a
code change that gets reviewed, not a setting someone flips.

Phase 1's classifier calls `classify_check_in` for every check-in.
"""
from datetime import date

# Check-ins before this date are never billed. Aug 31, 2026 is the first
# check-in day of the 2026-27 school year (after the Jun 9 - Aug 31 summer gap).
BILLING_STARTS_ON = {
    "sacred-heart": date(2026, 8, 31),
}


def billing_start(slug):
    return BILLING_STARTS_ON.get(slug)


def classify_check_in(check_in_date, getting_lunch, bill_separately, has_order, already_billed_that_day, start):
    """Decide how one check-in is billed.

    Returns (classification, note), or (None, reason) if it must not be
    classified at all. Uses check_in_date only: when the row was recorded
    (check_in_time) doesn't matter, so late or early entries bill normally.

    Order matters; the first rule that applies wins.
    """
    if start is None:
        return None, "no billing start date for this school"
    if check_in_date < start:
        return None, "before billing start date"
    if getting_lunch is False:
        return "no_lunch", "getting_lunch = false"
    if bill_separately is True:
        return "excluded", "bill_separately = true (not included)"
    if already_billed_that_day:
        return "duplicate", "another check-in already counts for this child today"
    if has_order:
        return "pre_ordered", None
    return "post_paid", None
