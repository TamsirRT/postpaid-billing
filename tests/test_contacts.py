"""Contact management: editing, validation, permissions, and the missing-contacts list."""
import csv
import io
import unittest

from app.contacts import clean_contact_form, clean_email, clean_phone, csv_safe
from tests.test_app import Base


class CleanTests(unittest.TestCase):
    def test_placeholders_are_not_emails_or_phones(self):
        for junk in ("redacted", "n/a", "none", "@", "a@b", "parent at gmail.com"):
            self.assertIsNotNone(clean_email(junk)[1], junk)
        for junk in ("redacted", "555-1234", "123"):
            self.assertIsNotNone(clean_phone(junk)[1], junk)

    def test_valid_values_are_normalized(self):
        self.assertEqual(clean_email("  Pat.Parent@Example.COM "), ("pat.parent@example.com", None))
        self.assertEqual(clean_phone(" (410) 555-0101 "), ("(410) 555-0101", None))
        self.assertEqual(clean_email(""), (None, None))

    def test_form_needs_email_or_phone(self):
        values, errors = clean_contact_form({"name": "Pat"})
        self.assertEqual(errors, ["Enter an email or a phone number."])
        values, errors = clean_contact_form({"name": " ", "phone": "410-555-0101"})
        self.assertEqual(errors, [])
        self.assertIsNone(values["name"])

    def test_csv_formula_injection_neutralized(self):
        self.assertEqual(csv_safe("=HYPERLINK(\"x\")"), "'=HYPERLINK(\"x\")")
        self.assertEqual(csv_safe("-5"), "'-5")
        self.assertEqual(csv_safe("Smith"), "Smith")
        self.assertEqual(csv_safe(None), "")


