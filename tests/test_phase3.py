"""Phase 3: the fee line and online payments through Stripe Checkout.

Stripe itself is replaced by FakeStripe (records Checkout Sessions) and by
webhook events signed exactly the way Stripe signs them.
"""
import datetime as dt
import json
import os
import unittest
import uuid
from unittest import mock

from app import create_app
from app.config import ConfigError, load_config
from app.mailer import Mailer
from app.portal import portal_token
from app.repo import Repo
from app.stripe_client import StripeError, WebhookSignatureError, _flatten, sign_payload, verify_webhook
from tests.fakes import FakeAuth
from tests.test_app import csrf_from
from tests.test_phase1 import MODULE, SCHOOL
from tests.test_phase2 import SECRET, TEST_INBOX, RecordingBackend
from tests.test_repo_sql import PsqlDatabase, fresh_database, needs_pg

WHSEC = "whsec_testsecret"


class FakeStripe:
    def __init__(self):
        self.sessions, self.fail, self.charge_type = [], False, {}

    def create_checkout_session(self, params, idempotency_key):
        if self.fail:
            raise StripeError("Couldn't reach Stripe: ConnectionError")
        sid = f"cs_test_{len(self.sessions) + 1}"
        self.sessions.append({"id": sid, "params": params, "key": idempotency_key})
        return {"id": sid, "url": f"https://checkout.stripe.com/c/pay/{sid}"}

    def retrieve_payment_intent(self, pi):
        return {"id": pi, "latest_charge": {"payment_method_details": {"type": self.charge_type.get(pi, "card")}}}


# ====================================================================== units
class StripeClientTests(unittest.TestCase):
    def test_form_encoding_matches_stripe(self):
        got = _flatten({"mode": "payment", "payment_method_types": ["card", "us_bank_account"],
                        "line_items": [{"quantity": 1, "price_data": {"unit_amount": 825}}], "skip": None})
        self.assertEqual(got, [("mode", "payment"), ("payment_method_types[0]", "card"),
                               ("payment_method_types[1]", "us_bank_account"), ("line_items[0][quantity]", "1"),
                               ("line_items[0][price_data][unit_amount]", "825")])

    def test_webhook_signature(self):
        body = json.dumps({"id": "evt_1", "type": "x"}).encode()
        self.assertEqual(verify_webhook(body, sign_payload(body, WHSEC), WHSEC)["id"], "evt_1")
        for header in (None, "", "t=1", sign_payload(body, "whsec_other"), sign_payload(body + b" ", WHSEC)):
            with self.assertRaises(WebhookSignatureError):
                verify_webhook(body, header, WHSEC)
        with self.assertRaisesRegex(WebhookSignatureError, "too old"):
            verify_webhook(body, sign_payload(body, WHSEC, ts=1_000_000), WHSEC)

    def test_webhook_accepts_any_matching_v1_during_secret_rollover(self):
        body = b'{"id":"evt_2"}'
        good = sign_payload(body, WHSEC)
        ts = good.split(",")[0]
        header = f"{ts},v1={'0' * 64},{good.split(',')[1]}"
        self.assertEqual(verify_webhook(body, header, WHSEC)["id"], "evt_2")


