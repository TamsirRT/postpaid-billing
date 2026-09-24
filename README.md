# MealMode Postpaid Billing

Tracks post-paid lunch balances per child, lets parents see check-ins and pay any amount, and sends statements every two weeks. Successor to `SH_Invoicing1.4.html`.

The design lives in the spec doc **MealMode Postpaid Billing — System Spec v2**. This repo is **Phase 0: Foundation**.

| Phase | Status |
| --- | --- |
| 0 — Foundation: schema, staff sign-in and roles, dashboard shell, deploy config | **Done (this code)** |
| 0.5 — Contacts: edit parent contacts, flag billed children with no email, school export | **Done (this code)** |
| 1 — Orders import, check-in sorting, review queue, rates, waivers, offline payments, v1.4 comparison | **Done (this code)** |
| 2 — Parent portal and manual statement sends | |
| 3 — Stripe (bank + card) | |
| 4 — Scheduled cycles | |

## What's here

```
migrations/001_billing_schema.sql            every table in the spec, in a `billing` schema
migrations/002_balance_views.sql             v_check_in_ledger, v_student_balances: the only definition of "owed"
migrations/003_contacts_and_checkin_fields.sql  phone-only contacts, no-lunch check-ins,
                                             the missing-contacts and statement-recipient views
migrations/004_excluded_checkins_hardcoded_start.sql  'excluded' check-ins (bill_separately); start date moves to code
migrations/005_default_price_and_allocation.sql  settable standard price ($7.90), apply_credit(), offline payments
app/importer.py, app/names.py                orders CSV parsing; name matching ported from v1.4
app/classify.py                              the sorting run: match orders, sort check-ins, late orders, credit
app/compare.py                               parallel run against a v1.4 to_invoice file
app/billing_rules.py                         hard-coded billing start date and the check-in classification rules
app/                                         Flask app: sign-in, roles, dashboard, students, contacts, CLI
tests/test_app.py, tests/test_contacts.py    web layer with in-memory fakes
tests/test_repo_sql.py                       the app's real SQL against real Postgres, with roster data shaped like the export
tests/sql/test_*.sql                        98 checks that the database enforces the money and contact rules
tests/sql/stub_public.sql                    stand-ins for students / check_ins, column for column from the exports
```

Rules the **database** enforces, so no bug in the app can break them:

- Rate periods can't overlap (Postgres `EXCLUDE` constraint).
- One billable lunch per child per day.
- An unpaid lunch's price follows the rate schedule; once money is applied, the price locks and can't be edited.
- Money can't be applied beyond a lunch's price, beyond a payment's amount, to another child's lunch, to a waived lunch, or from a reversed payment.
- Payments, allocations, reversals, and the audit log can't be updated or deleted.
- Row-level security is on for every billing table with no policies, so Supabase's public API can't read billing data. The app's server connection bypasses it.

- A child who owes money but has no guardian with an email (who accepts notices) can never be handed to statement sending, and the database refuses a notification row for a guardian without an email.

Nothing in `billing` has a foreign key into `public`. The check-in app's `students` and `check_ins` tables stay untouched and read-only.

## Setup

### 1. Repo

Create a GitHub repo under a **MealMode-owned** account (not a personal one), then make the first commit and push:

```bash
git add . && git commit -m "Phase 0: schema, staff auth, dashboard shell"
git remote add origin git@github.com:<mealmode-org>/postpaid-billing.git
git push -u origin main
```

### 2. Local environment

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                               # then fill it in
```

`DATABASE_URL` is the Sacred Heart Supabase project's **session pooler** connection string (Supabase → Connect). `SUPABASE_URL` and `SUPABASE_ANON_KEY` come from Project Settings → API.

### 3. Database

Check first, then apply. Each migration runs in its own transaction and is recorded in `billing.schema_migrations`; an applied file that later changes is refused.

```bash
flask --app wsgi db status
flask --app wsgi db migrate
```

Only creates the `billing` and `extensions` schemas and the `btree_gist` extension. It does not touch `public`.

### 4. Institution

The anchor date is the first day of the first 14-day billing cycle. **Pick it deliberately**; nothing here guesses it.

```bash
flask --app wsgi institution create --slug sacred-heart \
  --name "Sacred Heart School of Glyndon" \
  --location "Sacred Heart School of Glyndon" --module "Order" \
  --anchor YYYY-MM-DD
