# psycopg failure-mode and consistency map

[Temper](https://github.com/turbolytics/temper) map of what psycopg promises
when something fails, and whether every way of doing the same thing gives the
same result. `MATRIX.md` at the repository root is the result.

- `lattices/`: the guarantees.
  - `batch.contract`: when one row of a batch fails, no row is committed.
  - `prepare.contract`: a prepared statement keeps working after DDL.
  - `send.contract` and `load.contract`: every path stores, and every format
    loads, the same value.
  - `pool.contract`: every pool class hands out a usable connection.
- `subjects/`: one file per way to do the same thing.
- `../tests/test_temper_failure_contract.py` and
  `../tests/test_temper_adapt_contract.py`: the tests. Each one states what it
  proves with `@pytest.mark.covers`.

Levels are the ways psycopg runs. Each cell of the matrix shows them in order:
the pure-Python implementation, the C implementation, and the pure-Python
implementation on libpq 13 (Rocky Linux 8, in Docker).

Run it (needs Docker and `pg_config` on `PATH` to build psycopg_c):

```sh
docker run -d --name temper-pg -e POSTGRES_PASSWORD=temper -p 54317:5432 postgres:17
uv venv
uv pip install --config-settings editable_mode=compat -e "./psycopg[test]" -e ./psycopg_pool temper-python
uv pip install ./psycopg_c
export PSYCOPG_TEST_DSN="host=localhost port=54317 user=postgres password=temper dbname=postgres"
brew install turbolytics/tap/temper   # or: go install github.com/turbolytics/temper/cmd/temper@v0.1.2
temper clean
temper run --level python -- env PSYCOPG_IMPL=python .venv/bin/pytest -p no:randomly tests/test_temper_adapt_contract.py tests/test_temper_failure_contract.py
temper run --level c -- env PSYCOPG_IMPL=c .venv/bin/pytest -p no:randomly tests/test_temper_adapt_contract.py tests/test_temper_failure_contract.py
temper run --level python_libpq13 -- temper/run-libpq13.sh
temper matrix --all-levels
```

`temper matrix` exits 1 while any cell is failing.