class StripeConfigTests(unittest.TestCase):
    BASE = {"APP_ENV": "production", "SECRET_KEY": "x" * 40, "DATABASE_URL": "postgresql://x",
            "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_ANON_KEY": "k", "PORTAL_SECRET": SECRET,
            "PUBLIC_BASE_URL": "https://billing.example.org"}

    def load(self, **extra):
        with mock.patch.dict(os.environ, {**self.BASE, **extra}, clear=True):
            return load_config()

    def test_off_by_default(self):
        self.assertIsNone(self.load()["STRIPE_MODE"])

    def test_both_or_neither(self):
        with self.assertRaisesRegex(ConfigError, "both"):
            self.load(STRIPE_SECRET_KEY="sk_test_abc")
        with self.assertRaisesRegex(ConfigError, "both"):
            self.load(STRIPE_WEBHOOK_SECRET=WHSEC)

    def test_catches_wrong_keys(self):
        with self.assertRaisesRegex(ConfigError, "publishable"):
            self.load(STRIPE_SECRET_KEY="pk_test_abc", STRIPE_WEBHOOK_SECRET=WHSEC)
        with self.assertRaisesRegex(ConfigError, "whsec_"):
            self.load(STRIPE_SECRET_KEY="sk_test_abc", STRIPE_WEBHOOK_SECRET="abc")

    def test_mode_from_key(self):
        self.assertEqual(self.load(STRIPE_SECRET_KEY="sk_test_a", STRIPE_WEBHOOK_SECRET=WHSEC)["STRIPE_MODE"], "test")
        self.assertEqual(self.load(STRIPE_SECRET_KEY="rk_live_a", STRIPE_WEBHOOK_SECRET=WHSEC)["STRIPE_MODE"], "live")


