"""Runs the app's REAL SQL (app/repo.py) against a real Postgres, via psql.

Why psql and not psycopg: this lets the queries be verified on machines
without the Python driver installed. Placeholders (%(name)s) are replaced
with safely quoted literals, so the SQL text itself is exactly what psycopg runs.

Skipped unless TEST_PG is set to libpq connection options for a server where
you can create databases, e.g.  TEST_PG="host=/tmp port=5433 user=postgres".
NEVER point it at Supabase.
"""
import csv
import datetime as dt
import io
import os
import re
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path

from app.cli import migration_files
from app.repo import Repo

ROOT = Path(__file__).resolve().parent.parent
NULL = "<NULL>"


def literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (dt.date, dt.datetime, uuid.UUID)):
        v = v.isoformat() if not isinstance(v, uuid.UUID) else str(v)
    return "'" + str(v).replace("'", "''") + "'"


class PsqlDatabase:
    """Same interface as app.db.Database, backed by the psql CLI."""

    def __init__(self, conninfo, dbname):
        self.conninfo = f"{conninfo} dbname={dbname}"

    def _run(self, sql, csv_out):
        args = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", self.conninfo]
        if csv_out:
            args += ["--csv", "-P", f"null={NULL}"]
        proc = subprocess.run(args, input=sql, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip())
        return proc.stdout

    @staticmethod
    def bind(sql, params):
        params = params or {}
        return re.sub(r"%\((\w+)\)s", lambda m: literal(params[m.group(1)]), sql)

    def fetch_all(self, sql, params=None):
        out = self._run(self.bind(sql, params) + ";", csv_out=True)
        if not out.strip():
            return []
        rows = list(csv.DictReader(io.StringIO(out)))
        return [{k: (None if v == NULL else v) for k, v in r.items()} for r in rows]

    def fetch_one(self, sql, params=None):
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=None):
        self._run(self.bind(sql, params) + ";", csv_out=False)

    def execute_script(self, sql):
        self._run("begin;\n" + sql + "\ncommit;\n", csv_out=False)


def fresh_database(tag):
    """Create a scratch DB with the check-in app's tables stubbed and all migrations applied."""
    conninfo = os.environ["TEST_PG"]
    dbname = f"repo_{tag}_{os.getpid()}"
    PsqlDatabase(conninfo, "postgres").execute(f"create database {dbname}")
    db = PsqlDatabase(conninfo, dbname)
    db.execute_script((ROOT / "tests/sql/stub_public.sql").read_text())
    for f in migration_files():
        db.execute_script(f.read_text())
    return conninfo, dbname, db


needs_pg = unittest.skipUnless(os.environ.get("TEST_PG") and shutil.which("psql"), "set TEST_PG to run SQL tests")


class StaticSqlTests(unittest.TestCase):
    def test_billing_never_reads_the_pin_column(self):
        for name in dir(Repo):
            if name.startswith("SQL_"):
                sql = getattr(Repo, name).lower()
                self.assertIsNone(re.search(r"\bpin\b", sql), f"{name} mentions pin")
                self.assertIsNone(re.search(r"\bs\.\*", sql), f"{name} selects every student column")


