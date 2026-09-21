"""Thin Postgres access layer over psycopg 3.

The rest of the app talks to the database only through `Database`
(fetch_one / fetch_all / execute), so tests can substitute another
implementation with the same three methods.

Queries use psycopg's named placeholders: %(name)s.
"""


class Database:
    def __init__(self, dsn, min_size=1, max_size=5):
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool = None

    def _get_pool(self):
        if self._pool is None:
            # Imported lazily so the app (and its unit tests) load without the driver.
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                self._dsn,
                min_size=self._min,
                max_size=self._max,
                kwargs={"row_factory": dict_row, "autocommit": False},
                open=True,
            )
        return self._pool

    def fetch_all(self, sql, params=None):
        with self._get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or {})
                rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows

    def fetch_one(self, sql, params=None):
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=None):
        with self._get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or {})
            conn.commit()

    def execute_script(self, sql):
        """Run a multi-statement script (a migration) in one transaction."""
        with self._get_pool().connection() as conn:
            with conn.transaction():
                conn.execute(sql)

    def close(self):
        if self._pool is not None:
            self._pool.close()
