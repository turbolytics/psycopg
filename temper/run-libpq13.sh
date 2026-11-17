#!/bin/sh
# The python_libpq13 level: the same tests, with the pure-Python
# implementation on Rocky Linux 8 and its system libpq 13.
# Run it through `temper run --level python_libpq13 -- temper/run-libpq13.sh`.
# The database is the one in PSYCOPG_TEST_DSN, reached from the container
# through host.docker.internal.
set -eu
DSN=$(printf '%s' "${PSYCOPG_TEST_DSN:?set PSYCOPG_TEST_DSN}" | sed 's/localhost/host.docker.internal/; s/127\.0\.0\.1/host.docker.internal/')
exec docker run --rm \
  -e TEMPER_LEVEL -e TEMPER_EVIDENCE -e TEMPER_RUN \
  -e PSYCOPG_IMPL=python -e PSYCOPG_TEST_DSN="$DSN" \
  -v "$PWD":"$PWD" -w "$PWD" \
  rockylinux:8 sh -c '
    dnf -q -y install libpq python3.12 python3.12-pip >/dev/null &&
    python3.12 -m venv /tmp/venv &&
    /tmp/venv/bin/pip -q install --config-settings editable_mode=compat -e ./psycopg -e ./psycopg_pool pytest anyio pproxy temper-python &&
    /tmp/venv/bin/pytest -p no:randomly -q tests/test_temper_adapt_contract.py tests/test_temper_failure_contract.py
  '