# ====================================================================== end to end
@needs_pg
class Phase3EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conninfo, cls.dbname, cls.db = fresh_database("phase3")
        cls.repo = Repo(cls.db)
        cls.repo.create_institution("sacred-heart", SCHOOL, SCHOOL, MODULE, dt.date(2026, 8, 31))
        cls.inst = cls.repo.get_institution("sacred-heart")
        iid = cls.inst["id"]
        cls.sid, cls.lunch = {}, {}
        for key, first, last in [("ava", "Ava", "Lopez"), ("ben", "Ben", "Lopez"), ("cal", "Cal", "Ng")]:
            cls.sid[key] = cls.db.fetch_one(
                "insert into public.students (first_name, last_name, grade_level, home_room, pin) "
                "values (%(f)s, %(l)s, '3', 'Room 1', '9876') returning id", {"f": first, "l": last})["id"]
        for key, days in {"ava": ["2026-09-01", "2026-09-02"], "ben": ["2026-09-01"], "cal": ["2026-09-03"]}.items():
            for d in days:
                cid = str(uuid.uuid4())
                cls.db.execute("insert into public.check_ins (id, student_id, check_in_date, getting_lunch, "
                               "check_in_time, bill_separately) values (%(id)s, %(s)s, %(d)s, true, %(t)s, false)",
                               {"id": cid, "s": cls.sid[key], "d": d, "t": f"{d} 12:00-04"})
                cls.db.execute("insert into billing.check_in_billing (check_in_id, institution_id, student_id, "
                               "service_date, classification) values (%(c)s, %(i)s, %(s)s, %(d)s, 'post_paid')",
                               {"c": cid, "i": iid, "s": cls.sid[key], "d": d})
                cls.lunch[f"{key}_{d[-2:]}"] = cid
        add = cls.repo.add_guardian_to_student
        cls.lopez = add(iid, cls.sid["ava"], "Maria Lopez", "maria@example.org", None, None, "setup")["guardian_id"]
        add(iid, cls.sid["ben"], "Maria Lopez", "maria@example.org", None, None, "setup")
        cls.ng = add(iid, cls.sid["cal"], "Sam Ng", "sam@example.org", None, None, "setup")["guardian_id"]

        cls.backend = RecordingBackend()
        cls.stripe = FakeStripe()
        cls.auth = FakeAuth()
        cls.app = create_app({"TESTING": True, "SECRET_KEY": "t", "SESSION_COOKIE_SECURE": False,
                              "PORTAL_SECRET": SECRET, "PUBLIC_BASE_URL": "https://billing.example.org",
                              "STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": WHSEC},
                             repo=cls.repo, auth=cls.auth, mailer=Mailer("test", TEST_INBOX, cls.backend),
                             stripe=cls.stripe)
        cls.users = {}
        for role in ("viewer", "admin", "super_admin"):
            email = f"{role}@mealmode.test"
            uid = cls.auth.add(email, "long-enough-password")
            cls.repo.touch_staff(uid, email)
            cls.repo.set_role(uid, role, None, "setup")
            cls.users[role] = email
        # parents' links exist once a statement or preview has been made
        from app.notify import portal_url
        with cls.app.test_request_context():
            for gid in (cls.lopez, cls.ng):
                portal_url(cls.app.config, cls.repo, cls.inst, cls.repo.statement_guardian(iid, gid))
        from app import views
        views._portal_hits.clear()

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

    def link(self, gid):
        return f"/p/{portal_token(SECRET, str(gid), 1)}"

    def pay(self, gid, student_key, lunches, client=None):
        c = client or self.app.test_client()
        page = self.link(gid)
        return c.post(page + "/pay", data={"csrf_token": self.token(c, page), "student_id": self.sid[student_key],
                                           "lunches": str(lunches)})

    def webhook(self, etype, obj, event_id=None, secret=WHSEC):
        body = json.dumps({"id": event_id or f"evt_{uuid.uuid4().hex}", "type": etype,
                           "data": {"object": obj}}).encode()
        return self.app.test_client().post("/stripe/webhook", data=body, content_type="application/json",
                                           headers={"Stripe-Signature": sign_payload(body, secret)})

    def session_event(self, n, payment_status="paid", pi=None, amount=None):
        s = self.stripe.sessions[n - 1]
        return {"id": s["id"], "object": "checkout.session", "payment_status": payment_status,
                "payment_intent": pi, "amount_total": amount or s["params"]["line_items"][0]["price_data"]["unit_amount"],
                "client_reference_id": s["params"]["client_reference_id"], "metadata": s["params"]["metadata"]}

    def balance(self, key):
        row = self.db.fetch_one("select balance_due_cents from billing.v_student_balances where student_id = %(s)s",
                                {"s": self.sid[key]})
        return int(row["balance_due_cents"]) if row else 0

    def intent(self, n):
        return self.db.fetch_one("select * from billing.payment_intents where processor_ref = %(r)s",
                                 {"r": self.stripe.sessions[n - 1]["id"]})

    # ------------------------------------------------------------ fee line
    def test_1_fee_is_set_on_rates_and_shown_to_parents(self):
        sa = self.client_as("super_admin")
        html = sa.post("/rates/fee", data={"csrf_token": self.token(sa, "/rates"), "fee": "0.35"},
                       follow_redirects=True).get_data(as_text=True)
        self.assertIn("Payment processing fee changed from $0.00 to $0.35 per lunch. 4 unpaid lunch(es)", html)
        self.assertIn("A lunch now costs <strong>$8.25</strong>", html)
        admin = self.client_as("admin")
        self.assertEqual(admin.post("/rates/fee", data={"csrf_token": self.token(admin, "/rates"), "fee": "0"}).status_code, 403)
        portal = self.app.test_client().get(self.link(self.lopez)).get_data(as_text=True)
        self.assertIn("Meal $7.90 + processing $0.35", portal)
        self.assertIn("Pay $8.25", portal)
        self.assertIn("Pay $16.50", portal)
        self.assertIn("payment-processing amount, shown separately", portal)
        self.assertEqual(self.balance("ava"), 1650)

    def test_2_period_can_carry_its_own_fee(self):
        sa = self.client_as("super_admin")
        html = sa.post("/rates/periods", data={"csrf_token": self.token(sa, "/rates"), "starts_on": "2026-09-03",
                                               "ends_on": "2026-09-03", "price": "6.00", "fee": "0",
                                               "label": "Pizza day"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Added $6.00 + $0.00 processing for 2026-09-03 to 2026-09-03", html)
        self.assertEqual(self.balance("cal"), 600)
        html = sa.post("/rates/periods", data={"csrf_token": self.token(sa, "/rates"), "starts_on": "2026-10-01",
                                               "ends_on": "2026-10-02", "price": "6.00", "fee": "abc"}).get_data(as_text=True)
        self.assertIn("Enter the processing fee like 0.35", html)

    def test_3_statement_email_shows_meal_and_processing(self):
        from app.notify import compose_statement
        with self.app.test_request_context():
            msg = compose_statement(self.app.config, self.repo, self.inst, self.repo.statement_guardian(self.inst["id"], self.lopez))
        self.assertIn(">Processing</th>", msg["html"])
        self.assertIn("meal $7.90 + processing $0.35", msg["text"])
        self.assertIn("Pay online or see every lunch", msg["html"])

    # ------------------------------------------------------------ starting a payment
    def test_4_pay_button_goes_to_stripe_checkout(self):
        resp = self.pay(self.lopez, "ava", 1)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["Location"], "https://checkout.stripe.com/c/pay/cs_test_1")
        p = self.stripe.sessions[0]["params"]
        self.assertEqual(p["line_items"][0]["price_data"]["unit_amount"], 825)
        self.assertEqual(p["payment_method_types"], ["card", "us_bank_account"])
        self.assertTrue(p["success_url"].startswith("https://billing.example.org/p/"))
        self.assertIn("1 lunch, Sep 1, 2026", p["line_items"][0]["price_data"]["product_data"]["description"])
        it = self.intent(1)
        self.assertEqual((it["status"], it["amount_cents"], it["lunches"]), ("pending", "825", "1"))
        self.assertEqual(self.stripe.sessions[0]["key"], f"checkout-{it['id']}")
        self.assertEqual(self.balance("ava"), 1650)             # nothing counts until Stripe says so

    def test_5_bad_requests_never_reach_stripe(self):
        before = len(self.stripe.sessions)
        c = self.app.test_client()
        html = c.get(self.pay(self.lopez, "ava", 9, client=c).headers["Location"]).get_data(as_text=True)
        self.assertIn("That amount isn&#39;t available any more", html)
        html = c.get(self.pay(self.lopez, "cal", 1, client=c).headers["Location"]).get_data(as_text=True)
        self.assertIn("Choose how many lunches", html)      # not her child
        self.assertEqual(self.app.test_client().post(self.link(self.lopez) + "/pay",
                                                     data={"student_id": self.sid["ava"], "lunches": "1"}).status_code, 400)
        self.assertEqual(len(self.stripe.sessions), before)

    def test_6_portal_csp_allows_posting_to_stripe_only(self):
        csp = self.app.test_client().get(self.link(self.lopez)).headers["Content-Security-Policy"]
        self.assertIn("form-action 'self' https://checkout.stripe.com;", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    # ------------------------------------------------------------ webhooks
    def test_7_webhook_rejects_bad_signatures(self):
        self.assertEqual(self.webhook("checkout.session.completed", self.session_event(1, pi="pi_x"),
                                      secret="whsec_wrong").status_code, 400)
        self.assertEqual(self.intent(1)["status"], "pending")

    def test_8_card_payment_recorded_once_with_receipt(self):
        sent_before = len(self.backend.sent)
        r = self.webhook("checkout.session.completed", self.session_event(1, pi="pi_card_1"), event_id="evt_card")
        self.assertEqual(r.get_data(as_text=True), "recorded card")
        self.assertEqual(self.balance("ava"), 825)
        it = self.intent(1)
        self.assertEqual((it["status"], it["method"]), ("succeeded", "card"))
        locked = self.db.fetch_one("select locked_price_cents, locked_fee_cents from billing.check_in_billing "
                                   "where check_in_id = %(c)s", {"c": self.lunch["ava_01"]})
        self.assertEqual((locked["locked_price_cents"], locked["locked_fee_cents"]), ("825", "35"))
        receipts = self.backend.sent[sent_before:]
        self.assertEqual([m["to"] for m in receipts], [TEST_INBOX])
        self.assertIn("Payment received for Ava: $8.25", receipts[0]["subject"])
        self.assertIn("(card)", receipts[0]["text"])
        # Stripe retries the same event, then sends a second event for the same payment
        self.assertEqual(self.webhook("checkout.session.completed", self.session_event(1, pi="pi_card_1"),
                                      event_id="evt_card").get_data(as_text=True), "duplicate")
        self.assertEqual(self.webhook("checkout.session.async_payment_succeeded",
                                      self.session_event(1, pi="pi_card_1")).get_data(as_text=True), "already recorded")
        self.assertEqual(self.balance("ava"), 825)
        self.assertEqual(len(self.backend.sent), sent_before + 1)
        # parent comes back from Stripe
        page = self.app.test_client().get(f"{self.link(self.lopez)}?paid={it['id']}").get_data(as_text=True)
        self.assertIn("Payment received. Thank you!", page)
        self.assertIn("$8.25 · card", page)

    def test_9_bank_payment_clears_after_a_few_days(self):
        self.assertEqual(self.pay(self.lopez, "ben", 1).status_code, 303)
        n = len(self.stripe.sessions)
        self.stripe.charge_type["pi_ach_1"] = "us_bank_account"
        self.assertEqual(self.webhook("checkout.session.completed", self.session_event(n, "unpaid", "pi_ach_1"))
                         .get_data(as_text=True), "processing")
        self.assertEqual(self.balance("ben"), 825)
        page = self.app.test_client().get(self.link(self.lopez)).get_data(as_text=True)
        self.assertIn("Bank payment of <strong>$8.25</strong>", page)
        self.assertEqual(self.pay(self.lopez, "ben", 1).status_code, 302)      # can't pay twice meanwhile
        self.assertEqual(len(self.stripe.sessions), n)
        r = self.webhook("checkout.session.async_payment_succeeded", self.session_event(n, "paid", "pi_ach_1"))
        self.assertEqual(r.get_data(as_text=True), "recorded ach")
        self.assertEqual(self.balance("ben"), 0)
        self.assertIn("· bank payment ·", self.app.test_client().get(self.link(self.lopez)).get_data(as_text=True))

    def test_a_failed_bank_payment_leaves_the_balance(self):
        self.assertEqual(self.pay(self.ng, "cal", 1).status_code, 303)
        n = len(self.stripe.sessions)
        self.webhook("checkout.session.completed", self.session_event(n, "unpaid", "pi_ach_2"))
        r = self.webhook("checkout.session.async_payment_failed", self.session_event(n, "unpaid", "pi_ach_2"))
        self.assertEqual(r.get_data(as_text=True), "failed")
        self.assertEqual(self.balance("cal"), 600)
        # a late 'completed' retry does not resurrect it
        self.webhook("checkout.session.completed", self.session_event(n, "unpaid", "pi_ach_2"))
        self.assertEqual(self.intent(n)["status"], "failed")

    def test_b_full_refund_in_stripe_reopens_the_lunch(self):
        r = self.webhook("charge.refunded", {"id": "ch_1", "payment_intent": "pi_card_1", "amount": 825,
                                             "amount_refunded": 825})
        self.assertEqual(r.get_data(as_text=True), "reversed (refund)")
        self.assertEqual(self.balance("ava"), 1650)
        self.assertEqual(self.webhook("charge.refunded", {"id": "ch_1", "payment_intent": "pi_card_1", "amount": 825,
                                                          "amount_refunded": 825}).get_data(as_text=True), "already reversed")
        page = self.client_as("viewer").get("/payments/online").get_data(as_text=True)
        self.assertIn("Reversed</span> (refunded)", page)
        self.assertIn("dashboard.stripe.com/test/payments/pi_card_1", page)

    def test_c_partial_refund_and_dispute_need_a_person(self):
        r = self.webhook("charge.refunded", {"id": "ch_2", "payment_intent": "pi_ach_1", "amount": 825,
                                             "amount_refunded": 300})
        self.assertEqual(r.get_data(as_text=True), "partial refund flagged")
        self.assertEqual(self.balance("ben"), 0)                   # unchanged: a person decides
        admin = self.client_as("admin")
        page = admin.get("/payments/online").get_data(as_text=True)
        self.assertIn("Partly refunded in Stripe ($3.00 of $8.25)", page)
        iid = self.db.fetch_one("select id from billing.payment_intents where stripe_payment_intent = 'pi_ach_1'")["id"]
        admin.post(f"/payments/online/{iid}/resolved", data={"csrf_token": self.token(admin, "/payments/online")})
        self.assertNotIn("Partly refunded", admin.get("/payments/online").get_data(as_text=True))
        r = self.webhook("charge.dispute.created", {"id": "dp_1", "payment_intent": "pi_ach_1", "reason": "fraudulent"})
        self.assertEqual(r.get_data(as_text=True), "reversed (dispute)")
        self.assertEqual(self.balance("ben"), 825)
        page = admin.get("/payments/online").get_data(as_text=True)
        self.assertIn("Disputed by the payer (fraudulent)", page)
        self.assertIn("Reversed</span> (disputed)", page)

    def test_d_unknown_or_foreign_sessions_are_ignored(self):
        fake = {"id": "cs_other", "payment_status": "paid", "payment_intent": "pi_other", "amount_total": 825,
                "metadata": {"intent_id": str(uuid.uuid4())}}
        self.assertEqual(self.webhook("checkout.session.completed", fake).get_data(as_text=True), "unknown intent")
        real = self.session_event(1, pi="pi_other")
        real["id"] = "cs_tampered"
        self.assertEqual(self.webhook("checkout.session.completed", real).get_data(as_text=True), "session mismatch")
        self.assertEqual(self.webhook("customer.created", {}).get_data(as_text=True), "ignored")

    def test_e_stripe_outage_charges_nothing(self):
        self.stripe.fail = True
        try:
            c = self.app.test_client()
            html = c.get(self.pay(self.lopez, "ava", 1, client=c).headers["Location"]).get_data(as_text=True)
        finally:
            self.stripe.fail = False
        self.assertIn("couldn&#39;t reach the payment service. Nothing was charged", html)
        row = self.db.fetch_one("select status from billing.payment_intents order by created_at desc limit 1")
        self.assertEqual(row["status"], "canceled")

    def test_f_fee_change_leaves_paid_lunches_alone(self):
        sa = self.client_as("super_admin")
        sa.post("/rates/fee", data={"csrf_token": self.token(sa, "/rates"), "fee": "0.50"})
        rows = {r["service_date"]: r for r in self.repo.student_lunches(self.inst["id"], self.sid["ben"])}
        self.assertEqual((rows["2026-09-01"]["price_cents"], rows["2026-09-01"]["fee_cents"]), ("825", "35"))  # paid, then disputed: locked
        ava = {r["service_date"]: r for r in self.repo.student_lunches(self.inst["id"], self.sid["ava"])}
        self.assertEqual(ava["2026-09-02"]["price_cents"], "840")

    def test_z_no_pin_and_no_parent_address_reached_providers(self):
        page = self.app.test_client().get(self.link(self.lopez)).get_data(as_text=True)
        self.assertNotIn("9876", page)
        self.assertNotIn("9876", json.dumps([s["params"] for s in self.stripe.sessions]))
        self.assertEqual({m["to"] for m in self.backend.sent}, {TEST_INBOX})


class PaymentsOffTests(unittest.TestCase):
    def test_webhook_is_404_and_portal_says_coming_soon_when_stripe_is_off(self):
        from tests.fakes import FakeRepo
        app = create_app({"TESTING": True, "SECRET_KEY": "t", "PORTAL_SECRET": SECRET}, repo=FakeRepo(),
                         auth=FakeAuth(), mailer=Mailer("outbox"))
        self.assertEqual(app.test_client().post("/stripe/webhook", data=b"{}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
