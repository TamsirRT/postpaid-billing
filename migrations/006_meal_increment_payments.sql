-- 006_meal_increment_payments.sql
--
-- Parents pay for whole lunches, oldest first: "pay for N lunches" costs the
-- remaining amount on the N oldest unpaid lunches, each at its own price
-- (promo, standard, or locked). So the only amounts a parent can pay are the
-- running totals down the list of unpaid lunches.
--
-- Staff-recorded payments (cash, check, Zoho) are NOT restricted: staff record
-- what was actually received, and any remainder becomes credit.

-- One row per unpaid lunch, oldest first, with the running total a parent
-- would pay to cover it and every older lunch.
create view billing.v_payment_options as
select l.institution_id,
       l.student_id,
       row_number() over w                 as lunches,
       l.check_in_id,
       l.service_date,
       l.rate_label,
       l.open_cents,
       sum(l.open_cents) over w            as amount_cents
  from billing.v_check_in_ledger l
 where l.status in ('open', 'partial')
window w as (partition by l.student_id order by l.service_date, l.check_in_id
             rows between unbounded preceding and current row);

comment on view billing.v_payment_options is
    'Allowed parent payment amounts per child: amount_cents pays exactly the oldest `lunches` unpaid lunches.';

-- How many whole lunches an amount pays for, or NULL if it isn't an allowed amount.
create function billing.meal_increment_lunches(p_student uuid, p_amount_cents integer) returns integer
language sql stable as $$
    select lunches::int from billing.v_payment_options
     where student_id = p_student and amount_cents = p_amount_cents
$$;

-- Hard stop: an online (processor) payment can only be started for an allowed amount.
create function billing.require_meal_increment() returns trigger
language plpgsql as $$
begin
    if billing.meal_increment_lunches(new.student_id, new.amount_cents) is null then
        raise exception 'online payments must cover whole lunches, oldest first; % cents is not an allowed amount for student %',
            new.amount_cents, new.student_id using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

create trigger payment_intents_meal_increment
    before insert on billing.payment_intents
    for each row execute function billing.require_meal_increment();