class ContactViewTests(Base):
    def setUp(self):
        super().setUp()
        r = self.repo
        self.ava = r.add_student("Ava", "Lopez", balance=1500)      # owes, no contact
        self.ben = r.add_student("Ben", "Lopez", balance=750)       # owes, will get contact
        self.cal = r.add_student("Cal", "Ng", balance=0)            # owes nothing, no contact
        self.dee = r.add_student("=Dee", "Hacker", balance=750)     # owes, name tries formula injection

    def as_admin(self):
        self.login(email="admin@mealmode.test", role="admin")

    def as_viewer(self):
        self.login(email="viewer@mealmode.test", role="viewer")

    # -------------------------------------------------------------- viewing
    def test_viewer_can_browse_students_and_filter_missing(self):
        self.as_viewer()
        html = self.client.get("/students").get_data(as_text=True)
        self.assertIn("Lopez, Ava", html)
        self.assertIn("Ng, Cal", html)
        html = self.client.get("/students?missing=1").get_data(as_text=True)
        self.assertIn("Lopez, Ava", html)
        self.assertNotIn("Ng, Cal", html)      # owes nothing: not a problem

    def test_student_page_warns_when_billed_without_contact(self):
        self.as_viewer()
        html = self.client.get(f"/students/{self.ava}").get_data(as_text=True)
        self.assertIn("no one can be emailed", html)
        self.assertNotIn("Add a contact", html)  # viewers can't edit

    def test_bad_student_id_is_404(self):
        self.as_viewer()
        self.assertEqual(self.client.get("/students/not-a-uuid").status_code, 404)
        self.assertEqual(self.client.get("/students/00000000-0000-0000-0000-000000000000").status_code, 404)

    # -------------------------------------------------------------- adding
    def test_viewer_cannot_add_contact(self):
        self.as_viewer()
        token = self.token(f"/students/{self.ava}")
        resp = self.client.post(f"/students/{self.ava}/guardians",
                                data={"email": "pat@example.com", "csrf_token": token})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.repo.guardians, {})

    def test_admin_adds_contact_and_child_leaves_missing_list(self):
        self.as_admin()
        token = self.token(f"/students/{self.ben}")
        resp = self.client.post(f"/students/{self.ben}/guardians",
                                data={"name": "Pat Lopez", "email": "Pat@Example.com", "csrf_token": token})
        self.assertEqual(resp.status_code, 302)
        (gd,) = self.repo.guardians.values()
        self.assertEqual(gd["email"], "pat@example.com")
        missing = {r["id"] for r in self.repo.billed_without_contact("inst-1")}
        self.assertNotIn(self.ben, missing)
        self.assertIn(self.ava, missing)

    def test_junk_email_rejected_with_message_and_nothing_saved(self):
        self.as_admin()
        token = self.token(f"/students/{self.ava}")
        resp = self.client.post(f"/students/{self.ava}/guardians",
                                data={"email": "redacted", "csrf_token": token})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("look like an email address", resp.get_data(as_text=True))
        self.assertEqual(self.repo.guardians, {})

    def test_sibling_reuses_existing_contact(self):
        self.as_admin()
        token = self.token(f"/students/{self.ava}")
        self.client.post(f"/students/{self.ava}/guardians", data={"email": "pat@example.com", "csrf_token": token})
        resp = self.client.post(f"/students/{self.ben}/guardians", data={"email": "PAT@example.com", "csrf_token": token},
                                follow_redirects=True)
        self.assertEqual(len(self.repo.guardians), 1)
        self.assertEqual(len(self.repo.links), 2)
        self.assertIn("Linked the existing contact", resp.get_data(as_text=True))

    def test_phone_only_contact_still_flags_child(self):
        self.as_admin()
        token = self.token(f"/students/{self.ava}")
        self.client.post(f"/students/{self.ava}/guardians", data={"phone": "410-555-0101", "csrf_token": token})
        (row,) = [r for r in self.repo.billed_without_contact("inst-1") if r["id"] == self.ava]
        self.assertEqual(row["reason"], "no_email")

    # -------------------------------------------------------------- editing
    def _add(self, sid, email):
        res = self.repo.add_guardian_to_student("inst-1", sid, None, email, None, "x", "x")
        return res["guardian_id"]

    def test_admin_edits_contact_and_it_is_audited(self):
        gid = self._add(self.ava, "old@example.com")
        self.as_admin()
        token = self.token(f"/guardians/{gid}")
        resp = self.client.post(f"/guardians/{gid}", data={
            "name": "Pat", "email": "new@example.com", "phone": "", "receives_notices": "on", "csrf_token": token})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.repo.guardians[gid]["email"], "new@example.com")
        self.assertEqual(self.repo.audit_log[-1]["action"], "update_guardian")
        self.assertEqual(self.repo.audit_log[-1]["before"]["email"], "old@example.com")

    def test_unticking_notices_moves_child_to_missing_list(self):
        gid = self._add(self.ava, "pat@example.com")
        self.assertNotIn(self.ava, {r["id"] for r in self.repo.billed_without_contact("inst-1")})
        self.as_admin()
        token = self.token(f"/guardians/{gid}")
        self.client.post(f"/guardians/{gid}", data={"email": "pat@example.com", "csrf_token": token})
        self.assertFalse(self.repo.guardians[gid]["receives_notices"])
        self.assertIn(self.ava, {r["id"] for r in self.repo.billed_without_contact("inst-1")})

    def test_edit_to_email_used_by_another_contact_is_refused(self):
        g1 = self._add(self.ava, "one@example.com")
        self._add(self.ben, "two@example.com")
        self.as_admin()
        token = self.token(f"/guardians/{g1}")
        resp = self.client.post(f"/guardians/{g1}", data={"email": "TWO@example.com", "csrf_token": token})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Another contact already uses", resp.get_data(as_text=True))
        self.assertEqual(self.repo.guardians[g1]["email"], "one@example.com")

    def test_edit_cannot_leave_no_contact_at_all(self):
        gid = self._add(self.ava, "pat@example.com")
        self.as_admin()
        token = self.token(f"/guardians/{gid}")
        resp = self.client.post(f"/guardians/{gid}", data={"email": "", "phone": "", "csrf_token": token})
        self.assertEqual(resp.status_code, 400)

    def test_viewer_cannot_edit_contact(self):
        gid = self._add(self.ava, "pat@example.com")
        self.as_viewer()
        token = self.token(f"/guardians/{gid}")
        resp = self.client.post(f"/guardians/{gid}", data={"email": "evil@example.com", "csrf_token": token})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.repo.guardians[gid]["email"], "pat@example.com")

    def test_back_link_cannot_redirect_off_site(self):
        gid = self._add(self.ava, "pat@example.com")
        self.as_admin()
        token = self.token(f"/guardians/{gid}")
        resp = self.client.post(f"/guardians/{gid}", data={
            "email": "pat@example.com", "receives_notices": "on", "back": "https://evil.example", "csrf_token": token})
        self.assertEqual(resp.headers["Location"], "/")

    def test_admin_unlinks_contact(self):
        gid = self._add(self.ava, "pat@example.com")
        self.as_admin()
        token = self.token(f"/students/{self.ava}")
        self.client.post(f"/students/{self.ava}/guardians/{gid}/unlink", data={"csrf_token": token})
        self.assertEqual(self.repo.links, set())

    # -------------------------------------------------------------- school list
    def test_missing_contacts_page_lists_only_children_who_owe(self):
        self.as_viewer()
        html = self.client.get("/contacts/missing").get_data(as_text=True)
        self.assertIn("Lopez, Ava", html)
        self.assertNotIn("Ng, Cal", html)
        self.assertIn("No parent contact on file", html)
        self.assertNotIn("Import contacts from school roster", html)   # admin-only

    def test_csv_for_school(self):
        self.as_viewer()
        resp = self.client.get("/contacts/missing.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment; filename=\"missing-contacts-sacred-heart-", resp.headers["Content-Disposition"])
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True).lstrip("﻿"))))
        self.assertEqual(rows[0], ["Last name", "First name", "Grade", "Homeroom", "Balance due",
                                   "Unpaid lunches", "Oldest unpaid lunch", "Contact problem"])
        by_first = {r[1]: r for r in rows[1:]}
        self.assertEqual(by_first["Ava"][4], "15.00")
        self.assertEqual(by_first["Ava"][7], "No parent contact on file")
        self.assertIn("'=Dee", by_first)                     # formula neutralized
        self.assertNotIn("Cal", by_first)
        self.assertEqual(self.repo.audit_log[-1]["action"], "export_missing_contacts")

    def test_roster_import_is_admin_only_and_reports_skips(self):
        self.as_viewer()
        token = self.token("/contacts/missing")
        self.assertEqual(self.client.post("/contacts/import-roster", data={"csrf_token": token}).status_code, 403)
        self.client.post("/logout", data={"csrf_token": token})
        self.as_admin()
        self.repo.roster_import_result = {"roster_students": 485, "with_valid_email": 0,
                                          "guardians_created": 0, "links_created": 0}
        token = self.token("/contacts/missing")
        html = self.client.post("/contacts/import-roster", data={"csrf_token": token},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("485 of 485 students have no usable email", html)


if __name__ == "__main__":
    unittest.main()
