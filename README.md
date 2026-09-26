# MealMode Postpaid Billing

Tracks post-paid lunch balances per child, lets parents see check-ins and pay any amount, and sends statements every two weeks. Successor to `SH_Invoicing1.4.html`.

The design lives in the spec doc **MealMode Postpaid Billing — System Spec v2**. This repo is **Phase 0: Foundation**.

| Phase | Status |
| --- | --- |
| 0 — Foundation: schema, staff sign-in and roles, dashboard shell, deploy config | **Done (this code)** |
| 0.5 — Contacts: edit parent contacts, flag billed children with no email, school export | **Done (this code)** |
| 1 — Orders import, check-in sorting, review queue, rates, waivers, offline payments, v1.4 comparison | **Done (this code)** |
| 2 — Parent portal, statements (approve-to-send), receipts, email test mode | **Done (this code)** |
| 3 — Fee line; online payments with Stripe (card + bank), refunds and disputes | **Done (this code)** |
| 4 — Scheduled cycles | |

## What's here

```
migrations/001_billing_schema.sql            every table in the spec, in a `billing` schema
migrations/002_balance_views.sql             v_check_in_ledger, v_student_balances: the only definition of "owed"
migrations/003_contacts_and_checkin_fields.sql  phone-only contacts, no-lunch check-ins,
                                             the missing-contacts and statement-recipient views
migrations/004_excluded_checkins_hardcoded_start.sql  'excluded' check-ins (bill_separately); start date moves to code
migrations/005_default_price_and_allocation.sql  settable standard price ($7.90), apply_credit(), offline payments
migrations/006_meal_increment_payments.sql   parents pay whole lunches, oldest first (v_payment_options + a guard on online payments)
migrations/007_statements_and_portal.sql     full email log (mode, intended vs delivered address, content); portal link versions
app/mailer.py                                SendGrid + the outbox/test/live safety switch
app/notify.py, app/portal.py                 statements, receipts, parent portal links
migrations/008_fee_line_and_stripe.sql       processing fee on top of the meal price (locks with it); Stripe event log; record_stripe_payment()
app/online.py, app/stripe_client.py          Stripe Checkout, webhook handling, signature checks
migrations/009_stripe_tax.sql                sales tax collected by Stripe Tax, kept beside each payment
app/payments.py                              the parent payment-amount rule, for phase 3 checkout
app/importer.py, app/names.py                orders CSV parsing; name matching ported from v1.4
app/classify.py                              the sorting run: match orders, sort check-ins, late orders, credit
app/compare.py                               parallel run against a v1.4 to_invoice file
app/billing_rules.py                         hard-coded billing start date and the check-in classification rules
app/                                         Flask app: sign-in, roles, dashboard, students, contacts, CLI
tests/test_app.py, tests/test_contacts.py    web layer with in-memory fakes
tests/test_phase1-3.py                       end to end: real pages, real SQL, recording email backend, fake Stripe
tests/test_repo_sql.py                       the app's real SQL against real Postgres, with roster data shaped like the export
tests/sql/test_*.sql                        133 checks that the database enforces the money and contact rules
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

New service → Deploy from GitHub repo. Set variables: `APP_ENV=production`, `SECRET_KEY` (48+ random characters), `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `INSTITUTION_SLUG=sacred-heart`, plus the email and portal variables below. After the first deploy, set `PUBLIC_BASE_URL` to the Railway URL (Settings → Networking → Generate domain) and redeploy. `railway.json` sets the gunicorn start command and a `/healthz` health check.

Migrations are **not** run on deploy on purpose: a billing schema change should be applied by a person who has read it.

## Phase 1: using it

**Every week or two:**
1. **Orders → Upload** the ALL ORDERS export and enter when you downloaded it. Check-ins are sorted automatically.
   Only days *before* the download time are billed, so a missing upload delays billing instead of charging families who pre-ordered.
2. **Review** anything the app wouldn't guess: order names it couldn't match to exactly one student. Pick the child once; every past and future order under that name follows. Matching re-sorts affected days automatically (unpaid ones only).
3. **Students → a child** shows every check-in and how it was sorted, what's paid, and a **Waive** button (reason required).
   **Record a payment** there for cash, checks, or Zoho: it pays the oldest unpaid lunches first; extra becomes credit that pays future lunches automatically.

