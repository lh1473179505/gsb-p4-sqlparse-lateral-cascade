#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

"""Tests for PostgreSQL LATERAL binding in FROM lists.

The invariant under test: every LATERAL keyword binds to the derived
table (subquery) or table function that *immediately* follows it,
forming a single FROM item.  This must hold uniformly for
comma-separated FROM lists and for JOIN forms, for chained LATERAL
items, and for mixtures with plain derived tables.
"""

import pytest

import sqlparse
from sqlparse import sql, tokens as T

# A: comma LATERAL + subquery with WHERE/ORDER/LIMIT + outer WHERE
SQL_A = (
    'SELECT * FROM t, LATERAL (\n'
    '  SELECT u.id FROM u WHERE u.t_id = t.id ORDER BY u.id LIMIT 1\n'
    ') x WHERE x.id IS NOT NULL;'
)

# B: CTE + two chained comma LATERAL items (cascading-list regression)
SQL_B = (
    'WITH s AS (SELECT 1 AS id)\n'
    'SELECT * FROM s,\n'
    '  LATERAL (SELECT s.id + 1 AS n) x,\n'
    '  LATERAL (SELECT x.n AS m) y;'
)

# C: plain derived table mixed with a LATERAL item
SQL_C = 'SELECT * FROM t, (SELECT 1) AS plain, LATERAL (SELECT 2) AS lat;'

# D: JOIN forms (must not regress)
SQL_D1 = ('SELECT * FROM t LEFT JOIN LATERAL (SELECT 1 AS n) x '
          'ON true WHERE t.id > 0;')
SQL_D2 = ('SELECT * FROM t CROSS JOIN LATERAL '
          'jsonb_array_elements(t.js) AS x(val);')


def _walk(tlist):
    for token in tlist.tokens:
        yield token
        if token.is_group:
            yield from _walk(token)


def _lateral_keywords(stmt):
    return [tk for tk in _walk(stmt)
            if tk.ttype is T.Keyword and tk.normalized == 'LATERAL']


def _identifier_lists(stmt):
    return [tk for tk in _walk(stmt) if isinstance(tk, sql.IdentifierList)]


def assert_no_dangling_lateral_list(stmt):
    """No IdentifierList may swallow a LATERAL keyword.

    Forbids ``t, LATERAL``, ``s, LATERAL``, ``(...) x, LATERAL`` and
    ``t, (SELECT 1) AS plain, LATERAL`` shapes: a LATERAL keyword must
    never appear as a direct member of an IdentifierList, and no list
    element may *end* in a bare LATERAL.
    """
    for ilist in _identifier_lists(stmt):
        for token in ilist.tokens:
            assert not (token.ttype is T.Keyword
                        and token.normalized == 'LATERAL'), \
                f'bare LATERAL inside IdentifierList: {ilist.value!r}'
        for ident in ilist.get_identifiers():
            _, last = ident.token_prev(len(ident.tokens), skip_cm=True)
            assert not (last is not None and last.ttype is T.Keyword
                        and last.normalized == 'LATERAL'), \
                f'identifier ends in dangling LATERAL: {ident.value!r}'


def assert_lateral_bound(stmt, count):
    """Every LATERAL forms one FROM item with its subquery/function."""
    laterals = _lateral_keywords(stmt)
    assert len(laterals) == count
    for kw in laterals:
        item = kw.parent
        assert isinstance(item, sql.Identifier), \
            f'LATERAL not grouped into a single FROM item: {kw.parent!r}'
        assert item.value.lstrip().upper().startswith('LATERAL')
        # the bound source (subquery parenthesis or table function)
        # lives inside the same FROM item
        sources = [tk for tk in _walk(item)
                   if isinstance(tk, (sql.Parenthesis, sql.Function))]
        assert sources, f'LATERAL item misses its source: {item.value!r}'


def assert_from_invariants(stmt, lateral_count):
    assert_no_dangling_lateral_list(stmt)
    assert_lateral_bound(stmt, lateral_count)


def _from_items(stmt):
    """Top-level FROM items (Identifiers / IdentifierList members)."""
    from_idx, _ = stmt.token_next_by(m=(T.Keyword, 'FROM'))
    items = []
    for token in stmt.tokens[from_idx + 1:]:
        if isinstance(token, sql.IdentifierList):
            items.extend(token.get_identifiers())
        elif isinstance(token, sql.Identifier):
            items.append(token)
        elif token.ttype in T.Keyword:
            # stop at the next clause keyword (WHERE, GROUP BY, ...)
            if token.normalized.split()[0] in (
                    'WHERE', 'GROUP', 'ORDER', 'LIMIT', 'HAVING'):
                break
    return items


