"""Command-line tasks:  flask --app wsgi <group> <command>

  db status                 show applied / pending migrations
  db migrate                apply pending migrations, each in its own transaction
  institution create ...    create the institution row (once per school)
  staff grant EMAIL ROLE    bootstrap: give a role to someone who has signed in once
"""
import hashlib
from pathlib import Path

import click

from .repo import ROLES

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

SQL_ENSURE_MIGRATIONS_TABLE = """
    create schema if not exists billing;
    create table if not exists billing.schema_migrations (
        filename    text primary key,
        checksum    text not null,
        applied_at  timestamptz not null default now()
    );
"""


def migration_files():
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_migrations(applied, files):
    """Return (pending_files, problems). `applied` maps filename -> checksum.

    A migration that was applied and then edited is a problem, never re-run:
    fix forward with a new numbered file instead.
    """
    problems, pending = [], []
    on_disk = {f.name for f in files}
    for name in applied:
        if name not in on_disk:
            problems.append(f"{name} was applied but is missing from migrations/")
    for f in files:
        if f.name in applied:
            if applied[f.name] != checksum(f):
                problems.append(f"{f.name} changed after it was applied; add a new migration instead")
        else:
            pending.append(f)
    return pending, problems


def register_cli(app):
    db_group = click.Group("db", help="Database migrations.")
    inst_group = click.Group("institution", help="Institution setup.")
    staff_group = click.Group("staff", help="Staff access.")

    def _db():
        return app.extensions["repo"].db

    def _applied():
        _db().execute_script(SQL_ENSURE_MIGRATIONS_TABLE)
        rows = _db().fetch_all("select filename, checksum from billing.schema_migrations")
        return {r["filename"]: r["checksum"] for r in rows}

    @db_group.command("status")
    def db_status():
        applied = _applied()
        pending, problems = plan_migrations(applied, migration_files())
        for name in sorted(applied):
            click.echo(f"  applied  {name}")
        for f in pending:
            click.echo(f"  PENDING  {f.name}")
        for p in problems:
            click.echo(f"  PROBLEM  {p}", err=True)
        if problems:
            raise SystemExit(1)

    @db_group.command("migrate")
    @click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
    def db_migrate(yes):
        applied = _applied()
        pending, problems = plan_migrations(applied, migration_files())
        if problems:
            for p in problems:
                click.echo(f"PROBLEM: {p}", err=True)
            raise SystemExit(1)
        if not pending:
            click.echo("Nothing to apply.")
            return
        click.echo("Will apply: " + ", ".join(f.name for f in pending))
        if not yes:
            click.confirm("Apply to the database in DATABASE_URL?", abort=True)
        for f in pending:
            sql = f.read_text()
            record = (
                "insert into billing.schema_migrations (filename, checksum) "
                f"values ('{f.name}', '{checksum(f)}');"
            )
            _db().execute_script(sql + "\n" + record)
            click.echo(f"applied {f.name}")

    @inst_group.command("create")
    @click.option("--slug", required=True, help="URL-safe id, e.g. sacred-heart")
    @click.option("--name", required=True)
    @click.option("--location", required=True, help="Exact 'Location Name' in the orders export")
    @click.option("--module", default="Order", show_default=True, help="'Module Name' in the orders export")
    @click.option("--anchor", required=True, type=click.DateTime(formats=["%Y-%m-%d"]),
                  help="First day of the first 14-day billing cycle (YYYY-MM-DD)")
    def institution_create(slug, name, location, module, anchor):
        row = app.extensions["repo"].create_institution(slug, name, location, module, anchor.date())
        click.echo(f"created institution {row['slug']} ({row['id']})")

    @staff_group.command("grant")
    @click.argument("email")
    @click.argument("role", type=click.Choice(ROLES))
    def staff_grant(email, role):
        """Give ROLE to EMAIL. They must have signed in once so their account exists here."""
        repo = app.extensions["repo"]
        staff = repo.find_staff_by_email(email)
        if not staff:
            click.echo(f"No staff record for {email}. Have them sign in once first.", err=True)
            raise SystemExit(1)
        result = repo.set_role(staff["user_id"], role, None, "cli")
        click.echo(f"{result['email']} -> {result['role']}")

    app.cli.add_command(db_group)
    app.cli.add_command(inst_group)
    app.cli.add_command(staff_group)
