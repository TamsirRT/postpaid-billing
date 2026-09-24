"""Phase 2: statements, test-mode email, receipts, the parent portal.

The safety rule tested hardest: outside EMAIL_MODE=live, no parent address is
ever handed to the email provider.
"""
import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from app import create_app
from app.config import ConfigError, load_config
from app.mailer import DeliveryError, Mailer
from app.portal import looks_like_token, portal_token, token_hash_hex
from app.repo import Repo
from tests.fakes import FakeAuth
from tests.test_app import csrf_from
from tests.test_phase1 import MODULE, SCHOOL
from tests.test_repo_sql import PsqlDatabase, fresh_database, needs_pg

TEST_INBOX = "trichtoure@gmail.com"
SECRET = "s" * 40


class RecordingBackend:
    """Stands in for SendGrid: records what would have been sent."""

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, to_email, subject, html, text):
        if self.fail:
            raise DeliveryError("SendGrid refused the message (401): bad key")
        self.sent.append({"to": to_email, "subject": subject, "html": html, "text": text})
        return f"msg-{len(self.sent)}"


# ====================================================================== pure units
class MailerTests(unittest.TestCase):
    def test_test_mode_sends_only_to_the_test_inbox_and_names_the_parent(self):
        b = RecordingBackend()
        out = Mailer("test", TEST_INBOX, b).deliver("parent@example.org", "Lunch balance", "<p>hi</p>", "hi")
        self.assertEqual([m["to"] for m in b.sent], [TEST_INBOX])
        self.assertEqual(out["delivered_to"], TEST_INBOX)
        self.assertEqual(b.sent[0]["subject"], "[TEST → parent@example.org] Lunch balance")
        self.assertIn("TEST EMAIL", b.sent[0]["html"])
        self.assertIn("parent@example.org", b.sent[0]["text"])

    def test_banner_escapes_the_address(self):
        _, _, html, _ = Mailer("test", TEST_INBOX, RecordingBackend()).prepare("<b>x@y.z", "s", "", "")
        self.assertIn("&lt;b&gt;x@y.z", html)

    def test_outbox_sends_nothing(self):
        out = Mailer("outbox").deliver("parent@example.org", "s", "<p>h</p>", "t")
        self.assertIsNone(out["delivered_to"])

    def test_live_goes_to_the_parent_unchanged(self):
        b = RecordingBackend()
        Mailer("live", None, b).deliver("parent@example.org", "s", "<p>h</p>", "t")
        self.assertEqual(b.sent[0], {"to": "parent@example.org", "subject": "s", "html": "<p>h</p>", "text": "t"})

    def test_bad_setups_refused(self):
        for args in (("prod",), ("test", None, RecordingBackend()), ("test", TEST_INBOX, None), ("live", None, None)):
            with self.assertRaises(ValueError):
                Mailer(*args)


class EmailConfigTests(unittest.TestCase):
    BASE = {"APP_ENV": "production", "SECRET_KEY": "x" * 40, "DATABASE_URL": "postgresql://x",
            "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_ANON_KEY": "k", "PORTAL_SECRET": SECRET,
            "PUBLIC_BASE_URL": "https://billing.example.org"}

    def load(self, **extra):
        with mock.patch.dict(os.environ, {**self.BASE, **extra}, clear=True):
            return load_config()

    def test_default_is_outbox(self):
        self.assertEqual(self.load()["EMAIL_MODE"], "outbox")

    def test_test_mode_needs_key_sender_and_inbox(self):
        with self.assertRaisesRegex(ConfigError, "SENDGRID_API_KEY"):
            self.load(EMAIL_MODE="test", EMAIL_TEST_RECIPIENT=TEST_INBOX)
        with self.assertRaisesRegex(ConfigError, "EMAIL_TEST_RECIPIENT"):
            self.load(EMAIL_MODE="test", SENDGRID_API_KEY="k", EMAIL_FROM="billing@mealmode.com")
        cfg = self.load(EMAIL_MODE="TEST", SENDGRID_API_KEY="k", EMAIL_FROM="billing@mealmode.com",
                        EMAIL_TEST_RECIPIENT=TEST_INBOX)
        self.assertEqual((cfg["EMAIL_MODE"], cfg["EMAIL_TEST_RECIPIENT"]), ("test", TEST_INBOX))

    def test_unknown_mode_refused(self):
        with self.assertRaisesRegex(ConfigError, "outbox, test, or live"):
            self.load(EMAIL_MODE="on")

    def test_portal_secret_and_public_url_required(self):
        with self.assertRaisesRegex(ConfigError, "PORTAL_SECRET"):
            self.load(PORTAL_SECRET="short")
        with self.assertRaisesRegex(ConfigError, "PUBLIC_BASE_URL"):
            self.load(PUBLIC_BASE_URL="")

    def test_development_defaults_public_url_to_local(self):
        cfg = self.load(APP_ENV="development", PUBLIC_BASE_URL="")
        self.assertEqual(cfg["PUBLIC_BASE_URL"], "http://127.0.0.1:5000")