```

The billing start date is **hard-coded** in `app/billing_rules.py` (Sacred Heart: Aug 31, 2026, the first check-in day of the 2026–27 year). Check-ins before it are never billed. A school with no entry there bills nothing, and the dashboard says so. Changing it is a code change.

### 5. First super admin

1. Run the app (`flask --app wsgi run`, or deploy), open `/signup`, create your account.
2. Sign in once. You'll land on "Waiting for access".
3. Grant yourself the role from the command line:

```bash
flask --app wsgi staff grant you@example.com super_admin
```

After that, grant everyone else from the **Staff** page.

### 6. Railway

New service → Deploy from GitHub repo. Set variables: `APP_ENV=production`, `SECRET_KEY` (48+ random characters), `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `INSTITUTION_SLUG=sacred-heart`. `railway.json` sets the gunicorn start command and a `/healthz` health check.

Migrations are **not** run on deploy on purpose: a billing schema change should be applied by a person who has read it.

## Phase 1: using it

**Every week or two:**
1. **Orders → Upload** the ALL ORDERS export and enter when you downloaded it. Check-ins are sorted automatically.
   Only days *before* the download time are billed, so a missing upload delays billing instead of charging families who pre-ordered.
2. **Review** anything the app wouldn't guess: order names it couldn't match to exactly one student. Pick the child once; every past and future order under that name follows. Matching re-sorts affected days automatically (unpaid ones only).
3. **Students → a child** shows every check-in and how it was sorted, what's paid, and a **Waive** button (reason required).
   **Record a payment** there for cash, checks, or Zoho: it pays the oldest unpaid lunches first; extra becomes credit that pays future lunches automatically.

**Rates** (super admin): the standard price per post-paid lunch (starts at $7.90) and optional special-price periods for promos. Unpaid lunches follow price changes immediately; a lunch's price locks once any money lands on it. A period that paid lunches depend on can't be deleted.

**How check-ins are sorted** (`app/billing_rules.py`, first match wins): before Aug 31, 2026 → not billed · `getting_lunch = false` → no lunch · `bill_separately = true` → not included · second check-in that day → duplicate · non-refunded order that day → pre-ordered · otherwise → **post-paid**.

**Parallel run against v1.4:**
```bash
flask --app wsgi billing compare-v14 path/to/to_invoice.csv --from 2026-08-31 --to 2026-09-24
```

### Acceptance result (Sep 24, 2026, real data)
Students export, 13,803 check-ins, and the ALL ORDERS export, run through the real SQL, compared with v1.4's `to_invoice_2025-08-31_to_2026-09-24.csv` for Aug 31–Sep 18, 2026:

| | v1.4 | This app |
| --- | --- | --- |
| Post-paid lunches | 306 | 301 |
| Total | $2,417.40 | $2,377.90 |
| Same lunch, different price | | 0 |
| Billed only by this app | | 0 |

All 5 differences are check-ins with `getting_lunch = false`, which v1.4 billed and this app doesn't (5 × $7.90 = $39.50). A second run added nothing.

16 order names from the billing period need matching in Review. From first names, about 7 lunches ($55.30) for three children look like days they had orders; **both** tools currently bill those. Matching them in Review fixes it.

### Known limits
- A price change shows its impact (lunches and dollars affected) right *after* saving, not as a preview before. Paid lunches are never affected.
- Payments are never edited or deleted. A mistaken or bounced one is **reversed** from the student page (reason required): the lunches it paid become unpaid again, and the reversal stays on record.

## Contacts

Parent contacts live in `billing.guardians`, not in the check-in app's `students` table, which billing never writes to.

