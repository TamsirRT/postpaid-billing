"""Parent payments: whole lunches, oldest first (app/payments.py + the real SQL)."""
import datetime as dt
import unittest

from app.payments import PaymentAmountError, lunches_for_amount, option_for_lunches
from app.repo import Repo
from tests.test_repo_sql import PsqlDatabase, fresh_database, needs_pg

OPTS = [{"lunches": 1, "amount_cents": 500}, {"lunches": 2, "amount_cents": 1290}, {"lunches": 3, "amount_cents": 2080}]


class RuleTests(unittest.TestCase):
    def test_allowed_amounts_map_to_lunch_counts(self):
        self.assertEqual(lunches_for_amount(OPTS, 1290), 2)
        self.assertEqual(option_for_lunches(OPTS, 3), 2080)

    def test_anything_else_is_refused_with_a_parent_friendly_message(self):
        for bad in (790, 1000, 0, 99999):
            with self.assertRaisesRegex(PaymentAmountError, "Reload the page"):
                lunches_for_amount(OPTS, bad)
        with self.assertRaisesRegex(PaymentAmountError, "nothing to pay"):
            lunches_for_amount([], 790)
        with self.assertRaises(PaymentAmountError):
            option_for_lunches(OPTS, 4)


@needs_pg
class PaymentOptionsSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conninfo, cls.dbname, cls.db = fresh_database("payopts")
        cls.repo = Repo(cls.db)
        cls.iid = cls.repo.create_institution("sacred-heart", "SH", "SH", "Order", dt.date(2026, 8, 31))["id"]
        cls.db.execute("insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents, label) "
                       "values (%(i)s, '2026-09-01', '2026-09-05', 500, 'Promo')", {"i": cls.iid})
        cls.sid = "00000000-0000-0000-0000-0000000000b7"
        for d in ("2026-09-02", "2026-09-08"):
            cls.db.execute("insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, "
                           "classification) values (gen_random_uuid(), %(i)s, %(s)s, %(d)s, 'post_paid')",
                           {"i": cls.iid, "s": cls.sid, "d": d})

    @classmethod
    def tearDownClass(cls):
        PsqlDatabase(cls.conninfo, "postgres").execute(f"drop database if exists {cls.dbname} with (force)")

    def test_options_come_from_the_database_oldest_first(self):
        opts = self.repo.payment_options(self.iid, self.sid)
        self.assertEqual([(o["lunches"], o["amount_cents"], o["rate_label"]) for o in opts],
                         [("1", "500", "Promo"), ("2", "1290", "Standard rate")])
        self.assertEqual(lunches_for_amount(opts, 1290), 2)
