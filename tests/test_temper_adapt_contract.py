"""The send.contract and load.contract lattices (temper/lattices).

Each test states what it proves with @pytest.mark.covers (temper-python).
Without `temper run`, the marks do nothing.

Every path that sends a value must store the same value, and every format
that loads one must return the same value or raise. The reference is
Postgres itself: a literal written in SQL, built from hex where quoting
could get in the way, and never another psycopg path.

The matrix runs this file twice, once per implementation, with
PSYCOPG_IMPL=python and PSYCOPG_IMPL=c. Those are its two levels.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

import psycopg
from psycopg import sql

UTC = dt.UTC

SEND_PATHS = [
    "bind_text",
    "bind_binary",
    "client_cursor",
    "sql_literal",
    "copy_text",
    "copy_binary",
]
LOAD_FORMATS = ["text", "binary"]


@pytest.fixture(autouse=True)
def _level_matches_impl():
    want = os.environ.get("PSYCOPG_IMPL")
    if want and want != psycopg.pq.__impl__:
        pytest.fail(
            f"PSYCOPG_IMPL={want} but psycopg loaded the {psycopg.pq.__impl__} implementation"
        )


@pytest.fixture
def aconn(dsn):
    # Automatic prepared statements are off here: these tests recreate one
    # table many times, and test_temper_failure_contract.py covers preparing.
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn:
        yield conn


def params(
    lattice: str, member: str, subjects: list[str], prefix: str, feature: bool = False
):
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


def text_lit(s: str) -> str:
    return f"convert_from(decode('{s.encode().hex()}', 'hex'), 'UTF8')"


def bytes_lit(b: bytes) -> str:
    return f"decode('{b.hex()}', 'hex')"


def send(conn, path: str, value, typ: str) -> str:
    """Store value in a column of type typ through path; return the server's text of it."""
    conn.execute("drop table if exists temper_send")
    conn.execute(f"create table temper_send (v {typ})")
    ins = "insert into temper_send (v) values "
    if path == "bind_text":
        conn.execute(ins + "(%t)", [value])
    elif path == "bind_binary":
        conn.execute(ins + "(%b)", [value])
    elif path == "client_cursor":
        psycopg.ClientCursor(conn).execute(ins + "(%s)", [value])
    elif path == "sql_literal":
        conn.execute(sql.SQL(ins + "({})").format(sql.Literal(value)))
    elif path in ("copy_text", "copy_binary"):
        fmt = " (format binary)" if path == "copy_binary" else ""
        with conn.cursor().copy(f"copy temper_send (v) from stdin{fmt}") as cp:
            cp.set_types([typ])
            cp.write_row([value])
    if typ == "interval":
        # Postgres keeps days and time apart: compare the justified value.
        return conn.execute(
            "select justify_interval(v)::text from temper_send"
        ).fetchone()[0]
    return conn.execute("select v::text from temper_send").fetchone()[0]


def check_send(conn, path, cases):
    wrong = []
    for value, typ, literal in cases:
        cast = (
            f"justify_interval(({literal})::interval)"
            if typ == "interval"
            else f"({literal})::{typ}"
        )
        want = conn.execute(f"select {cast}::text").fetchone()[0]
        try:
            got = send(conn, path, value, typ)
        except psycopg.Error as e:
            got = f"{type(e).__name__}: {str(e).splitlines()[0]}"
        if got != want:
            wrong.append(f"{value!r} as {typ}: stored {got!r}, want {want!r}")
    assert wrong == []


NUMBERS = [
    (-0.0, "float8", "'-0'"),
    (math.nan, "float8", "'NaN'"),
    (math.inf, "float8", "'Infinity'"),
    (-math.inf, "float8", "'-Infinity'"),
    (5e-324, "float8", "'5e-324'"),
    (0.1, "float8", "'0.1'"),
    (1.5e300, "float8", "'1.5e300'"),
    (-(2**63), "int8", "'-9223372036854775808'"),
    (10**40, "numeric", "1e40"),
    (Decimal("-0.00"), "numeric", "'-0.00'"),
    (Decimal("NaN"), "numeric", "'NaN'"),
    (Decimal("-NaN"), "numeric", "'NaN'"),
    (Decimal("sNaN"), "numeric", "'NaN'"),
    (Decimal("Infinity"), "numeric", "'Infinity'"),
    (Decimal("-Infinity"), "numeric", "'-Infinity'"),
    (Decimal("1E+1000"), "numeric", "'1E+1000'"),
    (Decimal("123.4500"), "numeric", "'123.4500'"),
    (Decimal("0E-20"), "numeric", "'0E-20'"),
]

STRINGS = [
    "a\\b",
    "it's",
    'say "hi"',
    "a\nb",
    "a\tb",
    "a\rb",
    "\\N",
    "NULL",
    "\U0001f600",
    "abc\\",
    "\\x41",
    "",
    "\x01\x1f\x7f",
]
BYTES = [b"\x00\xff", b"\\", b"'", b"", bytes(range(256))]


