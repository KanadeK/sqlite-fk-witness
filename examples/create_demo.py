"""Generate a broken composite foreign key in a WITHOUT ROWID child table."""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("path", type=Path)
parser.add_argument("--healthy", action="store_true")
args = parser.parse_args()
with args.path.open("xb"):
    pass
with closing(sqlite3.connect(args.path)) as db, db:
    db.executescript("""
    CREATE TABLE projects(tenant TEXT, id INTEGER, PRIMARY KEY(tenant,id)) WITHOUT ROWID;
    CREATE TABLE tasks(task_id TEXT PRIMARY KEY, tenant TEXT, project INTEGER,
      FOREIGN KEY(tenant,project) REFERENCES projects) WITHOUT ROWID;
    INSERT INTO projects VALUES('demo',1);
    INSERT INTO tasks VALUES('valid','demo',1),('orphan','demo',99),('unassigned',NULL,NULL);
    """)
    if args.healthy:
        db.execute("INSERT INTO projects VALUES('demo',99)")
print("Created healthy sample." if args.healthy else "Created one WITHOUT ROWID orphan FK.")