**What parents can pay** (from phase 3): whole lunches only, oldest first, each at its own price. Pay for 1 lunch, 2 lunches, … up to everything owed; with a $5.00 promo lunch then two $7.90 lunches, the choices are $5.00, $12.90, and $20.80. The student page shows each child's options. The database refuses to start an online payment for any other amount. **Staff-recorded payments are not restricted**: record what was actually handed over, and any remainder is credit.

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
- If a price changes between a parent opening checkout and the payment landing (rare), the payment still applies oldest-first; the last lunch may end up part-paid, and the parent's next options start with its remainder.
- Payments are never edited or deleted. A mistaken or bounced one is **reversed** from the student page (reason required): the lunches it paid become unpaid again, and the reversal stays on record.

## Phase 2: statements, emails, parent page

### Settings

| Variable | Value |
| --- | --- |
| `EMAIL_MODE` | `outbox` (default: nothing is emailed, messages are stored under **Emails**), `test` (every email goes to `EMAIL_TEST_RECIPIENT`, never to parents), `live` (parents; must be set on purpose) |
| `EMAIL_TEST_RECIPIENT` | `trichtoure@gmail.com` |
| `SENDGRID_API_KEY` | SendGrid → Settings → API Keys, "Restricted: Mail Send" |
| `EMAIL_FROM` | a sender verified in SendGrid (Single Sender or domain authentication) |
| `EMAIL_FROM_NAME` | optional, default `MealMode` |
| `SUPPORT_EMAIL` | optional; shown to parents as the contact for questions and offline payment |
| `PORTAL_SECRET` | 32+ random characters. Signs parent links; **changing it breaks every link already emailed** |
| `PUBLIC_BASE_URL` | where parents open the app, e.g. `https://xxx.up.railway.app`. Defaults to `http://127.0.0.1:5000` in development |

The app refuses to start if `test`/`live` is missing its key, sender, or test inbox. The redirect to the test inbox happens inside the mailer, below every feature, so nothing can reach a parent unless the app was started with `EMAIL_MODE=live`. Every staff page shows a banner with the current mode.

### Using it

- **Statements**: every parent with an email who has a child owing, with totals. Filter by minimum owed. Admins tick "I've reviewed this list" and send; if the list changed since it was loaded, the send is refused. A parent who got a statement in the last 24 hours is skipped unless you tick the override.
- **Contact page**: "Send statement now", "Open this parent's page" (exactly what they see), and for super admins "Replace their private link" (old links stop working).
- **Receipts** go automatically to the child's parents when a payment is recorded (same mode rules).
- **Emails**: every statement and receipt with the address it was meant for, where it actually went, status, and the exact content. Failed sends are kept with the error.
- **Parent page** (`/p/<link>`, no sign-in): each child's balance, whole-lunch pay amounts (oldest first), every lunch (pre-ordered, post-paid with price and status, checked in without lunch), payments, and the fine print. Online payment arrives in phase 3; until then it points to `SUPPORT_EMAIL`. The link is the key, so the page is not indexed, not cached, sends no referrer, and is rate-limited. Only a hash of each link is stored.

## Phase 3: fee line and online payments

### Fee line
**Rates → Payment processing fee per lunch** (super admin). It's added on top of the meal price: $7.90 meal + $0.35 fee = $8.25. Parents see both parts on the parent page and in statements. The fee is part of every lunch's price however the parent pays, cash and check included, so it isn't a surcharge on online payments. A special-price period can set its own fee; leave the field blank to use the standard fee. The fee locks together with the price when money lands on a lunch. It starts at $0.

### Stripe setup
1. In the Stripe dashboard (test mode first): **Settings → Payment methods**, make sure **Cards** and **ACH Direct Debit** are on.
2. **Developers → API keys**: copy the **secret key** (`sk_test_...`).
3. **Developers → Webhooks → Add endpoint**: URL `https://<your Railway domain>/stripe/webhook`. Events: `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, `checkout.session.expired`, `charge.refunded`, `charge.dispute.created`, `charge.dispute.closed`. Copy its **signing secret** (`whsec_...`).
4. Railway variables: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`. Leave both unset to keep online payments off. The app refuses to start with only one, with a publishable `pk_` key, or with a signing secret that doesn't start with `whsec_`.
5. Run `flask --app wsgi db migrate` (008) before deploying this code.