def text_and_bytes_cases():
    cases = [(s, "text", text_lit(s)) for s in STRINGS]
    cases += [(b, "bytea", bytes_lit(b)) for b in BYTES]
    strs = ["a,b", 'c"d', "", "NULL", None, "a\\b", "{x}", " sp "]
    arr = ", ".join("NULL" if s is None else text_lit(s) for s in strs)
    cases.append((strs, "text[]", f"array[{arr}]::text[]"))
    ba = b"\x00\\"
    cases.append(([ba, None], "bytea[]", "array[" + bytes_lit(ba) + ", NULL]::bytea[]"))
    return cases


DATES_TIMES = [
    (dt.date.min, "date", "'0001-01-01'"),
    (dt.date.max, "date", "'9999-12-31'"),
    (dt.datetime.min, "timestamp", "'0001-01-01 00:00:00'"),
    (dt.datetime.max, "timestamp", "'9999-12-31 23:59:59.999999'"),
    (dt.datetime(2020, 1, 1, 0, 0, 0, 1), "timestamp", "'2020-01-01 00:00:00.000001'"),
    (
        dt.datetime.max.replace(tzinfo=UTC),
        "timestamptz",
        "'9999-12-31 23:59:59.999999+00'",
    ),
    (
        dt.datetime(
            2020, 1, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))
        ),
        "timestamptz",
        "'2020-01-01 12:00:00+05:30'",
    ),
    (
        dt.datetime(2020, 1, 1, 12, tzinfo=dt.timezone(-dt.timedelta(seconds=30))),
        "timestamptz",
        "'2020-01-01 12:00:00-00:00:30'",
    ),
    (dt.time.max, "time", "'23:59:59.999999'"),
    (
        dt.time(12, tzinfo=dt.timezone(-dt.timedelta(seconds=30))),
        "timetz",
        "'12:00:00-00:00:30'",
    ),
    (
        dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=15, minutes=59))),
        "timetz",
        "'12:00:00+15:59'",
    ),
    (dt.timedelta.max, "interval", "'999999999 days 23:59:59.999999'"),
    (dt.timedelta.min, "interval", "'-999999999 days'"),
    (dt.timedelta(microseconds=-1), "interval", "'-00:00:00.000001'"),
    (dt.timedelta(days=-1, microseconds=1), "interval", "'-1 days +00:00:00.000001'"),
    (dt.timedelta(seconds=1.5), "interval", "'00:00:01.5'"),
]

CONTAINERS = [
    ([[1, 2], [3, 4]], "int8[]", "'{{1,2},{3,4}}'"),
    ([-0.0, math.nan, math.inf], "float8[]", "'{-0,NaN,Infinity}'"),
    (
        [Decimal("NaN"), Decimal("-0.00"), Decimal("1E+3")],
        "numeric[]",
        "'{NaN,-0.00,1E+3}'",
    ),
    ([dt.date.max, None], "date[]", "'{9999-12-31,NULL}'"),
    ([True, None], "bool[]", "'{t,NULL}'"),
    (
        Jsonb({"a": [1, None, True, 'x\\y"z\n']}),
        "jsonb",
        text_lit(json.dumps({"a": [1, None, True, 'x\\y"z\n']})),
    ),
    (Range(1, 1, "[]"), "int4range", "'[1,1]'"),
    (Range(Decimal("1.5"), Decimal("NaN")), "numrange", "'[1.5,NaN)'"),
    (Range(dt.date(2020, 1, 1), None), "daterange", "'[2020-01-01,)'"),
    (
        Range(dt.datetime(2020, 1, 1, tzinfo=UTC), dt.datetime(2021, 1, 1, tzinfo=UTC)),
        "tstzrange",
        "'[2020-01-01 00:00:00+00,2021-01-01 00:00:00+00)'",
    ),
]


@pytest.mark.parametrize(
    "path",
    params("send.contract", "numbers_keep_value", SEND_PATHS, "send", feature=True),
)
def test_send_numbers(aconn, path):
    check_send(aconn, path, NUMBERS)


@pytest.mark.parametrize(
    "path", params("send.contract", "text_and_bytes_keep_value", SEND_PATHS, "send")
)
def test_send_text_and_bytes(aconn, path):
    check_send(aconn, path, text_and_bytes_cases())


@pytest.mark.parametrize(
    "path", params("send.contract", "bytes_keep_value_scs_off", SEND_PATHS, "send")
)
def test_send_bytes_with_standard_conforming_strings_off(aconn, path):
    # Postgres 18 and earlier can turn standard_conforming_strings off.
    aconn.execute("set standard_conforming_strings = off")
    cases = [(b, "bytea", bytes_lit(b)) for b in BYTES]
    ba = b"\x00\xff"
    cases.append(([ba, None], "bytea[]", "array[" + bytes_lit(ba) + ", NULL]::bytea[]"))
    check_send(aconn, path, cases)