@needs_pg
class RepoSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conninfo, cls.dbname, cls.db = fresh_database("core")
        cls.repo = Repo(cls.db)
        cls.inst = cls.repo.create_institution("sacred-heart", "Sacred Heart School of Glyndon",
                                               "Sacred Heart School of Glyndon", "Order", dt.date(2026, 8, 31))

    @classmethod
    def tearDownClass(cls):
        PsqlDatabase(cls.conninfo, "postgres").execute(f"drop database if exists {cls.dbname} with (force)")

    def test_get_institution(self):
        row = self.repo.get_institution("sacred-heart")
        self.assertEqual(row["name"], "Sacred Heart School of Glyndon")
        self.assertEqual(row["cycle_length_days"], "14")
        self.assertEqual(row["auto_send_enabled"], "f")
        self.assertIsNone(self.repo.get_institution("nope"))

    def test_first_touch_has_no_role_and_second_updates_email(self):
        uid = str(uuid.uuid4())
        row = self.repo.touch_staff(uid, "First@Example.com")
        self.assertIsNone(row["role"])
        row = self.repo.touch_staff(uid, "first@example.com")
        self.assertEqual(row["email"], "first@example.com")
        self.assertEqual(self.repo.find_staff_by_email("FIRST@example.com")["user_id"], uid)

    def test_set_role_writes_audit_in_same_statement(self):
        boss, new = str(uuid.uuid4()), str(uuid.uuid4())
        self.repo.touch_staff(boss, "boss@example.com")
        self.repo.set_role(boss, "super_admin", None, "cli")   # bootstrap path: no actor
        self.repo.touch_staff(new, "new@example.com")

        row = self.repo.set_role(new, "admin", boss, "boss@example.com")
        self.assertEqual(row["role"], "admin")
        self.assertEqual(row["audited"], "1")
        audit = self.db.fetch_one(
            "select actor, before, after from billing.audit_log where entity_id = %(id)s order by id desc limit 1",
            {"id": new})
        self.assertEqual(audit["actor"], boss)
        self.assertEqual(audit["before"], '{"role": null}')
        self.assertEqual(audit["after"], '{"role": "admin"}')

        row = self.repo.set_role(new, None, boss, "boss@example.com")
        self.assertIsNone(row["role"])
        self.assertIsNone(self.db.fetch_one("select granted_at from billing.staff_roles where user_id = %(u)s", {"u": new})["granted_at"])

    def test_set_role_unknown_user_returns_nothing(self):
        self.assertIsNone(self.repo.set_role(str(uuid.uuid4()), "viewer", None, "cli"))

    def test_list_staff_puts_pending_people_first(self):
        rows = self.repo.list_staff()
        roles = [r["role"] for r in rows]
        first_granted = next((i for i, r in enumerate(roles) if r is not None), len(roles))
        self.assertTrue(all(r is None for r in roles[:first_granted]))
        self.assertTrue(all(r is not None for r in roles[first_granted:]))

    def test_dashboard_on_empty_school(self):
        empty = self.repo.create_institution("empty-school", "Empty School", "Empty School", "Order", dt.date(2026, 8, 31))
        d = self.repo.dashboard(empty["id"])
        self.assertEqual(d["students_owing"], "0")
        self.assertEqual(d["outstanding_cents"], "0")
        self.assertIsNone(d["orders_known_through"])
        self.assertIsNone(d["current_cycle_status"])

    def test_dashboard_counts_only_money_owed(self):
        iid = self.inst["id"]
        self.db.execute("""
            insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents)
            values (%(i)s, '2026-09-01', '2026-09-30', 750);
            insert into billing.check_in_billing (check_in_id, institution_id, student_id, service_date, classification) values
              (gen_random_uuid(), %(i)s, '11111111-1111-1111-1111-111111111111', '2026-09-03', 'post_paid'),
              (gen_random_uuid(), %(i)s, '11111111-1111-1111-1111-111111111111', '2026-09-04', 'post_paid'),
              (gen_random_uuid(), %(i)s, '22222222-2222-2222-2222-222222222222', '2026-09-04', 'pre_ordered')
        """, {"i": iid})
        d = self.repo.dashboard(iid)
        self.assertEqual(d["students_owing"], "1")
        self.assertEqual(d["outstanding_cents"], "1500")
        self.assertEqual(d["oldest_unpaid_date"], "2026-09-03")