Going live later: repeat steps 2–4 with live-mode keys and a live-mode webhook. Test-mode and live-mode data never mix.

### How it works
- The parent taps **Pay $X** next to a whole-lunch amount and pays on Stripe's hosted page by card or bank account. Card and bank details never reach the app.
- Nothing counts until Stripe confirms. A card payment is recorded at once: it's applied oldest-first, the prices lock, and receipts go out. A bank payment shows as **Clearing** for a few business days and is recorded when the money arrives. While a bank payment is clearing, that child's Pay buttons are hidden so nobody pays twice.
- Every Stripe event is verified by its signature and processed once; retries are harmless.
- **Refunds:** refund in the Stripe dashboard. A full refund reverses the payment in the app and reopens those lunches. A partial refund changes nothing and is flagged on **Online payments** for a person to settle.
- **Disputes and bank returns:** the payment is reversed automatically and flagged. If you win the dispute, it's flagged again so you can re-record the money.
- **Online payments** (staff) lists every attempt with status, what needs attention, and a link to it in Stripe.

### Sales tax (Stripe Tax, optional)
Off by default. When on, Stripe adds sales tax at checkout, based on the payer's address, on top of the lunch amount.
- **Online payments only.** Cash and check payments recorded by staff carry no tax.
- **Kept apart from lunches.** The tax is stored beside the payment and shown on receipts, the parent page, the student page and Online payments. Only the lunch amount is applied to lunches.
- **Refunds.** A full refund in Stripe (tax included) reverses the payment as usual.

To turn it on:
1. Set up tax in Stripe: Settings → Tax. Add the business address and your Maryland registration.
2. Pick a product tax code for the lunches.
3. In Railway, set `STRIPE_AUTOMATIC_TAX=true`, plus `STRIPE_TAX_CODE=txcd_...` for the code you picked.
4. Run `flask --app wsgi db migrate` to apply 009.

Turn it on only after step 1, or Stripe refuses to open checkout. Stripe charges its own fee for Stripe Tax.

## Contacts

Parent contacts live in `billing.guardians`, not in the check-in app's `students` table, which billing never writes to.

- **Students** lists every child with balance and contact status. The filter shows only children who owe money with no email.
- **Student page**: add a contact (name, email, phone). An email that already belongs to a contact links that contact instead of creating a duplicate, so siblings share one parent record. Admins can remove a contact from a child.
- **Contact page**: edit name, email, phone, and whether they get emails. Changes apply to every child they're linked to and are recorded in the audit log.
- **Missing contacts**: every child who owes money but can't be emailed, with the reason (no contact, phone only, opted out). **Download list for the school** gives a CSV with name, grade, homeroom, balance, and the problem, ready to send so the school can supply contacts.
- **Import contacts from school roster** copies valid emails and phones from `students.email` / `students.phone`. Placeholders such as `redacted`, blanks, and malformed values are skipped; siblings with the same email get one shared contact. Safe to run again.

Children without a usable email are simply **skipped** by statements. Nothing errors; they stay on the Missing contacts list until fixed. Statement sending reads only from `billing.v_statement_recipients`.

## Tests

```bash
python -m unittest discover -s tests -t . -v                   # web layer; DB tests skip
TEST_PG="host=localhost user=postgres" python -m unittest discover -s tests -t . -v
PGHOST=localhost PGUSER=postgres scripts/test_sql.sh           # 133 schema checks
```

The last two need a **local** Postgres 14+ you can create databases on. Never point them at Supabase.

## Not yet verified

These were built in an environment that couldn't install packages, so be aware:

- `app/db.py` (the psycopg connection pool) has **never executed**. Every SQL statement it will run *has* been tested against Postgres 16 via `psql`, but the first real run of the Python-to-database path will be yours. Run `flask --app wsgi db status` before anything else.
- Supabase Auth calls (`app/auth.py`) are tested against a fake, not the real service.
- Migrations were tested on Postgres 16, not on Supabase itself.
- Stripe is tested with a stand-in and with webhook events signed the way Stripe signs them. Before going live, run a real test-mode payment end to end: card `4242 4242 4242 4242`, and a test bank account on the Checkout page, then check **Online payments**.
- SendGrid is tested with a stand-in that records messages. The first real send is yours: set `EMAIL_MODE=test`, send one statement from a contact page, and check the test inbox (and spam).

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