- **Students** lists every child with balance and contact status. The filter shows only children who owe money with no email.
- **Student page**: add a contact (name, email, phone). An email that already belongs to a contact links that contact instead of creating a duplicate, so siblings share one parent record. Admins can remove a contact from a child.
- **Contact page**: edit name, email, phone, and whether they get emails. Changes apply to every child they're linked to and are recorded in the audit log.
- **Missing contacts**: every child who owes money but can't be emailed, with the reason (no contact, phone only, opted out). **Download list for the school** gives a CSV with name, grade, homeroom, balance, and the problem, ready to send so the school can supply contacts.
- **Import contacts from school roster** copies valid emails and phones from `students.email` / `students.phone`. Placeholders such as `redacted`, blanks, and malformed values are skipped; siblings with the same email get one shared contact. Safe to run again.

Children without a usable email are simply **skipped** by statements. Nothing errors; they stay on the Missing contacts list until fixed. Statement sending (phase 2) must read only from `billing.v_statement_recipients`.

## Tests

```bash
python -m unittest discover -s tests -t . -v                   # web layer; DB tests skip
TEST_PG="host=localhost user=postgres" python -m unittest discover -s tests -t . -v
PGHOST=localhost PGUSER=postgres scripts/test_sql.sh           # 98 schema checks
```

The last two need a **local** Postgres 14+ you can create databases on. Never point them at Supabase.

## Not yet verified

These were built in an environment that couldn't install packages, so be aware:

- `app/db.py` (the psycopg connection pool) has **never executed**. Every SQL statement it will run *has* been tested against Postgres 16 via `psql`, but the first real run of the Python-to-database path will be yours. Run `flask --app wsgi db status` before anything else.
- Supabase Auth calls (`app/auth.py`) are tested against a fake, not the real service.
- Migrations were tested on Postgres 16, not on Supabase itself.

## Findings from the real exports (September 2026)

**Orders**
- `User ID` is a **parent login, not a child**: 47 of 187 Sacred Heart IDs ordered for 2–3 differently named children. Matching uses (user ID, child name), per `billing.ordering_user_map`.
- `Order Date` is `YYYY-MM-DD`. v1.4 parsed it as day-first; the importer will read ISO.
- 49 of 240 Sacred Heart order names don't match any roster student by name. Only 2 are `User Group = Staff`; the other 47 carry a grade (PK–8), so they're most likely nicknames, spelling differences, or students missing from `students`. Each needs one manual match in the review queue, then the mapping remembers it.

**Students**
- Has `email` and `phone` columns. In the test export every value is `redacted`, so a roster import there creates nothing and every billed child shows on Missing contacts. That's the intended behavior.
- `pin` exists. Billing never selects it (a test fails the build if any query mentions it).

**Check-ins** (13,803 rows). Rules live in `classify_check_in` in `app/billing_rules.py`, first match wins: before start date → not billed · `getting_lunch = false` → `no_lunch` · `bill_separately = true` → `excluded` · second check-in that day → `duplicate` · order that day → `pre_ordered` · otherwise → `post_paid`.

- `id` is a `uuid`, as assumed. `check_in_date` is a plain date and is used as the service date.
- **`getting_lunch = false` on 174 check-ins**: checked in, no lunch. Classified `no_lunch`, never billed.
- **History starts 2025-08-26.** The hard-coded start date (Aug 31, 2026) keeps last school year out of billing.
- At most one check-in per child per day in this data. The database still guarantees it.
- **`bill_separately = true` on 28 check-ins** means **do not include** (confirmed). Classified `excluded`, never billed.
- `notes` has free text such as "manual" and "manually entered after the fact". Kept in the check-in app; billing doesn't need it.
- For 67 check-ins, `check_in_date` differs from the Eastern-time date of `check_in_time` (when the row was recorded). 24 are dated earlier than recorded (entered after the fact). 43 are dated **later** than recorded, 24 of them by exactly one day and the rest by up to 26 days. Only 14 of the 67 have a note. **Decision: bill them normally by `check_in_date`**; when the row was recorded is ignored.
