# -*- coding: utf-8 -*-
"""Every column a query names must exist.

An automation rule with a "message" action wrote to `ws_messages.author_user_id`
for as long as the feature had existed. There is no such column. The rule threw
every time it fired, and 2000 passing checks never noticed, because nothing in
the suite happened to take that branch.

That is the shape of the problem: a rarely-taken branch containing SQL that has
simply never run. No amount of endpoint coverage finds it, because it is not an
endpoint. So this reads the queries themselves and checks them against the real
schema, which costs nothing and does not care how obscure the branch is.

It builds the schema in a scratch database rather than parsing db.py, so what it
compares against is what SQLite would actually create.
"""
import io
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from seamclient import Client                                # noqa: E402
import db as D                                               # noqa: E402

c = Client()

#: SQL in this codebase is written in uppercase, without exception - checked,
#: not assumed. Matching case-sensitively is what keeps prose out: the first
#: run of this file reported a table called "one", from a docstring saying
#: "update one field at a time".
#: Names that appear where a column name would but are not columns: SQL
#: functions, the upsert pseudo-table, and Python's own format placeholder in
#: queries whose column list is built at runtime.
NOT_COLUMNS = {"datetime", "date", "time", "max", "min", "coalesce", "excluded",
               "json_extract", "s", "d"}

# --------------------------------------------------------------------------- #
fd, path = tempfile.mkstemp(suffix=".db")
os.close(fd)
conn = sqlite3.connect(path)
conn.executescript(D.SCHEMA)
cols = {}
for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    cols[t] = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)}
conn.close()
os.remove(path)

c.check("the schema builds from scratch", len(cols) > 40, "%d tables" % len(cols))

# Columns added by _migrate() to older databases are real on a live instance
# even though CREATE TABLE does not mention them, so read those too.
mig_src = io.open(os.path.join(ROOT, "db.py"), encoding="utf-8").read()
for m in re.finditer(r'ALTER TABLE (\w+) ADD COLUMN (\w+)', mig_src):
    cols.setdefault(m.group(1), set()).add(m.group(2))

SRC = {name: io.open(os.path.join(ROOT, name), encoding="utf-8").read()
       for name in sorted(os.listdir(ROOT)) if name.endswith(".py")}

bad = []
for name, src in SRC.items():
    for m in re.finditer(r'INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*\(([^)]*)\)', src, re.S):
        table, names = m.group(1), m.group(2)
        if table not in cols or "%s" in names:
            continue
        for col in re.findall(r'\b([a-z_][a-z0-9_]*)\b', names):
            if col not in cols[table] and col not in NOT_COLUMNS:
                bad.append("%s:%d  INSERT INTO %s (%s)"
                           % (name, src[:m.start()].count("\n") + 1, table, col))
    for m in re.finditer(r'UPDATE\s+(\w+)\s+SET\s+(.+?)(?:\bWHERE\b|"|\')', src, re.S):
        table, sets = m.group(1), m.group(2)
        if table not in cols or "%s" in sets:
            continue
        for col in re.findall(r'([a-z_][a-z0-9_]*)\s*=', sets):
            if col not in cols[table] and col not in NOT_COLUMNS:
                bad.append("%s:%d  UPDATE %s SET %s="
                           % (name, src[:m.start()].count("\n") + 1, table, col))

c.check("every column written to exists in the schema", not bad, "\n      ".join(bad[:8]))

# The same for tables: a query naming a table that was renamed or never created
# fails only when its branch runs.
missing_tables = []
for name, src in SRC.items():
    for m in re.finditer(r'\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(\w+)\b',
                         src):
        t = m.group(1)
        if t.islower() and t not in cols and t not in ("sqlite_master",):
            missing_tables.append("%s:%d  %s" % (name, src[:m.start()].count("\n") + 1, t))
c.check("every table written to exists in the schema", not missing_tables,
        "\n      ".join(sorted(set(missing_tables))[:8]))

# The regression this file was written for, named so it cannot come back
# quietly under a different column.
wsm = cols.get("ws_messages", set())
c.check("ws_messages stores its author in user_id", "user_id" in wsm, str(sorted(wsm)))
c.check("and nothing writes to author_user_id",
        not any("author_user_id" in s for s in SRC.values()),
        [n for n, s in SRC.items() if "author_user_id" in s])

sys.exit(c.summary())
