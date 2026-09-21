"""Web-layer tests. Run:  python -m unittest discover -s tests -v"""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import create_app, format_cents
from app.cli import checksum, plan_migrations
from app.config import ConfigError, load_config
from tests.fakes import FakeAuth, FakeRepo


def make_app(**repo_kwargs):
    repo, auth = FakeRepo(**repo_kwargs), FakeAuth()
    app = create_app({"TESTING": True, "SECRET_KEY": "test-secret", "INSTITUTION_SLUG": "sacred-heart",
                      "SESSION_COOKIE_SECURE": False}, repo=repo, auth=auth)
    return app, repo, auth


def csrf_from(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf token on page"
    return m.group(1)


class Base(unittest.TestCase):
    def setUp(self):
        self.app, self.repo, self.auth = make_app()
        self.client = self.app.test_client()

    def login(self, email="pat@mealmode.test", password="correct horse battery", role=None):
        uid = self.auth.add(email, password)
        if role is not None:
            self.repo.staff[uid] = {"user_id": uid, "email": email, "role": role}
        token = csrf_from(self.client.get("/login").get_data(as_text=True))
        resp = self.client.post("/login", data={"email": email, "password": password, "csrf_token": token})
        return uid, resp

    def token(self, path="/"):
        return csrf_from(self.client.get(path).get_data(as_text=True))


class SignInTests(Base):
    def test_login_page_renders_with_csrf(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        csrf_from(resp.get_data(as_text=True))

    def test_post_without_csrf_is_rejected(self):
        resp = self.client.post("/login", data={"email": "a@b.co", "password": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_first_sign_in_creates_staff_row_with_no_access(self):
        uid, resp = self.login()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/pending"))
        self.assertIsNone(self.repo.staff[uid]["role"])
        self.assertIn("no role has been granted", self.client.get("/pending").get_data(as_text=True))

    def test_wrong_password_shows_error(self):
        self.auth.add("pat@mealmode.test", "right password!!")
        token = csrf_from(self.client.get("/login").get_data(as_text=True))
        resp = self.client.post("/login", data={"email": "pat@mealmode.test", "password": "wrong", "csrf_token": token})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("incorrect", resp.get_data(as_text=True))

    def test_open_redirect_blocked(self):
        uid = self.auth.add("pat@mealmode.test", "pw-long-enough!")
        self.repo.staff[uid] = {"user_id": uid, "email": "pat@mealmode.test", "role": "viewer"}
        token = csrf_from(self.client.get("/login").get_data(as_text=True))
        resp = self.client.post("/login", data={"email": "pat@mealmode.test", "password": "pw-long-enough!",
                                                "csrf_token": token, "next": "https://evil.example/steal"})
        self.assertEqual(resp.headers["Location"], "/")

    def test_signup_requires_long_password(self):
        token = self.token("/signup")
        resp = self.client.post("/signup", data={"email": "new@mealmode.test", "password": "short", "csrf_token": token})
        self.assertEqual(resp.status_code, 400)

    def test_logout_requires_post_with_csrf(self):
        self.login(role="viewer")
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertEqual(self.client.post("/logout").status_code, 400)
        token = self.token("/")
        self.assertEqual(self.client.post("/logout", data={"csrf_token": token}).status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 302)


class AccessTests(Base):
    def test_dashboard_requires_sign_in(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_viewer_sees_dashboard(self):
        self.login(role="viewer")
        self.repo.dashboard_row["outstanding_cents"] = 123456
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Sacred Heart School of Glyndon", html)
        self.assertIn("$1,234.56", html)

    def test_revoking_role_takes_effect_on_next_request(self):
        uid, _ = self.login(role="admin")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.repo.staff[uid]["role"] = None
        resp = self.client.get("/")
        self.assertTrue(resp.headers["Location"].endswith("/pending"))

    def test_viewer_and_admin_cannot_manage_staff(self):
        uid, _ = self.login(role="admin")
        self.assertEqual(self.client.get("/admin/staff").status_code, 403)

    def test_dashboard_shows_hard_coded_billing_start(self):
        self.login(role="viewer")
        self.assertIn("Aug 31, 2026", self.client.get("/").get_data(as_text=True))

    def test_dashboard_warns_for_school_without_start_date(self):
        app, repo, auth = make_app()
        repo.institution["slug"] = "st-joseph"
        app.config["INSTITUTION_SLUG"] = "st-joseph"
        client = app.test_client()
        uid = auth.add("v@mealmode.test", "pw-long-enough!")
        repo.staff[uid] = {"user_id": uid, "email": "v@mealmode.test", "role": "viewer"}
        token = csrf_from(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "v@mealmode.test", "password": "pw-long-enough!", "csrf_token": token})
        self.assertIn("Nothing will be billed", client.get("/").get_data(as_text=True))

    def test_missing_institution_shows_setup_message(self):
        self.repo.institution = None
        self.login(role="viewer")
        self.assertIn("No institution configured", self.client.get("/").get_data(as_text=True))

    def test_security_headers(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", resp.headers["Content-Security-Policy"])


class StaffRoleTests(Base):
    def setUp(self):
        super().setUp()
        self.admin_uid, _ = self.login(email="boss@mealmode.test", role="super_admin")
        self.other = self.auth.add("new@mealmode.test", "whatever-password")
        self.repo.staff[self.other] = {"user_id": self.other, "email": "new@mealmode.test", "role": None}

    def test_super_admin_grants_role_and_it_is_audited(self):
        token = self.token("/admin/staff")
        resp = self.client.post(f"/admin/staff/{self.other}/role", data={"role": "admin", "csrf_token": token})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.repo.staff[self.other]["role"], "admin")
        self.assertEqual(self.repo.audit_log[-1], {"actor": self.admin_uid, "target": self.other, "before": None, "after": "admin"})

    def test_revoke_to_no_access(self):
        self.repo.staff[self.other]["role"] = "viewer"
        token = self.token("/admin/staff")
        self.client.post(f"/admin/staff/{self.other}/role", data={"role": "", "csrf_token": token})
        self.assertIsNone(self.repo.staff[self.other]["role"])

    def test_cannot_change_own_role(self):
        token = self.token("/admin/staff")
        self.client.post(f"/admin/staff/{self.admin_uid}/role", data={"role": "viewer", "csrf_token": token})
        self.assertEqual(self.repo.staff[self.admin_uid]["role"], "super_admin")

    def test_unknown_role_rejected(self):
        token = self.token("/admin/staff")
        resp = self.client.post(f"/admin/staff/{self.other}/role", data={"role": "owner", "csrf_token": token})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_user_404(self):
        token = self.token("/admin/staff")
        resp = self.client.post("/admin/staff/00000000-0000-0000-0000-000000000000/role",
                                data={"role": "viewer", "csrf_token": token})
        self.assertEqual(resp.status_code, 404)


class HelperTests(unittest.TestCase):
    def test_format_cents(self):
        self.assertEqual(format_cents(0), "$0.00")
        self.assertEqual(format_cents(750), "$7.50")
        self.assertEqual(format_cents(123456789), "$1,234,567.89")
        self.assertEqual(format_cents(-500), "-$5.00")
        self.assertEqual(format_cents(None), "—")

    def test_config_requires_settings_in_production(self):
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            with self.assertRaises(ConfigError):
                load_config()

    def test_config_rejects_short_secret_in_production(self):
        env = {"APP_ENV": "production", "SECRET_KEY": "short", "DATABASE_URL": "postgresql://x",
               "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_ANON_KEY": "k"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError):
                load_config()

    def test_production_cookies_are_secure(self):
        env = {"APP_ENV": "production", "SECRET_KEY": "x" * 40, "DATABASE_URL": "postgresql://x",
               "SUPABASE_URL": "https://x.supabase.co/", "SUPABASE_ANON_KEY": "k"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertTrue(cfg["SESSION_COOKIE_SECURE"])
        self.assertEqual(cfg["SUPABASE_URL"], "https://x.supabase.co")


class MigrationPlanTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.a = self.dir / "001_a.sql"; self.a.write_text("select 1;")
        self.b = self.dir / "002_b.sql"; self.b.write_text("select 2;")

    def test_new_database_has_everything_pending(self):
        pending, problems = plan_migrations({}, [self.a, self.b])
        self.assertEqual([p.name for p in pending], ["001_a.sql", "002_b.sql"])
        self.assertEqual(problems, [])

    def test_only_unapplied_are_pending(self):
        pending, problems = plan_migrations({"001_a.sql": checksum(self.a)}, [self.a, self.b])
        self.assertEqual([p.name for p in pending], ["002_b.sql"])

    def test_edited_applied_migration_is_a_problem(self):
        pending, problems = plan_migrations({"001_a.sql": "stale"}, [self.a, self.b])
        self.assertTrue(any("changed after it was applied" in p for p in problems))

    def test_missing_applied_migration_is_a_problem(self):
        pending, problems = plan_migrations({"000_gone.sql": "x"}, [self.a])
        self.assertTrue(any("missing" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