@needs_pg
class ContactSqlTests(unittest.TestCase):
    """Contacts against roster data shaped like the Sep 2026 export."""

    @classmethod
    def setUpClass(cls):
        cls.conninfo, cls.dbname, cls.db = fresh_database("contacts")
        cls.repo = Repo(cls.db)
        cls.iid = cls.repo.create_institution("sacred-heart", "Sacred Heart", "Sacred Heart", "Order",
                                              dt.date(2026, 8, 31))["id"]
        cls.ids = {}
        roster = [
            # first, last, email, phone
            ("Ava", "Lopez", "pat@example.com", "410-555-0101"),
            ("Ben", "Lopez", " PAT@Example.com ", "redacted"),   # sibling, messy copy of same email
            ("Cal", "Ng", "redacted", "redacted"),              # placeholders, like the test export
            ("Dee", "O'Brien", "", None),                        # nothing; apostrophe in name
            ("Eve", "Fox", "eve@example.com", None),
        ]
        for first, last, email, phone in roster:
            row = cls.db.fetch_one(
                "insert into public.students (first_name, last_name, grade_level, home_room, email, phone, pin) "
                "values (%(f)s, %(l)s, '3', 'Smith', %(e)s, %(p)s, '9999') returning id",
                {"f": first, "l": last, "e": email, "p": phone})
            cls.ids[first] = row["id"]
        # Cal, Dee, and Eve each owe one $7.50 lunch; Ava and Ben owe nothing
        cls.db.execute("insert into billing.rate_periods (institution_id, starts_on, ends_on, price_cents) "
                       "values (%(i)s, '2026-10-01', '2026-10-31', 750)", {"i": cls.iid})
        for first in ("Cal", "Dee", "Eve"):
            cls.db.execute("insert into billing.check_in_billing (check_in_id, institution_id, student_id, "
                           "service_date, classification) values (gen_random_uuid(), %(i)s, %(s)s, '2026-10-05', 'post_paid')",
                           {"i": cls.iid, "s": cls.ids[first]})
        cls.first_import = cls.repo.import_roster_contacts(cls.iid, None, "tester")

    @classmethod
    def tearDownClass(cls):
        PsqlDatabase(cls.conninfo, "postgres").execute(f"drop database if exists {cls.dbname} with (force)")

    def guardians_of(self, first):
        return self.repo.student_guardians(self.iid, self.ids[first])

    def test_import_skips_junk_and_merges_siblings(self):
        r = self.first_import
        self.assertEqual(r["roster_students"], "5")
        self.assertEqual(r["with_valid_email"], "3")        # Ava, Ben (same parent), Eve
        self.assertEqual(r["guardians_created"], "2")       # pat@, eve@
        self.assertEqual(r["links_created"], "3")
        (pat,) = self.guardians_of("Ava")
        self.assertEqual(pat["email"], "pat@example.com")
        self.assertEqual(pat["phone"], "410-555-0101")      # 'redacted' from Ben's row ignored
        self.assertEqual(pat["source"], "roster")
        self.assertEqual(self.guardians_of("Ben")[0]["id"], pat["id"])
        self.assertEqual(self.guardians_of("Cal"), [])

    def test_import_is_safe_to_rerun(self):
        again = self.repo.import_roster_contacts(self.iid, None, "tester")
        self.assertEqual((again["guardians_created"], again["links_created"]), ("0", "0"))

    def test_school_list_has_only_billed_children_without_email(self):
        rows = self.repo.billed_without_contact(self.iid)
        got = {r["first_name"]: r["reason"] for r in rows}
        self.assertIn("Cal", got)
        self.assertIn("Dee", got)
        self.assertNotIn("Eve", got)     # owes, but has an email
        self.assertNotIn("Ava", got)     # has email, owes nothing
        self.assertEqual(rows[0]["balance_due_cents"], "750")
        self.assertEqual(int(self.repo.dashboard(self.iid)["billed_without_contact"]), len(rows))

    def test_list_students_search_and_missing_filter(self):
        names = [r["last_name"] for r in self.repo.list_students(self.iid, "o'bri")]
        self.assertEqual(names, ["O'Brien"])
        self.assertEqual(self.repo.list_students(self.iid, "100%"), [])      # % is literal, not a wildcard
        missing = {r["first_name"] for r in self.repo.list_students(self.iid, None, missing_only=True)}
        self.assertTrue({"Cal", "Dee"} <= missing)
        self.assertNotIn("Eve", missing)

    def test_student_page_never_returns_pin(self):
        s = self.repo.get_student(self.iid, self.ids["Cal"])
        self.assertNotIn("pin", s)
        self.assertEqual(s["roster_email"], "redacted")
        self.assertEqual(s["balance_due_cents"], "750")

    def test_add_phone_only_contact_keeps_child_on_school_list(self):
        res = self.repo.add_guardian_to_student(self.iid, self.ids["Dee"], "Dana O'Brien", None, "443-555-0199", None, "t")
        self.assertEqual((res["created"], res["linked"]), ("1", "1"))
        reasons = {r["first_name"]: r["reason"] for r in self.repo.billed_without_contact(self.iid)}
        self.assertEqual(reasons["Dee"], "no_email")

    def test_adding_existing_email_links_instead_of_duplicating(self):
        res = self.repo.add_guardian_to_student(self.iid, self.ids["Cal"], None, "EVE@example.com", None, None, "t")
        self.assertEqual(res["created"], "0")
        self.assertEqual(self.guardians_of("Cal")[0]["email"], "eve@example.com")
        self.assertNotIn("Cal", {r["first_name"] for r in self.repo.billed_without_contact(self.iid)})
        self.assertEqual(self.repo.unlink_guardian(self.iid, res["guardian_id"], self.ids["Cal"], None, "t"), 1)
        self.assertEqual(self.repo.unlink_guardian(self.iid, res["guardian_id"], self.ids["Cal"], None, "t"), 0)

    def test_update_guardian_audits_and_db_blocks_duplicate_email(self):
        (eve,) = self.guardians_of("Eve")
        row = self.repo.update_guardian(self.iid, eve["id"], "Eve Sr", "eve.fox@example.com", None, False, None, "t")
        self.assertEqual((row["email"], row["receives_notices"], row["audited"]), ("eve.fox@example.com", "f", "1"))
        audit = self.db.fetch_one("select before->>'email' as b, after->>'email' as a from billing.audit_log "
                                  "where action = 'update_guardian' order by id desc limit 1")
        self.assertEqual((audit["b"], audit["a"]), ("eve@example.com", "eve.fox@example.com"))
        # opted out -> Eve now flagged
        self.assertEqual({r["first_name"]: r["reason"] for r in self.repo.billed_without_contact(self.iid)}["Eve"],
                         "notices_off")
        self.assertEqual(self.repo.find_guardian_by_email(self.iid, "PAT@example.com")["email"], "pat@example.com")
        self.assertIsNone(self.repo.find_guardian_by_email(self.iid, "eve.fox@example.com", exclude_id=eve["id"]))
        with self.assertRaises(RuntimeError):     # app checks first; the unique index is the backstop
            self.repo.update_guardian(self.iid, eve["id"], None, "pat@example.com", None, True, None, "t")
        self.repo.update_guardian(self.iid, eve["id"], None, "eve@example.com", None, True, None, "t")


if __name__ == "__main__":
    unittest.main()
