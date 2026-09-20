#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

"""Regression tests for PostgreSQL LATERAL binding in FROM lists.

Each LATERAL keyword must form a single FROM item together with the
subquery or table function that immediately follows it.  In particular
comma-separated FROM lists must never swallow a bare LATERAL keyword
into an IdentifierList (``t, LATERAL``), and chained LATERAL items must
not cascade into secondary misgroupings (``(subquery) x, LATERAL``).
"""

import pytest

import sqlparse
from sqlparse import sql
from sqlparse import tokens as T

SQL_A = """SELECT * FROM t, LATERAL (
  SELECT u.id FROM u WHERE u.t_id = t.id ORDER BY u.id LIMIT 1
) x WHERE x.id IS NOT NULL;"""

SQL_B = """WITH s AS (SELECT 1 AS id)
SELECT * FROM s,
  LATERAL (SELECT s.id + 1 AS n) x,
  LATERAL (SELECT x.n AS m) y;"""

SQL_C = "SELECT * FROM t, (SELECT 1) AS plain, LATERAL (SELECT 2) AS lat;"

SQL_D1 = ("SELECT * FROM t LEFT JOIN LATERAL (SELECT 1 AS n) x "
          "ON true WHERE t.id > 0;")

SQL_D2 = ("SELECT * FROM t CROSS JOIN LATERAL "
          "jsonb_array_elements(t.js) AS x(val);")


def _iter_groups(token_list):
    """Yield token_list and all nested TokenList groups."""
    yield token_list
    for token in token_list.tokens:
        if token.is_group:
            yield from _iter_groups(token)


def _lateral_leaves(stmt):
    return [tk for tk in stmt.flatten()
            if tk.match(T.Keyword, 'LATERAL')]


def assert_no_dangling_lateral(stmt):
    """No IdentifierList may end in / contain a bare LATERAL keyword."""
    for group in _iter_groups(stmt):
        if not isinstance(group, sql.IdentifierList):
            continue
        for token in group.tokens:
            assert not (
                not token.is_group
                and token.match(T.Keyword, 'LATERAL')
            ), f"bare LATERAL inside IdentifierList: {group.value!r}"


def assert_lateral_bound(stmt, count):
    """Every LATERAL is grouped with its source into a single item."""
    laterals = _lateral_leaves(stmt)
    assert len(laterals) == count
    items = []
    for lateral in laterals:
        item = lateral.parent
        assert isinstance(item, sql.Identifier), (
            f"LATERAL not bound to its source: {item!r}")
        # LATERAL is the first real token of the item ...
        assert item.token_first(skip_cm=True) is lateral
        # ... and the item also holds the following source.
        assert len(item.value) > len('LATERAL')
        items.append(item)
    return items


def assert_lateral_invariants(statement, count):
    stmt = sqlparse.parse(statement)[0]
    assert_no_dangling_lateral(stmt)
    return assert_lateral_bound(stmt, count)


def test_lateral_comma_subquery():
    """Case A: comma LATERAL with inner WHERE/ORDER/LIMIT + outer WHERE."""
    stmt = sqlparse.parse(SQL_A)[0]
    assert_no_dangling_lateral(stmt)
    (item,) = assert_lateral_bound(stmt, 1)
    assert 'LATERAL' in item.value
    assert 'SELECT u.id FROM u' in item.value

    # The outer WHERE is a top-level clause of the statement ...
    outer_where = stmt.token_next_by(i=sql.Where)[1]
    assert outer_where is not None
    assert 'x.id IS NOT NULL' in outer_where.value
    # ... and not nested inside the lateral item.
    assert outer_where.parent is stmt

    # The inner WHERE stays inside the lateral subquery.
    inner_wheres = [g for g in _iter_groups(item)
                    if isinstance(g, sql.Where)]
    assert len(inner_wheres) == 1
    assert 'u.t_id = t.id' in inner_wheres[0].value


def test_lateral_chained_with_cte():
    """Case B: CTE + two chained comma LATERAL items (no cascade)."""
    stmt = sqlparse.parse(SQL_B)[0]
    assert_no_dangling_lateral(stmt)
    first, second = assert_lateral_bound(stmt, 2)

    assert '(SELECT s.id + 1 AS n)' in first.value
    assert first.get_alias() == 'x'
    assert '(SELECT x.n AS m)' in second.value
    assert second.get_alias() == 'y'

    # The driving table s and both lateral items are distinguishable
    # members of one FROM list.
    from_list = stmt.token_next_by(i=sql.IdentifierList)[1]
    assert from_list is not None
    identifiers = list(from_list.get_identifiers())
    assert len(identifiers) == 3
    assert identifiers[0].get_real_name() == 's'
    assert identifiers[1] is first
    assert identifiers[2] is second


def test_lateral_mixed_with_plain_derived_table():
    """Case C: plain derived table must not be pulled into LATERAL."""
    stmt = sqlparse.parse(SQL_C)[0]
    assert_no_dangling_lateral(stmt)
    (item,) = assert_lateral_bound(stmt, 1)
    assert '(SELECT 2)' in item.value
    assert item.get_alias() == 'lat'

    from_list = stmt.token_next_by(i=sql.IdentifierList)[1]
    identifiers = list(from_list.get_identifiers())
    assert len(identifiers) == 3
    plain = identifiers[1]
    assert plain.get_alias() == 'plain'
    assert 'LATERAL' not in plain.value.upper()


def test_lateral_left_join():
    """Case D1: LEFT JOIN LATERAL keeps LATERAL bound to its source."""
    stmt = sqlparse.parse(SQL_D1)[0]
    (item,) = assert_lateral_bound(stmt, 1)
    assert '(SELECT 1 AS n)' in item.value
    assert item.get_alias() == 'x'
    # The JOIN keyword itself stays outside the lateral item.
    assert 'JOIN' not in item.value.upper()


def test_lateral_cross_join_table_function():
    """Case D2: CROSS JOIN LATERAL with a table function + column alias."""
    stmt = sqlparse.parse(SQL_D2)[0]
    (item,) = assert_lateral_bound(stmt, 1)
    assert 'jsonb_array_elements(t.js)' in item.value
    assert 'x(val)' in item.value


@pytest.mark.parametrize('statement, count', [
    (SQL_A, 1),
    (SQL_B, 2),
    (SQL_C, 1),
    (SQL_D1, 1),
    (SQL_D2, 1),
])
def test_lateral_format_reindent_roundtrip(statement, count):
    """format(reindent=True) must not detach LATERAL from its source."""
    formatted = sqlparse.format(statement, reindent=True)
    assert_lateral_invariants(formatted, count)


@pytest.mark.parametrize('statement', [
    # plain comma joins stay untouched
    'SELECT * FROM t, u;',
    'SELECT a, b FROM t, u WHERE t.id = u.id;',
    # non-lateral derived tables stay untouched
    'SELECT * FROM t, (SELECT 1) AS x;',
    # non-lateral joins stay untouched
    'SELECT * FROM t LEFT JOIN u ON t.id = u.id;',
    'SELECT * FROM t CROSS JOIN u;',
])
def test_no_lateral_false_positives(statement):
    stmt = sqlparse.parse(statement)[0]
    assert _lateral_leaves(stmt) == []
    formatted = sqlparse.format(statement, reindent=True)
    assert _lateral_leaves(sqlparse.parse(formatted)[0]) == []
