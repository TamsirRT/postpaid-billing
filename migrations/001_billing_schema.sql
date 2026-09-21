-- 001_billing_schema.sql
-- MealMode postpaid billing: core schema.
--
-- Design rules (see "MealMode Postpaid Billing — System Spec v2"):
--   * Everything lives in the `billing` schema. The check-in app's tables in
--     `public` (students, check_ins) are READ-ONLY to billing.
--   * No foreign keys point into `public`. A FK would stop the check-in app
--     from deleting a student or check-in that billing references, coupling the
--     two apps. Orphaned references are caught by an integrity check instead.
--   * Money is integer cents. Never floats.
--   * Money rows (payments, allocations, reversals) and the audit log are
--     append-only: UPDATE and DELETE are rejected by trigger.
--   * A balance is never stored. It is derived by the views in 002.

create schema if not exists billing;
create schema if not exists extensions;
create extension if not exists btree_gist with schema extensions;

-- ---------------------------------------------------------------------------
-- Institutions
-- ---------------------------------------------------------------------------
create table billing.institutions (
    id                      uuid primary key default gen_random_uuid(),
    slug                    text not null unique check (slug ~ '^[a-z0-9-]+$'),
    name                    text not null,
    ordering_location_name  text not null unique,   -- e.g. 'Sacred Heart School of Glyndon'
    ordering_module_name    text not null default 'Order',
    cycle_anchor_date       date not null,          -- first day of the first billing cycle
    cycle_length_days       integer not null default 14 check (cycle_length_days between 1 and 90),
    auto_send_enabled       boolean not null default false,
    timezone                text not null default 'America/New_York',
    created_at              timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Staff (Supabase auth users). A sign-in with role = NULL has no access until
-- a super admin grants one.
-- ---------------------------------------------------------------------------
create table billing.staff_roles (
    user_id        uuid primary key,               -- auth.users.id
    email          text not null,
    role           text check (role in ('viewer', 'admin', 'super_admin')),
    granted_by     uuid references billing.staff_roles(user_id),
    granted_at     timestamptz,
    first_seen_at  timestamptz not null default now(),
    last_seen_at   timestamptz not null default now(),
    check ((role is null) = (granted_at is null))
);

-- ---------------------------------------------------------------------------
-- Guardians and their links to children. Billing unit is the CHILD; a
-- guardian is who gets notified and holds a portal link.
-- ---------------------------------------------------------------------------
create table billing.guardians (
    id                 uuid primary key default gen_random_uuid(),
    institution_id     uuid not null references billing.institutions(id),
    name               text not null,
    email              text not null check (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    receives_notices   boolean not null default true,
    portal_token_hash  bytea unique,                -- sha256 of the token; token itself is never stored
    token_issued_at    timestamptz,
    created_at         timestamptz not null default now(),
    check ((portal_token_hash is null) = (token_issued_at is null))
);
create unique index guardians_institution_email_uq
    on billing.guardians (institution_id, lower(email));

create table billing.guardian_students (
    guardian_id     uuid not null references billing.guardians(id) on delete cascade,
    student_id      uuid not null,                  -- public.students.id (no FK, see header)
    institution_id  uuid not null references billing.institutions(id),
    created_at      timestamptz not null default now(),
    primary key (guardian_id, student_id)
);
create index guardian_students_student_idx on billing.guardian_students (student_id);

-- ---------------------------------------------------------------------------
-- Rate periods. Overlaps are rejected by the database, not just the UI.
-- ---------------------------------------------------------------------------
create table billing.rate_periods (
    id              uuid primary key default gen_random_uuid(),
    institution_id  uuid not null references billing.institutions(id),
    starts_on       date not null,
    ends_on         date not null,
    price_cents     integer not null check (price_cents >= 0),
    label           text,
    created_by      uuid references billing.staff_roles(user_id),
    created_at      timestamptz not null default now(),
    check (starts_on <= ends_on),
    constraint rate_periods_no_overlap exclude using gist (
        institution_id with =,
        daterange(starts_on, ends_on, '[]') with &&
    )
);

-- ---------------------------------------------------------------------------
-- Orders imported from the ordering platform's CSV export.
-- ---------------------------------------------------------------------------
create table billing.import_batches (
    id                 uuid primary key default gen_random_uuid(),
    institution_id     uuid not null references billing.institutions(id),
    filename           text not null,
    uploaded_by        uuid references billing.staff_roles(user_id),
    uploaded_at        timestamptz not null default now(),
    exported_at        timestamptz not null,       -- when the export was taken; gates classification
    row_count          integer not null default 0 check (row_count >= 0),
    matched_row_count  integer not null default 0 check (matched_row_count >= 0)
);

create table billing.imported_orders (
    id                 bigint generated always as identity primary key,
    batch_id           uuid not null references billing.import_batches(id),
    institution_id     uuid not null references billing.institutions(id),
    external_order_id  text not null,
    ordering_user_id   text not null,              -- a PARENT login; one ID can order for several children
    raw_user_name      text not null,
    name_key           text,                       -- normalized 'first|last'; null if unusable
    service_date       date not null,
    product_name       text,
    is_refunded        boolean not null default false,
    student_id         uuid,                       -- null until matched
    source_row_hash    text not null unique        -- makes re-uploading the same export harmless
);
create index imported_orders_student_date_idx on billing.imported_orders (student_id, service_date);
create index imported_orders_institution_date_idx on billing.imported_orders (institution_id, service_date);

-- (parent login, child name) -> student. Keyed on the pair because the
-- ordering platform's user ID belongs to a parent, not a child.
create table billing.ordering_user_map (
    id                uuid primary key default gen_random_uuid(),
    institution_id    uuid not null references billing.institutions(id),
    ordering_user_id  text not null,
    name_key          text not null,
    student_id        uuid not null,
    mapped_by         uuid references billing.staff_roles(user_id),   -- null = matched automatically
    mapped_at         timestamptz not null default now(),
    unique (institution_id, ordering_user_id, name_key)
);

-- ---------------------------------------------------------------------------
-- Classification runs and the review queue.
-- ---------------------------------------------------------------------------
create table billing.reconciliation_runs (
    id              uuid primary key default gen_random_uuid(),
    institution_id  uuid not null references billing.institutions(id),
    period_start    date not null,
    period_end      date not null,
    started_by      uuid references billing.staff_roles(user_id),   -- null = nightly job
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    status          text not null default 'running' check (status in ('running', 'succeeded', 'failed')),
    counts          jsonb not null default '{}'::jsonb,
    error           text,
    check (period_start <= period_end)
);

create table billing.review_items (
    id                   uuid primary key default gen_random_uuid(),
    institution_id       uuid not null references billing.institutions(id),
    run_id               uuid references billing.reconciliation_runs(id),
    source               text not null check (source in ('order', 'check_in', 'late_order', 'integrity')),
    raw_reference        text not null,
    service_date         date,
    reason               text not null,
    status               text not null default 'open' check (status in ('open', 'resolved', 'dismissed')),
    resolved_student_id  uuid,
    resolved_by          uuid references billing.staff_roles(user_id),
    resolved_at          timestamptz,
    created_at           timestamptz not null default now(),
    check ((status = 'open') = (resolved_at is null))
);
-- A nightly job re-finding the same problem must not open a second item.
create unique index review_items_open_dedupe_uq
    on billing.review_items (institution_id, source, raw_reference, coalesce(service_date, '-infinity'::date), reason)
    where status = 'open';

-- ---------------------------------------------------------------------------
-- The bill: one row per classified check-in.
-- ---------------------------------------------------------------------------
create table billing.check_in_billing (
    check_in_id            uuid primary key,       -- public.check_ins.id (no FK, see header)
    institution_id         uuid not null references billing.institutions(id),
    student_id             uuid not null,          -- denormalized from check_ins for fast balance queries
    service_date           date not null,          -- local (institution timezone) date of the check-in
    classification         text not null check (classification in ('post_paid', 'pre_ordered', 'duplicate')),
    classified_at          timestamptz not null default now(),
    run_id                 uuid references billing.reconciliation_runs(id),
    locked_price_cents     integer check (locked_price_cents >= 0),
    locked_rate_period_id  uuid references billing.rate_periods(id),
    locked_at              timestamptz,
    waived_at              timestamptz,
    waived_by              uuid references billing.staff_roles(user_id),
    waive_reason           text,
    check ((locked_price_cents is null) = (locked_at is null)),
    check ((waived_at is null) = (waive_reason is null)),
    check ((waived_at is null) = (waived_by is null)),
    -- only post-paid check-ins carry a price or a waiver
    check (classification = 'post_paid' or (locked_at is null and waived_at is null))
);
-- One billable lunch per student per day, enforced by the database.
create unique index check_in_billing_one_post_paid_per_day_uq
    on billing.check_in_billing (student_id, service_date)
    where classification = 'post_paid';
create index check_in_billing_institution_date_idx on billing.check_in_billing (institution_id, service_date);

-- ---------------------------------------------------------------------------
-- Money: payments, allocations, reversals. Append-only.
-- ---------------------------------------------------------------------------
create table billing.payments (
    id                   uuid primary key default gen_random_uuid(),
    institution_id       uuid not null references billing.institutions(id),
    student_id           uuid not null,            -- payments are made toward ONE child
    paid_by_guardian_id  uuid references billing.guardians(id),
    amount_cents         integer not null check (amount_cents > 0),
    method               text not null check (method in ('card', 'ach', 'cash', 'check', 'zoho', 'other')),
    processor_ref        text unique,              -- Stripe PaymentIntent id; makes webhook retries harmless
    received_at          timestamptz not null default now(),
    recorded_by          uuid references billing.staff_roles(user_id),   -- null for Stripe
    note                 text,
    created_at           timestamptz not null default now(),
    -- every payment is either from the processor or recorded by a person
    check (processor_ref is not null or recorded_by is not null)
);
create index payments_student_idx on billing.payments (student_id, received_at);

-- In-flight processor payments (a bank debit takes days to settle). Mutable.
-- A row in billing.payments is written only when the money has settled, so
-- the portal shows these as "pending" and they never count toward a balance.
create table billing.payment_intents (
    id                   uuid primary key default gen_random_uuid(),
    institution_id       uuid not null references billing.institutions(id),
    student_id           uuid not null,
    guardian_id          uuid references billing.guardians(id),
    amount_cents         integer not null check (amount_cents > 0),
    method               text check (method in ('card', 'ach')),
    processor_ref        text not null unique,     -- Stripe Checkout Session / PaymentIntent id
    status               text not null default 'pending' check (status in ('pending', 'succeeded', 'failed', 'canceled')),
    payment_id           uuid references billing.payments(id),
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    check ((status = 'succeeded') = (payment_id is not null))
);
create index payment_intents_student_idx on billing.payment_intents (student_id, status);

create table billing.payment_reversals (
    id             uuid primary key default gen_random_uuid(),
    payment_id     uuid not null unique references billing.payments(id),
    reason         text not null,
    processor_ref  text,
    recorded_by    uuid references billing.staff_roles(user_id),
    reversed_at    timestamptz not null default now()
);

create table billing.payment_allocations (
    id           bigint generated always as identity primary key,
    payment_id   uuid not null references billing.payments(id),
    check_in_id  uuid not null references billing.check_in_billing(check_in_id),
    amount_cents integer not null check (amount_cents > 0),
    created_at   timestamptz not null default now(),
    unique (payment_id, check_in_id)
);
create index payment_allocations_check_in_idx on billing.payment_allocations (check_in_id);

-- ---------------------------------------------------------------------------
-- Cycles and notifications.
-- ---------------------------------------------------------------------------
create table billing.billing_cycles (
    id                  uuid primary key default gen_random_uuid(),
    institution_id      uuid not null references billing.institutions(id),
    period_start        date not null,
    period_end          date not null,
    status              text not null default 'draft' check (status in ('draft', 'blocked', 'approved', 'sent')),
    approved_by         uuid references billing.staff_roles(user_id),
    approved_at         timestamptz,
    sent_at             timestamptz,
    sent_automatically  boolean not null default false,
    created_at          timestamptz not null default now(),
    unique (institution_id, period_start),
    check (period_start <= period_end)
);

create table billing.notifications (
    id                     uuid primary key default gen_random_uuid(),
    institution_id         uuid not null references billing.institutions(id),
    guardian_id            uuid not null references billing.guardians(id),
    cycle_id               uuid references billing.billing_cycles(id),
    kind                   text not null check (kind in ('statement', 'receipt', 'manual_individual', 'manual_global')),
    balance_cents_at_send  jsonb not null default '{}'::jsonb,   -- {student_id: cents} as stated in the email
    sendgrid_message_id    text,
    status                 text not null default 'queued' check (status in ('queued', 'sent', 'delivered', 'bounced', 'failed')),
    sent_by                uuid references billing.staff_roles(user_id),   -- null = system
    created_at             timestamptz not null default now()
);
create index notifications_guardian_idx on billing.notifications (guardian_id, created_at);

-- ---------------------------------------------------------------------------
-- Audit log. Append-only.
-- ---------------------------------------------------------------------------
create table billing.audit_log (
    id              bigint generated always as identity primary key,
    institution_id  uuid references billing.institutions(id),
    actor           uuid,                          -- staff user id; null = system
    actor_email     text,
    action          text not null,
    entity          text not null,
    entity_id       text,
    before          jsonb,
    after           jsonb,
    at              timestamptz not null default now()
);
create index audit_log_entity_idx on billing.audit_log (entity, entity_id);
create index audit_log_at_idx on billing.audit_log (at);

-- ---------------------------------------------------------------------------
-- Append-only enforcement.
-- ---------------------------------------------------------------------------
create function billing.reject_mutation() returns trigger
language plpgsql as $$
begin
    raise exception '% on billing.% is not allowed: this table is append-only', tg_op, tg_table_name
        using errcode = 'restrict_violation';
end;
$$;

create trigger payments_append_only            before update or delete on billing.payments            for each row execute function billing.reject_mutation();
create trigger payment_allocations_append_only before update or delete on billing.payment_allocations for each row execute function billing.reject_mutation();
create trigger payment_reversals_append_only   before update or delete on billing.payment_reversals   for each row execute function billing.reject_mutation();
create trigger audit_log_append_only           before update or delete on billing.audit_log           for each row execute function billing.reject_mutation();

-- ---------------------------------------------------------------------------
-- Allocation invariants. Neither fits in a CHECK constraint, so a trigger
-- enforces them at insert time, under row locks:
--   1. the check-in is post-paid, not waived, and its price is already locked
--   2. allocations to a check-in never exceed its locked price
--   3. allocations from a payment never exceed the payment
--   4. a reversed payment cannot be allocated
--   5. the payment and the check-in belong to the same child
-- ---------------------------------------------------------------------------
create function billing.check_allocation() returns trigger
language plpgsql as $$
declare
    c   billing.check_in_billing%rowtype;
    p   billing.payments%rowtype;
    already_on_check_in  bigint;
    already_from_payment bigint;
begin
    select * into c from billing.check_in_billing where check_in_id = new.check_in_id for update;
    select * into p from billing.payments where id = new.payment_id for update;

    if c.classification <> 'post_paid' then
        raise exception 'check-in % is %, not post_paid', c.check_in_id, c.classification using errcode = 'check_violation';
    end if;
    if c.waived_at is not null then
        raise exception 'check-in % is waived', c.check_in_id using errcode = 'check_violation';
    end if;
    if c.locked_price_cents is null then
        raise exception 'check-in % has no locked price; lock it before allocating', c.check_in_id using errcode = 'check_violation';
    end if;
    if p.student_id <> c.student_id then
        raise exception 'payment % is for a different child than check-in %', p.id, c.check_in_id using errcode = 'check_violation';
    end if;
    if exists (select 1 from billing.payment_reversals r where r.payment_id = p.id) then
        raise exception 'payment % has been reversed', p.id using errcode = 'check_violation';
    end if;

    select coalesce(sum(a.amount_cents), 0) into already_on_check_in
      from billing.payment_allocations a
      join billing.payments pp on pp.id = a.payment_id
     where a.check_in_id = new.check_in_id
       and not exists (select 1 from billing.payment_reversals r where r.payment_id = pp.id);
    if already_on_check_in + new.amount_cents > c.locked_price_cents then
        raise exception 'allocation would exceed price of check-in % (% + % > %)',
            c.check_in_id, already_on_check_in, new.amount_cents, c.locked_price_cents using errcode = 'check_violation';
    end if;

    select coalesce(sum(amount_cents), 0) into already_from_payment
      from billing.payment_allocations where payment_id = new.payment_id;
    if already_from_payment + new.amount_cents > p.amount_cents then
        raise exception 'allocation would exceed payment % (% + % > %)',
            p.id, already_from_payment, new.amount_cents, p.amount_cents using errcode = 'check_violation';
    end if;

    return new;
end;
$$;

create trigger payment_allocations_invariants
    before insert on billing.payment_allocations
    for each row execute function billing.check_allocation();

-- A locked price can never change, and a lock can't be removed.
create function billing.protect_locked_price() returns trigger
language plpgsql as $$
begin
    if old.locked_at is not null and (
           new.locked_price_cents is distinct from old.locked_price_cents
        or new.locked_rate_period_id is distinct from old.locked_rate_period_id
        or new.locked_at is distinct from old.locked_at) then
        raise exception 'price of check-in % is locked', old.check_in_id using errcode = 'restrict_violation';
    end if;
    if old.locked_at is not null and new.classification <> old.classification then
        raise exception 'check-in % has money applied; reclassify through review', old.check_in_id using errcode = 'restrict_violation';
    end if;
    return new;
end;
$$;

create trigger check_in_billing_protect_lock
    before update on billing.check_in_billing
    for each row execute function billing.protect_locked_price();

create trigger check_in_billing_no_delete
    before delete on billing.check_in_billing
    for each row execute function billing.reject_mutation();

-- ---------------------------------------------------------------------------
-- Row-level security on with NO policies: Supabase's anon and authenticated
-- API roles can read nothing here. The Flask server connects with the
-- database owner role, which bypasses RLS.
-- ---------------------------------------------------------------------------
do $$
declare t record;
begin
    for t in select tablename from pg_tables where schemaname = 'billing' loop
        execute format('alter table billing.%I enable row level security', t.tablename);
    end loop;
end $$;
