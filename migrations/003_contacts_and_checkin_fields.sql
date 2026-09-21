-- 003_contacts_and_checkin_fields.sql
-- Changes driven by the real students and check_ins exports (Sep 21, 2026):
--
--   * public.students has email and phone columns, but values can be junk
--     (the test export has 'redacted' everywhere). Contacts are copied into
--     billing.guardians only when valid, and staff edit them there.
--   * A guardian may have only a phone. Such a guardian can't receive email,
--     so the child is flagged "billed without contact" instead of breaking a send.
--   * check_ins.getting_lunch = false means no lunch was taken: never billed.
--   * check_ins goes back to Aug 2025. Nothing before an institution's
--     billing_starts_on date is ever classified, so history isn't billed.

-- ---------------------------------------------------------------- guardians
alter table billing.guardians alter column email drop not null;
alter table billing.guardians alter column name  drop not null;
alter table billing.guardians add column phone      text;
alter table billing.guardians add column source     text not null default 'manual' check (source in ('manual', 'roster'));
alter table billing.guardians add column updated_at timestamptz not null default now();

alter table billing.guardians add constraint guardians_has_contact
    check (email is not null or phone is not null);
alter table billing.guardians add constraint guardians_email_trimmed
    check (email is null or email = btrim(email));
alter table billing.guardians add constraint guardians_phone_valid
    check (phone is null or length(regexp_replace(phone, '\D', '', 'g')) between 10 and 15);
alter table billing.guardians add constraint guardians_name_not_blank
    check (name is null or btrim(name) <> '');

drop index billing.guardians_institution_email_uq;
create unique index guardians_institution_email_uq
    on billing.guardians (institution_id, lower(email)) where email is not null;

-- ---------------------------------------------------------------- institutions
alter table billing.institutions add column billing_starts_on date;
comment on column billing.institutions.billing_starts_on is
    'Check-ins before this date are never classified or billed. NULL = classification refuses to run.';

-- ---------------------------------------------------------------- check-ins
alter table billing.check_in_billing drop constraint check_in_billing_classification_check;
alter table billing.check_in_billing add constraint check_in_billing_classification_check
    check (classification in ('post_paid', 'pre_ordered', 'duplicate', 'no_lunch'));
alter table billing.check_in_billing add column classification_note text;
comment on column billing.check_in_billing.classification_note is
    'Why it was classified this way when not obvious, e.g. check-in marked bill_separately.';

-- ---------------------------------------------------------------- contact views
-- Who can actually receive an email.
create view billing.v_student_contact_status as
select gs.institution_id,
       gs.student_id,
       count(*)                                                              as guardian_count,
       count(*) filter (where g.email is not null)                            as email_count,
       count(*) filter (where g.email is not null and g.receives_notices)     as reachable_count
  from billing.guardian_students gs
  join billing.guardians g on g.id = gs.guardian_id
 group by gs.institution_id, gs.student_id;

-- Children who owe money but no one can be emailed about it. This is the list
-- to send to the school. Statement sending never sees these children.
create view billing.v_billed_without_contact as
select b.institution_id,
       b.student_id,
       b.balance_due_cents,
       b.unpaid_count,
       b.oldest_unpaid_date,
       coalesce(cs.guardian_count, 0) as guardian_count,
       case
           when coalesce(cs.guardian_count, 0) = 0 then 'no_contact'
           when coalesce(cs.email_count, 0)    = 0 then 'no_email'
           else 'notices_off'
       end as reason
  from billing.v_student_balances b
  left join billing.v_student_contact_status cs using (institution_id, student_id)
 where b.balance_due_cents > 0
   and coalesce(cs.reachable_count, 0) = 0;

-- The ONLY source statement sending may read from: one row per
-- (guardian with a usable email, child they're linked to who owes money).
create view billing.v_statement_recipients as
select b.institution_id,
       g.id    as guardian_id,
       g.name  as guardian_name,
       g.email,
       b.student_id,
       b.balance_due_cents
  from billing.v_student_balances b
  join billing.guardian_students gs on gs.student_id = b.student_id and gs.institution_id = b.institution_id
  join billing.guardians g          on g.id = gs.guardian_id
 where b.balance_due_cents > 0
   and g.email is not null
   and g.receives_notices;

-- Belt and braces: no notification row can ever be created for a guardian
-- without an email, whatever the calling code does.
create function billing.require_guardian_email() returns trigger
language plpgsql as $$
begin
    if not exists (select 1 from billing.guardians where id = new.guardian_id and email is not null) then
        raise exception 'guardian % has no email; skip them and use billing.v_billed_without_contact', new.guardian_id
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

create trigger notifications_require_email
    before insert on billing.notifications
    for each row execute function billing.require_guardian_email();
