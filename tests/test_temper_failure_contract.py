"""The batch.contract, prepare.contract and pool.contract lattices (temper/lattices).

Each test states what it proves with @pytest.mark.covers (temper-python).
Without `temper run`, the marks do nothing.

These are failure modes: what is committed when a batch fails, whether a
prepared statement survives a change of schema, and what a pool hands out
after a connection breaks. Each subject is one way to do the same thing,
and every way must give the same result.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

import psycopg

psycopg_pool = pytest.importorskip("psycopg_pool")

ROWS = [(1,), (2,), (2,), (3,)]  # the third row breaks the primary key


@pytest.fixture(autouse=True)
def _level_matches_runtime():
    want = os.environ.get("PSYCOPG_IMPL")
    if want and want != psycopg.pq.__impl__:
        pytest.fail(
            f"PSYCOPG_IMPL={want} but psycopg loaded the {psycopg.pq.__impl__} implementation"
        )
    if (
        os.environ.get("TEMPER_LEVEL") == "python_libpq13"
        and psycopg.pq.version() >= 140000
    ):
        pytest.fail(f"level python_libpq13 needs libpq 13, got {psycopg.pq.version()}")


def params(lattice, member, subjects, prefix, feature=False):
    return [
        pytest.param(
            s,
            id=s,
            marks=[
                pytest.mark.covers(f"{lattice}/{member}", subject=f"{prefix}.{s}"),
                *([pytest.mark.covers(f"{prefix}.{s}")] if feature else []),
            ],
        )
        for s in subjects
    ]


def admin(dsn, *stmts):
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as c:
        for s in stmts:
            c.execute(s)


def committed(dsn):
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as c:
        return [r[0] for r in c.execute("select v from temper_batch order by v")]


# ---------------------------------------------------------------- batches

BATCH = [
    "executemany",
    "executemany_returning",
    "async_executemany",
    "client_cursor_executemany",
]


def run_batch(dsn, path, autocommit):
    query = "insert into temper_batch (v) values (%s)"
    if path == "async_executemany":

        async def go():
            conn = await psycopg.AsyncConnection.connect(dsn, autocommit=autocommit)
            try:
                await conn.cursor().executemany(query, ROWS)
            finally:
                if not autocommit:
                    await conn.rollback()
                await conn.close()

        return asyncio.run(go())

    factory = (
        psycopg.ClientCursor if path == "client_cursor_executemany" else psycopg.Cursor
    )
    conn = psycopg.connect(dsn, autocommit=autocommit, cursor_factory=factory)
    try:
        if path == "executemany_returning":
            conn.cursor().executemany(query + " returning v", ROWS, returning=True)
        else:
            conn.cursor().executemany(query, ROWS)
    finally:
        if not autocommit:
            conn.rollback()
        conn.close()


@pytest.mark.parametrize(
    "path",
    params(
        "batch.contract", "all_or_nothing_in_autocommit", BATCH, "batch", feature=True
    ),
)
def test_batch_failure_commits_nothing_in_autocommit(dsn, path):
    # executemany runs in pipeline mode, which wraps the batch in one implicit
    # transaction even in autocommit (docs/advanced/pipeline.rst).
    admin(
        dsn,
        "drop table if exists temper_batch",
        "create table temper_batch (v int primary key)",
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        run_batch(dsn, path, autocommit=True)
    assert committed(dsn) == []


@pytest.mark.parametrize(
    "path", params("batch.contract", "all_or_nothing_in_transaction", BATCH, "batch")
)
def test_batch_failure_commits_nothing_in_transaction(dsn, path):
    admin(
        dsn,
        "drop table if exists temper_batch",
        "create table temper_batch (v int primary key)",
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        run_batch(dsn, path, autocommit=False)
    assert committed(dsn) == []


# ------------------------------------------------- prepared statements vs DDL

DDL = ["drop_table", "discard_all", "deallocate_all"]


def change_schema(conn, ddl, new_type):
    """Run the DDL statement under test, leaving column v of type new_type."""
    if ddl == "drop_table":
        conn.execute("drop table if exists temper_prep")
        conn.execute(f"create table temper_prep (v {new_type})")
    elif ddl == "discard_all":
        conn.execute("discard all")
    elif ddl == "deallocate_all":
        conn.execute("deallocate all")


def prepared_insert_survives(dsn, ddl, runs_before):
    """Prepare an insert, run the DDL statement, then insert again.

    runs_before is how many times the same DDL statement already ran before
    anything was prepared, as a test fixture or a migration would.
    """
    admin(dsn, "drop table if exists temper_prep", "create table temper_prep (v text)")
    conn = psycopg.connect(dsn, autocommit=True)  # default prepare_threshold
    try:
        for _ in range(runs_before):
            change_schema(conn, ddl, "text")
        for _ in range(6):  # past the threshold: psycopg prepares the insert
            conn.execute("insert into temper_prep (v) values (%s)", ["x"])
        new_type = "int" if ddl == "drop_table" else "text"
        change_schema(conn, ddl, new_type)
        conn.execute(
            "insert into temper_prep (v) values (%s)",
            ["42" if new_type == "int" else "x"],
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "ddl", params("prepare.contract", "survives_first_run", DDL, "ddl", feature=True)
)
def test_prepared_statements_survive_ddl_first_run(dsn, ddl):
    prepared_insert_survives(dsn, ddl, runs_before=0)


@pytest.mark.parametrize(
    "ddl", params("prepare.contract", "survives_repeated_run", DDL, "ddl")
)
def test_prepared_statements_survive_ddl_repeated_run(dsn, ddl):
    prepared_insert_survives(dsn, ddl, runs_before=1)


# ------------------------------------------------------------------- pools

POOLS = {
    "connection_pool": "ConnectionPool",
    "null_connection_pool": "NullConnectionPool",
    "async_connection_pool": "AsyncConnectionPool",
    "async_null_connection_pool": "AsyncNullConnectionPool",
}


def kill(dsn, pid):
    admin(dsn, f"select pg_terminate_backend({int(pid)})")
    time.sleep(0.2)


def pool_kwargs(name, **extra):
    null = "Null" in name
    return dict(
        min_size=0 if null else 1,
        max_size=1,
        open=not name.startswith("Async"),
        timeout=5,
        **extra,
    )


def pool_class(subject):
    return POOLS[subject]


def run_pool(dsn, name, body):
    """Run body(pool, getconn, putconn, execute) with sync or async pools alike."""
    cls = getattr(psycopg_pool, name)
    if not name.startswith("Async"):
        with cls(dsn, **pool_kwargs(name, check=cls.check_connection)) as pool:
            return body(
                pool, pool.getconn, pool.putconn, lambda c, q: c.execute(q).fetchall()
            )

    async def go():
        async with cls(dsn, **pool_kwargs(name, check=cls.check_connection)) as pool:
            loop = asyncio.get_running_loop()

            def getconn():
                return asyncio.run_coroutine_threadsafe(pool.getconn(), loop).result()

            def putconn(c):
                return asyncio.run_coroutine_threadsafe(pool.putconn(c), loop).result()

            def execute(c, q):
                async def ex():
                    cur = await c.execute(q)
                    return await cur.fetchall()

                return asyncio.run_coroutine_threadsafe(ex(), loop).result()

            return await asyncio.to_thread(body, pool, getconn, putconn, execute)

    return asyncio.run(go())


@pytest.mark.parametrize(
    "name",
    params(
        "pool.contract",
        "returned_connection_is_clean",
        list(POOLS),
        "pool",
        feature=True,
    ),
)
def test_pool_returned_connection_is_clean(dsn, name):
    name = pool_class(name)
    admin(dsn, "drop table if exists temper_pool", "create table temper_pool (v int)")

    def body(pool, getconn, putconn, execute):
        conn = getconn()
        execute(
            conn, "insert into temper_pool values (1) returning v"
        )  # left in a transaction
        putconn(conn)
        conn = getconn()
        try:
            execute(conn, "select 1/0")
        except psycopg.Error:
            pass
        putconn(conn)
        conn = getconn()
        status = conn.info.transaction_status
        assert execute(conn, "select 1") == [(1,)]
        putconn(conn)
        return status

    assert run_pool(dsn, name, body) == psycopg.pq.TransactionStatus.IDLE
    with psycopg.connect(dsn, autocommit=True) as c:
        assert c.execute("select count(*) from temper_pool").fetchone()[0] == 0


@pytest.mark.parametrize(
    "name", params("pool.contract", "replaces_killed_connection", list(POOLS), "pool")
)
def test_pool_replaces_killed_connection(dsn, name):
    name = pool_class(name)

    def body(pool, getconn, putconn, execute):
        conn = getconn()
        pid = conn.info.backend_pid
        putconn(conn)
        kill(dsn, pid)  # killed while idle in the pool
        conn = getconn()
        assert execute(conn, "select 1") == [(1,)]
        kill(dsn, conn.info.backend_pid)  # killed while checked out
        with pytest.raises(psycopg.OperationalError):
            execute(conn, "select 1")
        putconn(conn)
        conn = getconn()
        assert execute(conn, "select 1") == [(1,)]
        putconn(conn)

    run_pool(dsn, name, body)


@pytest.mark.parametrize(
    "name", params("pool.contract", "times_out_when_exhausted", list(POOLS), "pool")
)
def test_pool_times_out_when_exhausted(dsn, name):
    name = pool_class(name)
    cls = getattr(psycopg_pool, name)
    if name.startswith("Async"):

        async def go():
            async with cls(dsn, **pool_kwargs(name)) as pool:
                held = await pool.getconn()
                t0 = time.monotonic()
                with pytest.raises(psycopg_pool.PoolTimeout):
                    await pool.getconn(timeout=1.0)
                elapsed = time.monotonic() - t0
                await pool.putconn(held)
                return elapsed

        elapsed = asyncio.run(go())
    else:
        with cls(dsn, **pool_kwargs(name)) as pool:
            held = pool.getconn()
            t0 = time.monotonic()
            with pytest.raises(psycopg_pool.PoolTimeout):
                pool.getconn(timeout=1.0)
            elapsed = time.monotonic() - t0
            pool.putconn(held)
    assert 0.9 <= elapsed < 3.0
