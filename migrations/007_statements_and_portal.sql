-- 007_statements_and_portal.sql
--
--   * Every email is recorded in full: who it was meant for, where it actually
--     went, which mode sent it, and the exact subject and body. In test mode the
--     intended parent and the real delivery address differ, and both are kept.
--   * Portal links are derived from a server secret + the guardian id + a
--     version number, so the same link keeps working in every email; bumping
--     the version (rotating) kills every old link at once. Only a hash of the
--     link is stored.

alter table billing.notifications
    add column mode          text not null default 'outbox' check (mode in ('outbox', 'test', 'live')),
    add column intended_email text,
    add column delivered_to   text,
    add column subject        text,
    add column body_html      text,
    add column body_text      text,
    add column error          text,
    add column sent_at        timestamptz;

create index notifications_guardian_kind_idx on billing.notifications (guardian_id, kind, mode, created_at);

alter table billing.guardians
    add column token_version integer not null default 1 check (token_version >= 1);
