"""Phase 1: orders import, matching, classification, review, rates, waivers, payments.

Importer tests run anywhere. The end-to-end tests drive the real web pages with
the real SQL against Postgres (set TEST_PG, see test_repo_sql.py).
"""
import datetime as dt
import io
import unittest
import uuid

from app import create_app
from app.importer import OrdersFileError, parse_orders_csv
from app.repo import Repo
from tests.fakes import FakeAuth
from tests.test_app import csrf_from
from tests.test_repo_sql import PsqlDatabase, fresh_database, needs_pg

SCHOOL, MODULE = "Sacred Heart School of Glyndon", "Order"
HEADER = ("Order ID,Order Date,Order or Refund,Line Type ,Location Name,Module Name,Product Amount,Product Name,"
          "User Name,User ID,User Group")


def orders_csv(lines):
    """lines: (order_id, date, kind, user_name, user_id[, location])"""
    out = [HEADER]
    for ln in lines:
        oid, d, kind, name, uid = ln[:5]
        loc = ln[5] if len(ln) > 5 else SCHOOL
        out.append(f"{oid},{d},{kind},line,{loc},{MODULE},1,Lunch,{name},{uid},3")
    return ("﻿" + "\n".join(out) + "\n").encode()


class ImporterTests(unittest.TestCase):
    def test_keeps_only_this_school_and_flags_every_line_of_a_refunded_order(self):
        data = orders_csv([
            ("o1", "2026-09-01", "order", "Ava Lopez", "u1"),
            ("o2", "2026-09-02", "order", "Ava Lopez", "u1"),
            ("o2", "2026-09-02", "refund", "Ava Lopez", "u1"),
            ("o3", "2026-09-02", "order", "Other Kid", "u9", "St. Joseph School Cockeysville"),
        ])
        res = parse_orders_csv(data, SCHOOL, MODULE)
        self.assertEqual(res["stats"]["school_rows"], 3)
        self.assertEqual(res["stats"]["other_schools_rows"], 1)
        refunded = {r["external_order_id"]: r["is_refunded"] for r in res["rows"]}
        self.assertEqual(refunded, {"o1": False, "o2": True})
        self.assertEqual(res["rows"][0]["name_key"], "ava|lopez")

    def test_same_file_gives_same_row_hashes(self):
        data = orders_csv([("o1", "2026-09-01", "order", "Ava Lopez", "u1")])
        a = parse_orders_csv(data, SCHOOL, MODULE)["rows"][0]["source_row_hash"]
        b = parse_orders_csv(data, SCHOOL, MODULE)["rows"][0]["source_row_hash"]
        self.assertEqual(a, b)

    def test_rejects_wrong_file_bad_dates_and_other_schools_only(self):
        with self.assertRaisesRegex(OrdersFileError, "Missing column"):
            parse_orders_csv(b"a,b\n1,2\n", SCHOOL, MODULE)
        with self.assertRaisesRegex(OrdersFileError, "isn't YYYY-MM-DD"):
            parse_orders_csv(orders_csv([("o1", "09/01/2026", "order", "Ava Lopez", "u1")]), SCHOOL, MODULE)
        with self.assertRaisesRegex(OrdersFileError, "No rows for"):
            parse_orders_csv(orders_csv([("o1", "2026-09-01", "order", "X Y", "u1", "Elsewhere")]), SCHOOL, MODULE)
        with self.assertRaisesRegex(OrdersFileError, "empty"):
            parse_orders_csv(b"", SCHOOL, MODULE)
        with self.assertRaisesRegex(OrdersFileError, "no User ID"):
            parse_orders_csv(orders_csv([("o1", "2026-09-01", "order", "Ava Lopez", "")]), SCHOOL, MODULE)