@pytest.mark.parametrize(
    "path", params("send.contract", "dates_times_keep_value", SEND_PATHS, "send")
)
def test_send_dates_times(aconn, path):
    check_send(aconn, path, DATES_TIMES)


@pytest.mark.parametrize(
    "path", params("send.contract", "containers_keep_value", SEND_PATHS, "send")
)
def test_send_containers(aconn, path):
    check_send(aconn, path, CONTAINERS)


ODD = 'a\\b "q" \n NULL'


def load(conn, fmt: str, literal: str):
    return (
        conn.cursor(binary=(fmt == "binary")).execute(f"select {literal}").fetchone()[0]
    )


def check_load(conn, fmt, cases):
    wrong = []
    for literal, want in cases:
        try:
            got = load(conn, fmt, literal)
        except psycopg.DataError:
            got = psycopg.DataError
        ok = got == want or repr(got) == repr(want)
        ok = ok or (
            isinstance(want, float)
            and isinstance(got, float)
            and math.isnan(want)
            and math.isnan(got)
        )
        if (
            isinstance(want, float)
            and isinstance(got, float)
            and want == 0
            and got == 0
        ):
            ok = math.copysign(1, want) == math.copysign(1, got)
        if not ok:
            wrong.append(f"{literal}: loaded {got!r}, want {want!r}")
    assert wrong == []


@pytest.mark.parametrize(
    "fmt",
    params("load.contract", "numbers_match_server", LOAD_FORMATS, "load", feature=True),
)
def test_load_numbers(aconn, fmt):
    check_load(
        aconn,
        fmt,
        [
            ("'-0'::float8", -0.0),
            ("'NaN'::float8", math.nan),
            ("'Infinity'::float8", math.inf),
            ("'-9223372036854775808'::int8", -(2**63)),
            ("'123.4500'::numeric", Decimal("123.4500")),
            ("'-Infinity'::numeric", Decimal("-Infinity")),
            ("'1e-1000'::numeric", Decimal("1E-1000")),
        ],
    )


@pytest.mark.parametrize(
    "fmt",
    params("load.contract", "intervals_match_server_or_raise", LOAD_FORMATS, "load"),
)
def test_load_intervals(aconn, fmt):
    # An interval Python cannot hold must raise, never come back as another value.
    check_load(
        aconn,
        fmt,
        [
            ("'1 day -1 microsecond'::interval", dt.timedelta(days=1, microseconds=-1)),
            ("'-1 mon 2 days'::interval", dt.timedelta(days=-28)),
            ("'999999999 days'::interval", dt.timedelta(days=999999999)),
            ("'1000000000 days'::interval", psycopg.DataError),
            ("'6000000 years'::interval", psycopg.DataError),
            ("'12000000 years'::interval", psycopg.DataError),
            ("'-12000000 years'::interval", psycopg.DataError),
            ("'178000000 years'::interval", psycopg.DataError),
            ("'2000000000 days 6000000 years'::interval", psycopg.DataError),
        ],
    )


@pytest.mark.parametrize(
    "fmt",
    params("load.contract", "dates_times_match_server_or_raise", LOAD_FORMATS, "load"),
)
def test_load_dates_times(aconn, fmt):
    aconn.execute("set timezone = 'America/New_York'")
    second = load(aconn, fmt, "'2020-11-01 06:30:00+00'::timestamptz")
    assert second.fold == 1 and second.utcoffset() == dt.timedelta(hours=-5)
    check_load(
        aconn,
        fmt,
        [
            ("'infinity'::date", psycopg.DataError),
            ("'10000-01-01'::date", psycopg.DataError),
            ("'0001-01-01'::date", dt.date.min),
            ("'9999-12-31 23:59:59.999999'::timestamp", dt.datetime.max),
            ("'24:00:00'::time", psycopg.DataError),
            (
                "'12:00:00-15:59:59'::timetz",
                dt.time(
                    12,
                    tzinfo=dt.timezone(-dt.timedelta(hours=15, minutes=59, seconds=59)),
                ),
            ),
        ],
    )


@pytest.mark.parametrize(
    "fmt", params("load.contract", "text_and_arrays_match_server", LOAD_FORMATS, "load")
)
def test_load_text_and_arrays(aconn, fmt):
    check_load(
        aconn,
        fmt,
        [
            (text_lit(ODD), ODD),
            ('\'{"a,b","c\\"d",NULL,"NULL"}\'::text[]', ["a,b", 'c"d', None, "NULL"]),
            ("'{{1,2},{3,4}}'::int4[]", [[1, 2], [3, 4]]),
            ("'{}'::int4[]", []),
            (bytes_lit(bytes(range(256))), bytes(range(256))),
            ("'{-0,NaN}'::numeric[]", [Decimal(0), Decimal("NaN")]),
        ],
    )
