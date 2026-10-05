-- 011_child_email_exclusions.sql
--
-- Staff can leave a child out of every email (e.g. staff children, families
-- the school pays for). The child's lunches are still tracked and shown on the
-- parent page; they just never appear in a statement or trigger a receipt,
-- even when the same parent has other children who do get emails.

create table billing.email_exclusions (
    institution_id  uuid not null references billing.institutions(id),
    student_id      uuid not null,                 -- public.students.id (no FK, see 001)
    reason          text not null check (length(btrim(reason)) between 1 and 300),
    excluded_by     uuid references billing.staff_roles(user_id),
    excluded_at     timestamptz not null default now(),
    primary key (institution_id, student_id)
);
alter table billing.email_exclusions enable row level security;

-- Statement sending reads only from this view: excluded children drop out.
create or replace view billing.v_statement_recipients as
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
   and g.receives_notices
   and not exists (select 1 from billing.email_exclusions x
                    where x.institution_id = b.institution_id and x.student_id = b.student_id);

-- Excluded children are left out on purpose, so they aren't "missing contacts".
create or replace view billing.v_billed_without_contact as
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
   and coalesce(cs.reachable_count, 0) = 0
   and not exists (select 1 from billing.email_exclusions x
                    where x.institution_id = b.institution_id and x.student_id = b.student_id);
