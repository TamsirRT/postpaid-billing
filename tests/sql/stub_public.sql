-- Local stand-ins for the check-in app's tables, matching the Sep 21, 2026
-- exports column for column. Test databases only; production already has these.
create table if not exists public.students (
    id           uuid primary key default gen_random_uuid(),
    last_name    text,
    first_name   text,
    grade_level  text,
    home_room    text,
    created_at   timestamptz default now(),
    updated_at   timestamptz default now(),
    pin          text,
    email        text,
    phone        text
);
create table if not exists public.check_ins (
    id               uuid primary key default gen_random_uuid(),
    student_id       uuid,
    check_in_date    date,
    getting_lunch    boolean,
    check_in_time    timestamptz,
    notes            text,
    bill_separately  boolean
);
