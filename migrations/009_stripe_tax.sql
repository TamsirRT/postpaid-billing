-- 009_stripe_tax.sql
--
-- Stripe Tax can add sales tax at checkout (online payments only). The tax is
-- money collected for the state, not payment for lunches: the payment's
-- amount_cents stays the lunch amount and is what gets applied to lunches;
-- the tax is kept beside it for receipts and reporting.

alter table billing.payments
    add column tax_cents integer not null default 0 check (tax_cents >= 0);
alter table billing.payment_intents
    add column tax_cents integer not null default 0 check (tax_cents >= 0);

-- Same as 008, plus the tax. p_amount_cents is the lunch amount (before tax).
drop function billing.record_stripe_payment(uuid, text, text, integer);
create function billing.record_stripe_payment(
    p_intent uuid, p_stripe_payment_intent text, p_method text, p_amount_cents integer, p_tax_cents integer default 0
) returns table (payment_id uuid, created boolean, applied_cents bigint)
language plpgsql as $$
declare
    it   billing.payment_intents%rowtype;
    pid  uuid;
begin
    select * into it from billing.payment_intents where id = p_intent for update;
    if not found then
        raise exception 'payment intent % not found', p_intent using errcode = 'no_data_found';
    end if;
    if p_method not in ('card', 'ach') then
        raise exception 'Stripe payments are card or ach (got %)', p_method using errcode = 'check_violation';
    end if;
    if p_amount_cents <> it.amount_cents then
        raise exception 'Stripe amount % (before tax) does not match intent amount %', p_amount_cents, it.amount_cents
            using errcode = 'check_violation';
    end if;
    if coalesce(p_tax_cents, 0) < 0 then
        raise exception 'tax cannot be negative' using errcode = 'check_violation';
    end if;

    select p.id into pid from billing.payments p where p.processor_ref = p_stripe_payment_intent;
    if pid is not null then
        return query select pid, false, 0::bigint;
        return;
    end if;

    insert into billing.payments (institution_id, student_id, paid_by_guardian_id, amount_cents, tax_cents, method,
                                  processor_ref, note)
    values (it.institution_id, it.student_id, it.guardian_id, it.amount_cents, coalesce(p_tax_cents, 0), p_method,
            p_stripe_payment_intent, 'Online payment for ' || coalesce(it.lunches::text, '?') || ' lunch(es)')
    returning id into pid;

    update billing.payment_intents
       set status = 'succeeded', payment_id = pid, method = p_method, tax_cents = coalesce(p_tax_cents, 0),
           stripe_payment_intent = p_stripe_payment_intent, failure_reason = null, updated_at = now()
     where id = p_intent;

    return query select pid, true, billing.apply_credit(it.student_id);
end;
$$;