class PortalTokenTests(unittest.TestCase):
    def test_token_is_stable_per_version_and_changes_on_rotation(self):
        gid = str(uuid.uuid4())
        a, b, c = portal_token(SECRET, gid, 1), portal_token(SECRET, gid, 1), portal_token(SECRET, gid, 2)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, portal_token("t" * 40, gid, 1))
        self.assertTrue(looks_like_token(a))
        self.assertEqual(len(token_hash_hex(a)), 64)

    def test_shape_check(self):
        for bad in ("", "x" * 42, "x" * 44, "a/" * 21 + "a", None):
            self.assertFalse(looks_like_token(bad))


# ====================================================================== end to end
@needs_pg
class Phase2EndToEndTests(unittest.TestCase):
    """Real SQL, real pages, a recording email backend in test mode."""

    @classmethod
    def setUpClass(cls):
        cls.conninfo, cls.dbname, cls.db = fresh_database("phase2")
        cls.repo = Repo(cls.db)
        cls.repo.create_institution("sacred-heart", SCHOOL, SCHOOL, MODULE, dt.date(2026, 8, 31))
        cls.inst = cls.repo.get_institution("sacred-heart")
        iid = cls.inst["id"]
        cls.sid = {}
        for key, first, last in [("ava", "Ava", "Lopez"), ("ben", "Ben", "Lopez"), ("cal", "Cal", "Ng"),
                                 ("dot", "Dot", "Reyes"), ("eli", "Eli", "Park")]:
            cls.sid[key] = cls.db.fetch_one(
                "insert into public.students (first_name, last_name, grade_level, home_room, pin) "
                "values (%(f)s, %(l)s, '3', 'Room 1', '9876') returning id", {"f": first, "l": last})["id"]
        # post-paid lunches straight into the ledger (classification is tested in phase 1)
        lunches = {"ava": ["2026-09-01", "2026-09-02"], "ben": ["2026-09-01"], "cal": ["2026-09-03"],
                   "dot": ["2026-09-04"], "eli": ["2026-09-04"]}
        for key, days in lunches.items():
            for d in days:
                cid = str(uuid.uuid4())
                cls.db.execute("insert into public.check_ins (id, student_id, check_in_date, getting_lunch, "
                               "check_in_time, bill_separately) values (%(id)s, %(s)s, %(d)s, true, %(t)s, false)",
                               {"id": cid, "s": cls.sid[key], "d": d, "t": f"{d} 12:00-04"})
                cls.db.execute("insert into billing.check_in_billing (check_in_id, institution_id, student_id, "
                               "service_date, classification) values (%(c)s, %(i)s, %(s)s, %(d)s, 'post_paid')",
                               {"c": cid, "i": iid, "s": cls.sid[key], "d": d})
        # one no-lunch day for Ava so the portal shows it
        cid = str(uuid.uuid4())
        cls.db.execute("insert into public.check_ins (id, student_id, check_in_date, getting_lunch, check_in_time, "
                       "bill_separately) values (%(id)s, %(s)s, '2026-09-08', false, '2026-09-08 12:00-04', false)",
                       {"id": cid, "s": cls.sid["ava"]})
        cls.db.execute("insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, "
                       "classification) values (%(c)s, %(i)s, %(s)s, '2026-09-08', 'no_lunch')",
                       {"c": cid, "i": iid, "s": cls.sid["ava"]})
        # contacts: Lopez parent has two kids; Cal's parent; Dot's parent opted out; Eli has no contact
        add = cls.repo.add_guardian_to_student
        cls.lopez = add(iid, cls.sid["ava"], "Maria Lopez", "maria@example.org", None, None, "setup")["guardian_id"]
        add(iid, cls.sid["ben"], "Maria Lopez", "maria@example.org", None, None, "setup")
        cls.ng = add(iid, cls.sid["cal"], "Sam Ng", "sam@example.org", None, None, "setup")["guardian_id"]
        cls.reyes = add(iid, cls.sid["dot"], "Jo Reyes", "jo@example.org", None, None, "setup")["guardian_id"]
        cls.db.execute("update billing.guardians set receives_notices = false where id = %(g)s", {"g": cls.reyes})

        cls.backend = RecordingBackend()
        cls.mailer = Mailer("test", TEST_INBOX, cls.backend)
        cls.auth = FakeAuth()
        cls.app = create_app({"TESTING": True, "SECRET_KEY": "t", "SESSION_COOKIE_SECURE": False,
                              "PORTAL_SECRET": SECRET, "PUBLIC_BASE_URL": "https://billing.example.org",
                              "SUPPORT_EMAIL": "help@mealmode.test"},
                             repo=cls.repo, auth=cls.auth, mailer=cls.mailer)
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

    def client_as(self, role):
        c = self.app.test_client()
        token = csrf_from(c.get("/login").get_data(as_text=True))
        c.post("/login", data={"email": self.users[role], "password": "long-enough-password", "csrf_token": token})
        return c

    def token(self, c, path="/"):
        return csrf_from(c.get(path).get_data(as_text=True))

    def link_for(self, gid):
        return f"/p/{portal_token(SECRET, str(gid), self.repo.guardian_token_version(self.inst['id'], str(gid)))}"

    def notifications(self):
        return self.db.fetch_all("select * from billing.notifications order by created_at")

    # ------------------------------------------------------------ ordered walk-through
    def test_1_statements_page_lists_only_reachable_parents(self):
        c = self.client_as("viewer")
        html = c.get("/statements").get_data(as_text=True)
        self.assertIn("TEST MODE: every email goes to trichtoure@gmail.com", html)
        self.assertIn("Maria Lopez", html)
        self.assertIn("$23.70", html)           # 3 lunches x $7.90, two kids, one parent
        self.assertIn("Sam Ng", html)
        self.assertNotIn("Jo Reyes", html)      # opted out
        self.assertIn("2 children</a> owe money but have no parent who can be emailed", html)  # Eli, Dot
        self.assertNotIn('action="/statements/send"', html)   # viewer can't send

    def test_2_send_all_requires_confirmation_and_a_current_list(self):
        c = self.client_as("admin")
        t = self.token(c, "/statements")
        html = c.post("/statements/send", data={"csrf_token": t, "expected_count": "2"},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn("Tick the box", html)
        html = c.post("/statements/send", data={"csrf_token": t, "confirm": "yes", "expected_count": "5"},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn("The list changed", html)
        self.assertEqual(self.backend.sent, [])

    def test_3_send_all_in_test_mode_reaches_only_the_test_inbox(self):
        c = self.client_as("admin")
        html = c.post("/statements/send", data={"csrf_token": self.token(c, "/statements"), "confirm": "yes",
                                                "expected_count": "2"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("2 statement(s) emailed to the test inbox trichtoure@gmail.com", html)
        self.assertEqual({m["to"] for m in self.backend.sent}, {TEST_INBOX})
        subjects = sorted(m["subject"] for m in self.backend.sent)
        self.assertEqual(subjects, ["[TEST → maria@example.org] Lunch balance for Ava & Ben: $23.70",
                                    "[TEST → sam@example.org] Lunch balance for Cal: $7.90"])
        maria = next(m for m in self.backend.sent if "maria" in m["subject"])
        link = self.link_for(self.lopez)
        self.assertIn("https://billing.example.org" + link, maria["text"])
        self.assertIn("https://billing.example.org" + link, maria["html"])
        self.assertIn("may change until paid", maria["text"])
        self.assertNotIn("9876", maria["html"] + maria["text"])     # never the PIN
        rows = self.notifications()
        self.assertEqual({(r["mode"], r["status"], r["delivered_to"]) for r in rows}, {("test", "sent", TEST_INBOX)})
        self.assertEqual({r["intended_email"] for r in rows}, {"maria@example.org", "sam@example.org"})
        # the log page shows both addresses
        html = c.get("/emails").get_data(as_text=True)
        self.assertIn("maria@example.org", html)
        self.assertIn(TEST_INBOX, html)

    def test_4_second_send_within_24h_is_skipped_unless_overridden(self):
        c = self.client_as("admin")
        before = len(self.backend.sent)
        html = c.post("/statements/send", data={"csrf_token": self.token(c, "/statements"), "confirm": "yes",
                                                "expected_count": "2"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("0 statement(s)", html)
        self.assertIn("2 skipped: already sent one in the last 24 hours", html)
        self.assertEqual(len(self.backend.sent), before)
        html = c.post(f"/guardians/{self.ng}/statement", data={"csrf_token": self.token(c, "/statements"),
                                                               "override_recent": "on"},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn("Statement emailed to the test inbox", html)
        self.assertEqual(len(self.backend.sent), before + 1)

    def test_5_opted_out_and_nothing_owed_are_not_sent(self):
        c = self.client_as("admin")
        html = c.post(f"/guardians/{self.reyes}/statement", data={"csrf_token": self.token(c, "/statements")},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn("Not sent: no email or opted out", html)

    def test_6_database_refuses_a_notification_without_an_email(self):
        g = self.db.fetch_one("insert into billing.guardians (institution_id, name, phone, source) "
                              "values (%(i)s, 'Phone Only', '410-555-0100', 'manual') returning id",
                              {"i": self.inst["id"]})["id"]
        with self.assertRaises(Exception):
            self.repo.insert_notification(self.inst["id"], g, "manual_individual", {}, None, "test", None,
                                          "s", "h", "t")

    def test_7_parent_portal_shows_their_children_only(self):
        c = self.app.test_client()                                  # not signed in
        resp = c.get(self.link_for(self.lopez))
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        for text in ("Ava Lopez", "Ben Lopez", "$15.80", "$7.90", "Checked in, no lunch", "Unpaid",
                     "Online payment is coming soon", "help@mealmode.test", "may change until paid"):
            self.assertIn(text, html)
        self.assertIn("2 lunches", html)                            # whole-lunch pay option for Ava
        self.assertNotIn("Cal", html)
        self.assertNotIn("9876", html)
        self.assertEqual(resp.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertIn("noindex", resp.headers["X-Robots-Tag"])

    def test_8_bad_and_guessed_links_are_404(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/p/nope").status_code, 404)
        self.assertEqual(c.get("/p/" + "A" * 43).status_code, 404)
        # a correctly-shaped token for a real guardian but signed with another secret
        self.assertEqual(c.get("/p/" + portal_token("t" * 40, str(self.ng), 1)).status_code, 404)

    def test_9_payment_sends_receipt_and_shows_on_portal(self):
        c = self.client_as("admin")
        before = len(self.backend.sent)
        html = c.post(f"/students/{self.sid['cal']}/payments", data={
            "csrf_token": self.token(c, f"/students/{self.sid['cal']}"), "amount": "7.90", "method": "check",
            "received_on": "2026-09-20", "note": ""}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Recorded $7.90", html)
        receipts = self.backend.sent[before:]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["to"], TEST_INBOX)
        self.assertIn("[TEST → sam@example.org] Payment received for Cal: $7.90", receipts[0]["subject"])
        portal = self.app.test_client().get(self.link_for(self.ng)).get_data(as_text=True)
        self.assertIn("Paid", portal)
        self.assertIn("nothing owed", portal)

    def test_a_failed_delivery_is_logged_not_lost(self):
        self.backend.fail = True
        try:
            c = self.client_as("admin")
            html = c.post(f"/guardians/{self.lopez}/statement", data={"csrf_token": self.token(c, "/statements"),
                                                                      "override_recent": "on"},
                          follow_redirects=True).get_data(as_text=True)
            self.assertIn("Sending failed", html)
        finally:
            self.backend.fail = False
        row = self.notifications()[-1]
        self.assertEqual((row["status"], row["delivered_to"]), ("failed", None))
        self.assertIn("401", row["error"])
        self.assertTrue(row["body_html"])

    def test_b_rotating_the_link_kills_the_old_one(self):
        old = self.link_for(self.lopez)
        admin = self.client_as("admin")
        self.assertEqual(admin.post(f"/guardians/{self.lopez}/rotate-link",
                                    data={"csrf_token": self.token(admin, "/statements")}).status_code, 403)
        sa = self.client_as("super_admin")
        html = sa.post(f"/guardians/{self.lopez}/rotate-link", data={"csrf_token": self.token(sa, "/statements")},
                       follow_redirects=True).get_data(as_text=True)
        self.assertIn("New link created", html)
        c = self.app.test_client()
        self.assertEqual(c.get(old).status_code, 404)
        self.assertEqual(c.get(self.link_for(self.lopez)).status_code, 200)
        self.assertTrue(self.db.fetch_one("select count(*) as n from billing.audit_log "
                                          "where action = 'rotate_portal_link'")["n"] == "1")

    def test_c_staff_preview_and_email_viewer(self):
        c = self.client_as("viewer")
        resp = c.get(f"/guardians/{self.ng}/portal")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].startswith("/p/"))
        nid = self.notifications()[0]["id"]
        page = c.get(f"/emails/{nid}").get_data(as_text=True)
        self.assertIn("Actually went to", page)
        body = c.get(f"/emails/{nid}/body")
        self.assertIn("sandbox", body.headers["Content-Security-Policy"])
        self.assertIn("TEST EMAIL", body.get_data(as_text=True))
        self.assertEqual(self.app.test_client().get(f"/emails/{nid}/body").status_code, 302)   # sign-in needed

    def test_d_portal_is_rate_limited(self):
        from app import views
        views._portal_hits.clear()
        c = self.app.test_client()
        codes = [c.get("/p/nope", headers={"X-Forwarded-For": "203.0.113.9"}).status_code for _ in range(61)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(set(codes[:60]), {404})
        views._portal_hits.clear()

    def test_z_no_parent_address_ever_reached_the_provider(self):
        self.assertTrue(self.backend.sent)
        self.assertEqual({m["to"] for m in self.backend.sent}, {TEST_INBOX})


if __name__ == "__main__":
    unittest.main()