def test_comma_lateral_subquery_and_outer_where():
    stmt = sqlparse.parse(SQL_A)[0]
    assert_from_invariants(stmt, 1)

    # outer WHERE stays a statement-level Where clause
    wheres = [tk for tk in stmt.tokens if isinstance(tk, sql.Where)]
    assert len(wheres) == 1
    assert 'x.id IS NOT NULL' in wheres[0].value

    # inner WHERE stays inside the lateral subquery
    [lateral] = [it for it in _from_items(stmt)
                 if it.value.lstrip().upper().startswith('LATERAL')]
    inner_wheres = [tk for tk in _walk(lateral) if isinstance(tk, sql.Where)]
    assert len(inner_wheres) == 1
    assert 'u.t_id = t.id' in inner_wheres[0].value
    assert lateral.get_alias() == 'x'


def test_chained_lateral_with_cte_no_cascade():
    stmt = sqlparse.parse(SQL_B)[0]
    assert_from_invariants(stmt, 2)

    items = _from_items(stmt)
    assert len(items) == 3
    # driving CTE reference stays a plain item
    assert items[0].value == 's'
    # both LATERAL items are bound, with recognizable aliases
    laterals = items[1:]
    assert [it.get_alias() for it in laterals] == ['x', 'y']
    assert all(it.value.lstrip().upper().startswith('LATERAL')
               for it in laterals)
    assert '(SELECT s.id + 1 AS n)' in laterals[0].value
    assert '(SELECT x.n AS m)' in laterals[1].value


def test_plain_derived_table_not_absorbed():
    stmt = sqlparse.parse(SQL_C)[0]
    assert_from_invariants(stmt, 1)

    items = _from_items(stmt)
    assert len(items) == 3
    plain, lat = items[1], items[2]
    # the plain derived table must not gain a LATERAL
    assert 'LATERAL' not in plain.value.upper()
    assert plain.get_alias() == 'plain'
    # ...while the lateral item is bound to its subquery
    assert lat.value.lstrip().upper().startswith('LATERAL')
    assert lat.get_alias() == 'lat'


def test_left_join_lateral_subquery():
    stmt = sqlparse.parse(SQL_D1)[0]
    assert_from_invariants(stmt, 1)

    [lateral] = [it for it in _from_items(stmt)
                 if it.value.lstrip().upper().startswith('LATERAL')]
    assert '(SELECT 1 AS n)' in lateral.value
    assert lateral.get_alias() == 'x'
    # JOIN keyword and ON condition are intact
    joined = stmt.value.upper()
    assert 'LEFT JOIN' in joined and 'ON TRUE' in joined
    wheres = [tk for tk in stmt.tokens if isinstance(tk, sql.Where)]
    assert len(wheres) == 1
    assert 't.id > 0' in wheres[0].value


def test_cross_join_lateral_table_function():
    stmt = sqlparse.parse(SQL_D2)[0]
    assert_from_invariants(stmt, 1)

    [lateral] = [it for it in _from_items(stmt)
                 if it.value.lstrip().upper().startswith('LATERAL')]
    functions = [tk for tk in _walk(lateral)
                 if isinstance(tk, sql.Function)]
    assert any('jsonb_array_elements' in f.value for f in functions)
    assert 'AS x(val)' in lateral.value


@pytest.mark.parametrize('sql_text,lateral_count', [
    (SQL_A, 1),
    (SQL_B, 2),
    (SQL_C, 1),
    (SQL_D1, 1),
    (SQL_D2, 1),
])
def test_format_reindent_roundtrip_keeps_binding(sql_text, lateral_count):
    """parse -> format(reindent=True) -> parse keeps the invariants."""
    formatted = sqlparse.format(sql_text, reindent=True)
    for stmt in sqlparse.parse(formatted):
        assert_from_invariants(stmt, lateral_count)


@pytest.mark.parametrize('sql_text', [SQL_A, SQL_B, SQL_C, SQL_D1, SQL_D2])
def test_lateral_item_class_is_uniform(sql_text):
    """One binding rule: comma and JOIN forms use the same node type."""
    stmt = sqlparse.parse(sql_text)[0]
    for kw in _lateral_keywords(stmt):
        assert type(kw.parent) is sql.Identifier


@pytest.mark.parametrize('sql_text', [
    'SELECT * FROM t, u;',
    'SELECT * FROM t, (SELECT 1) x;',
    'SELECT a FROM t LEFT JOIN u ON t.id = u.id;',
    'SELECT * FROM t CROSS JOIN u;',
])
def test_non_lateral_from_items_untouched(sql_text):
    stmt = sqlparse.parse(sql_text)[0]
    # no LATERAL keyword is invented and nothing is mis-grouped
    assert not _lateral_keywords(stmt)
    assert_no_dangling_lateral_list(stmt)
    for ident in _walk(stmt):
        if isinstance(ident, sql.Identifier):
            assert 'LATERAL' not in ident.value.upper()