@needs_pg
class Phase1EndToEndTests(unittest.TestCase):
    """One school, walked through a realistic first two weeks of billing."""

    @classmethod
    def setUpClass(cls):
        cls.conninfo, cls.dbname, cls.db = fresh_database("phase1")
        cls.repo = Repo(cls.db)
        cls.repo.create_institution("sacred-heart", SCHOOL, SCHOOL, MODULE, dt.date(2026, 8, 31))
        cls.inst = cls.repo.get_institution("sacred-heart")
        cls.sid = {}
        for key, first, last in [("ava", "Ava", "Lopez"), ("ben", "Ben", "Lopez"), ("cal", "Cal", "Ng"),
                                 ("dee1", "Dee", "Smith"), ("dee2", "Dee", "Smith")]:
            cls.sid[key] = cls.db.fetch_one(
                "insert into public.students (first_name, last_name, grade_level, home_room, pin) "
                "values (%(f)s, %(l)s, '3', 'Room 1', '1234') returning id", {"f": first, "l": last})["id"]
        cls.check_in = {}

        def ci(name, key, d, lunch=True, bill_sep=False, time="12:00"):
            cid = str(uuid.uuid4())
            cls.db.execute("insert into public.check_ins (id, student_id, check_in_date, getting_lunch, check_in_time, "
                           "bill_separately) values (%(id)s, %(s)s, %(d)s, %(l)s, %(t)s, %(b)s)",
                           {"id": cid, "s": cls.sid[key] if key else str(uuid.uuid4()), "d": d, "l": lunch,
                            "t": f"{d} {time}-04", "b": bill_sep})
            cls.check_in[name] = cid

        ci("ava_aug28", "ava", "2026-08-28")                    # before billing start
        ci("ava_sep1", "ava", "2026-09-01")                     # has order -> pre_ordered
        ci("ava_sep2", "ava", "2026-09-02")                     # no order -> post_paid
        ci("ava_sep2_again", "ava", "2026-09-02", time="12:30") # second check-in -> duplicate
        ci("ava_sep3", "ava", "2026-09-03", lunch=False)        # no lunch
        ci("ava_sep4", "ava", "2026-09-04", bill_sep=True)      # bill separately -> excluded
        ci("ava_sep8", "ava", "2026-09-08")                     # order later refunded -> post_paid
        ci("ava_sep25", "ava", "2026-09-25")                    # after the export -> not yet
        ci("ben_sep2", "ben", "2026-09-02")                     # sibling, same parent login, has order
        ci("cal_sep2", "cal", "2026-09-02")                     # post_paid; order arrives in a later import
        ci("dee1_sep2", "dee1", "2026-09-02")                   # ambiguous name on the order
        ci("ghost_sep2", None, "2026-09-02")                    # student_id not in students

        cls.auth = FakeAuth()
        cls.app = create_app({"TESTING": True, "SECRET_KEY": "t", "SESSION_COOKIE_SECURE": False},
                             repo=cls.repo, auth=cls.auth)
        cls.users = {}
        for role in ("viewer", "admin", "super_admin"):
            email = f"{role}@mealmode.test"
            uid = cls.auth.add(email, "long-enough-password")
            cls.repo.touch_staff(uid, email)
            cls.repo.set_role(uid, role, None, "setup")
            cls.users[role] = email

    @classmethod
    def tearDownClass(cls):
        PsqlDatabase(cls.conninfo, "postgres").execute(f"drop database if exists {cls.dbname} with (force)")

    # ------------------------------------------------------------ helpers
    def client_as(self, role):
        c = self.app.test_client()
        token = csrf_from(c.get("/login").get_data(as_text=True))
        c.post("/login", data={"email": self.users[role], "password": "long-enough-password", "csrf_token": token})
        return c

    def token(self, c, path="/"):
        return csrf_from(c.get(path).get_data(as_text=True))

    def upload(self, c, lines, exported_at="2026-09-24T08:00", name="ALL ORDERS.csv"):
        return c.post("/orders", data={"csrf_token": self.token(c, "/orders"), "exported_at": exported_at,
                                       "file": (io.BytesIO(orders_csv(lines)), name)},
                      content_type="multipart/form-data", follow_redirects=True)

    def cls_of(self, name):
        row = self.db.fetch_one("select classification from billing.check_in_billing where check_in_id = %(c)s",
                                {"c": self.check_in[name]})
        return row["classification"] if row else None

    def price_of(self, name):
        return int(self.db.fetch_one("select price_cents from billing.v_check_in_ledger where check_in_id = %(c)s",
                                     {"c": self.check_in[name]})["price_cents"])

    # ------------------------------------------------------------ the walk-through (ordered steps)
    def test_1_sorting_before_any_orders_is_refused(self):
        c = self.client_as("admin")
        html = c.post("/classify", data={"csrf_token": self.token(c)}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Import an orders export first", html)
        self.assertEqual(self.db.fetch_one("select count(*) as n from billing.check_in_billing")["n"], "0")

    def test_2_viewer_cannot_upload(self):
        c = self.client_as("viewer")
        resp = c.post("/orders", data={"csrf_token": self.token(c, "/orders"), "exported_at": "2026-09-24T08:00",
                                       "file": (io.BytesIO(orders_csv([])), "x.csv")},
                      content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 403)

    def test_3_bad_uploads_are_explained_and_import_nothing(self):
        c = self.client_as("admin")
        html = self.upload(c, [("o1", "2026-09-01", "order", "Ava Lopez", "u1")],
                           exported_at="2099-01-01T08:00").get_data(as_text=True)
        self.assertIn("can&#39;t be in the future", html)
        html = c.post("/orders", data={"csrf_token": self.token(c, "/orders"), "exported_at": "2026-09-24T08:00",
                                       "file": (io.BytesIO(b"not,the,right\n1,2,3\n"), "wrong.csv")},
                      content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Missing column", html)
        self.assertEqual(self.db.fetch_one("select count(*) as n from billing.import_batches")["n"], "0")

    def test_4_first_upload_sorts_everything(self):
        c = self.client_as("admin")
        html = self.upload(c, [
            ("o1", "2026-09-01", "order", "Ava Lopez", "parent-lopez"),
            ("o2", "2026-09-02", "order", "Ben Lopez", "parent-lopez"),     # same parent login, other child
            ("o3", "2026-09-08", "order", "Ava Lopez", "parent-lopez"),
            ("o4", "2026-09-02", "order", "Dee Smith", "parent-smith"),     # two Dee Smiths -> review
            ("o5", "2026-09-02", "order", "Zed Unknown", "parent-z"),      # nobody -> review
            ("o6", "2026-10-15", "order", "Ava Lopez", "parent-lopez"),     # future pre-order: fine
        ]).get_data(as_text=True)
        self.assertIn("Classified new check-ins", html)
        self.assertEqual(self.cls_of("ava_aug28"), None)          # before billing start
        self.assertEqual(self.cls_of("ava_sep1"), "pre_ordered")
        self.assertEqual(self.cls_of("ava_sep2"), "post_paid")
        self.assertEqual(self.cls_of("ava_sep2_again"), "duplicate")
        self.assertEqual(self.cls_of("ava_sep3"), "no_lunch")
        self.assertEqual(self.cls_of("ava_sep4"), "excluded")
        self.assertEqual(self.cls_of("ava_sep8"), "pre_ordered")
        self.assertEqual(self.cls_of("ava_sep25"), None)          # export doesn't cover it yet
        self.assertEqual(self.cls_of("ben_sep2"), "pre_ordered")  # sibling matched under the parent login
        self.assertEqual(self.cls_of("cal_sep2"), "post_paid")
        self.assertEqual(self.cls_of("dee1_sep2"), "post_paid")   # her order is stuck in review for now
        self.assertEqual(self.price_of("ava_sep2"), 790)
        reasons = sorted(r["reason"] for r in self.repo.open_review_items(self.inst["id"]))
        self.assertEqual(reasons, ["Check-in is for a student_id that is not in the students table",
                                   "Name matches 2 students", "No student in the roster has this name"])

    def test_5_uploading_the_same_file_again_changes_nothing(self):
        before = self.db.fetch_one("select count(*) as n from billing.check_in_billing")["n"]
        c = self.client_as("admin")
        html = self.upload(c, [("o1", "2026-09-01", "order", "Ava Lopez", "parent-lopez")]).get_data(as_text=True)
        self.assertIn("0 new, 1 already on file", html)
        self.assertEqual(self.db.fetch_one("select count(*) as n from billing.check_in_billing")["n"], before)
        self.assertEqual(len(self.repo.open_review_items(self.inst["id"])), 3)

    def test_6_resolving_a_review_item_rebills_that_day(self):
        c = self.client_as("admin")
        html = c.get("/review").get_data(as_text=True)
        self.assertIn("Dee Smith", html)
        item = next(i for i in self.repo.open_review_items(self.inst["id"]) if i["reason"].startswith("Name matches"))
        html = c.post(f"/review/{item['id']}/match", data={"csrf_token": self.token(c, "/review"),
                                                           "student_id": self.sid["dee1"]},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn("1 order line(s) now belong to that student", html)
        self.assertEqual(self.cls_of("dee1_sep2"), "pre_ordered")

    def test_7_later_import_brings_a_late_order_and_a_refund(self):
        c = self.client_as("admin")
        self.upload(c, [
            ("o7", "2026-09-02", "order", "Cal Ng", "parent-ng"),          # Cal's order shows up late
            ("o3", "2026-09-08", "refund", "Ava Lopez", "parent-lopez"),    # Ava's Sep 8 order refunded
        ], name="ALL ORDERS (2).csv")
        self.assertEqual(self.cls_of("cal_sep2"), "pre_ordered")
        self.assertEqual(self.cls_of("ava_sep8"), "post_paid")

    def test_8_rates_default_and_promo_period(self):
        admin = self.client_as("admin")
        self.assertEqual(admin.post("/rates/default", data={"csrf_token": self.token(admin, "/rates"),
                                                            "price": "9.00"}).status_code, 403)
        c = self.client_as("super_admin")
        html = c.post("/rates/default", data={"csrf_token": self.token(c, "/rates"), "price": "$8.50"},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn("from $7.90 to $8.50", html)
        self.assertEqual(self.price_of("ava_sep2"), 850)
        html = c.post("/rates/periods", data={"csrf_token": self.token(c, "/rates"), "starts_on": "2026-08-31",
                                              "ends_on": "2026-09-04", "price": "5", "label": "Back-to-school"},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn("Added $5.00", html)
        self.assertEqual(self.price_of("ava_sep2"), 500)
        self.assertEqual(self.price_of("ava_sep8"), 850)
        resp = c.post("/rates/periods", data={"csrf_token": self.token(c, "/rates"), "starts_on": "2026-09-03",
                                              "ends_on": "2026-09-10", "price": "6.00"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("overlaps the existing period", resp.get_data(as_text=True))
        resp = c.post("/rates/default", data={"csrf_token": self.token(c, "/rates"), "price": "7.9O"},
                      follow_redirects=True)
        self.assertIn("Enter a price like 7.90", resp.get_data(as_text=True))

    def test_9_payment_waiver_and_credit(self):
        c = self.client_as("admin")
        ava = self.sid["ava"]
        # Ava owes: Sep 2 $5.00 (promo) + Sep 8 $8.50 = $13.50
        html = c.post(f"/students/{ava}/payments", data={
            "csrf_token": self.token(c, f"/students/{ava}"), "amount": "10.00", "method": "check",
            "received_on": "2026-09-23", "note": "check #1041"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Recorded $10.00 (check). $10.00 applied", html)
        self.assertEqual(self.db.fetch_one("select balance_due_cents from billing.v_student_balances where student_id = %(s)s",
                                           {"s": ava})["balance_due_cents"], "350")
        # promo period now has a paid lunch: can't be removed
        period = self.repo.rate_periods(self.inst["id"])[0]
        self.assertEqual(period["locked_lunches"], "1")
        sa = self.client_as("super_admin")
        html = sa.post(f"/rates/periods/{period['id']}/delete", data={"csrf_token": self.token(sa, "/rates")},
                       follow_redirects=True).get_data(as_text=True)
        self.assertIn("can&#39;t be removed", html)
        # waive the $5 lunch: its $5 moves onto Sep 8, which becomes fully paid, leaving $1.50 credit
        html = c.post(f"/students/{ava}/lunches/{self.check_in['ava_sep2']}/waive", data={
            "csrf_token": self.token(c, f"/students/{ava}"), "reason": "Field trip"}, follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("The $5.00 already paid toward it was released: $3.50 went to other unpaid lunches and "
                      "$1.50 is kept as credit for future lunches.", html)
        bal = self.db.fetch_one("select balance_due_cents, credit_cents from billing.v_student_balances "
                                "where student_id = %(s)s", {"s": ava})
        self.assertEqual((bal["balance_due_cents"], bal["credit_cents"]), ("-150", "150"))
        # the student page shows it all, and never the PIN
        page = c.get(f"/students/{ava}").get_data(as_text=True)
        for text in ("Pre-ordered", "No lunch taken", "Not included", "Duplicate check-in", "Waived",
                     "Field trip", "check #1041", "Credit waiting for future lunches"):
            self.assertIn(text, page)
        self.assertNotIn("1234", page)

    def test_9a_reversing_a_mistaken_payment(self):
        c = self.client_as("admin")
        cal = self.sid["cal"]
        # Cal owes nothing (his Sep 2 was re-sorted as pre-ordered); a payment becomes pure credit
        c.post(f"/students/{cal}/payments", data={"csrf_token": self.token(c, f"/students/{cal}"), "amount": "20",
                                                  "method": "cash", "received_on": "2026-09-23"})
        pid = self.repo.student_payments(self.inst["id"], cal)[0]["id"]
        self.assertEqual(self.db.fetch_one("select credit_cents from billing.v_student_balances where student_id = %(s)s",
                                           {"s": cal})["credit_cents"], "2000")
        self.assertEqual(c.post(f"/students/{cal}/payments/{pid}/reverse",
                                data={"csrf_token": self.token(c, f"/students/{cal}"), "reason": ""},
                                follow_redirects=True).status_code, 200)
        self.assertEqual(self.repo.student_payments(self.inst["id"], cal)[0]["reversed_reason"], None)
        html = c.post(f"/students/{cal}/payments/{pid}/reverse",
                      data={"csrf_token": self.token(c, f"/students/{cal}"), "reason": "Entered on the wrong child"},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn("Payment reversed", html)
        self.assertIn("Reversed: Entered on the wrong child", html)
        # no billed lunches and no live payments left: no balance row at all, i.e. $0
        self.assertIsNone(self.db.fetch_one("select credit_cents from billing.v_student_balances where student_id = %(s)s",
                                            {"s": cal}))
        html = c.post(f"/students/{cal}/payments/{pid}/reverse",
                      data={"csrf_token": self.token(c, f"/students/{cal}"), "reason": "again"},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn("can&#39;t be reversed here", html)

    def test_9b_payment_form_validation(self):
        c = self.client_as("admin")
        ava = self.sid["ava"]
        html = c.post(f"/students/{ava}/payments", data={
            "csrf_token": self.token(c, f"/students/{ava}"), "amount": "abc", "method": "card",
            "received_on": "2099-01-01"}, follow_redirects=True).get_data(as_text=True)
        for msg in ("Enter an amount", "Choose how it was paid", "can&#39;t be in the future"):
            self.assertIn(msg, html)
        viewer = self.client_as("viewer")
        self.assertEqual(viewer.post(f"/students/{ava}/payments", data={
            "csrf_token": self.token(viewer, f"/students/{ava}"), "amount": "5", "method": "cash",
            "received_on": "2026-09-23"}).status_code, 403)

    def test_z_pages_render_and_everything_was_audited(self):
        c = self.client_as("viewer")
        for path in ("/", "/orders", "/review", "/rates", "/students", f"/students/{self.sid['cal']}"):
            self.assertEqual(c.get(path).status_code, 200, path)
        actions = {r["action"] for r in self.db.fetch_all("select distinct action from billing.audit_log")}
        for a in ("import_orders", "classification_run", "resolve_review_match", "set_default_price",
                  "add_rate_period", "record_payment", "waive_lunch", "reverse_payment"):
            self.assertIn(a, actions)
        run = self.repo.last_run(self.inst["id"])
        self.assertEqual(run["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
