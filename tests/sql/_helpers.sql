-- Assertion helpers shared by the SQL test files.
set client_min_messages = notice;

create or replace function pg_temp.expect_error(stmt text, expected_state text, label text)
returns void language plpgsql as $$
begin
    begin
        execute stmt;
    exception when others then
        if sqlstate = expected_state then
            raise notice 'ok   %', label;
            return;
        end if;
        raise exception 'FAIL % : expected SQLSTATE % but got % (%)', label, expected_state, sqlstate, sqlerrm;
    end;
    raise exception 'FAIL % : statement succeeded but should have failed', label;
end $$;

create or replace function pg_temp.expect_eq(actual anyelement, expected anyelement, label text)
returns void language plpgsql as $$
begin
    if actual is distinct from expected then
        raise exception 'FAIL % : expected % got %', label, expected, actual;
    end if;
    raise notice 'ok   %', label;
end $$;
