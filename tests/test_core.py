import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from sqlite_fk_witness.core import audit


class WitnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fk # 雪.sqlite"

    def make(self, sql):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(sql)

    def group(self, result, table="child"):
        return next(g for g in result["constraints"] if g["table"] == table)

    def test_basic_and_redaction(self):
        self.make(
            "CREATE TABLE parent(id TEXT PRIMARY KEY); "
            "CREATE TABLE child(id INTEGER PRIMARY KEY, p TEXT REFERENCES parent(id)); "
            "INSERT INTO child VALUES (8,'PRIVATE_MARKER')"
        )
        r = audit(self.path)
        self.assertTrue(r["complete"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["violations"], 1)
        self.assertNotIn("PRIVATE_MARKER", json.dumps(r))
        g = self.group(audit(self.path, include_values=True))
        self.assertEqual(g["samples"][0]["locator"]["_rowid_"]["value"], 8)
        self.assertEqual(g["samples"][0]["child_values"]["p"]["value"], "PRIVATE_MARKER")

    def test_without_rowid_composite_implicit_parent(self):
        self.make(
            "CREATE TABLE parent(a TEXT, b INTEGER, PRIMARY KEY(a,b)) WITHOUT ROWID; "
            "CREATE TABLE child(k TEXT PRIMARY KEY, a TEXT, b INT, "
            "FOREIGN KEY(a,b) REFERENCES parent) WITHOUT ROWID; "
            "INSERT INTO parent VALUES('yes',1); "
            "INSERT INTO child VALUES ('bad','no',2),('ok','yes',1),('null','no',NULL)"
        )
        r = audit(self.path, include_values=True)
        g = self.group(r)
        self.assertEqual(g["parent_columns"], ["a", "b"])
        self.assertEqual(g["violations"], 1)
        self.assertEqual(g["witness_status"], "bounded")
        self.assertEqual(g["samples"][0]["locator"]["k"]["value"], "bad")

    def test_parent_collation_wins(self):
        self.make(
            "CREATE TABLE parent(id TEXT COLLATE NOCASE PRIMARY KEY); "
            "CREATE TABLE child(p TEXT COLLATE BINARY REFERENCES parent(id)); "
            "INSERT INTO parent VALUES ('ABC'); INSERT INTO child VALUES ('abc'),('other')"
        )
        g = self.group(audit(self.path, include_values=True))
        self.assertEqual(g["violations"], 1)
        self.assertEqual(g["samples"][0]["child_values"]["p"]["value"], "other")

    def test_parent_text_affinity_not_child_numeric_affinity(self):
        self.make(
            "CREATE TABLE parent(id TEXT PRIMARY KEY); "
            "CREATE TABLE child(p INTEGER REFERENCES parent(id)); "
            "INSERT INTO parent VALUES ('001'); INSERT INTO child VALUES (1)"
        )
        g = self.group(audit(self.path, include_values=True))
        self.assertEqual(g["violations"], 1)
        self.assertEqual(g["witness_status"], "bounded")

    def test_parent_numeric_affinity(self):
        self.make(
            "CREATE TABLE parent(id INTEGER PRIMARY KEY); "
            "CREATE TABLE child(p TEXT REFERENCES parent(id)); "
            "INSERT INTO parent VALUES (1); INSERT INTO child VALUES ('001'),('2')"
        )
        g = self.group(audit(self.path, include_values=True))
        self.assertEqual(g["violations"], 1)
        self.assertEqual(g["samples"][0]["child_values"]["p"]["value"], "2")

    def test_blob_parent_does_not_inherit_child_text_affinity(self):
        self.make(
            "CREATE TABLE parent(id BLOB PRIMARY KEY); "
            "CREATE TABLE child(p TEXT REFERENCES parent(id)); "
            "INSERT INTO parent VALUES (1); INSERT INTO child VALUES ('1')"
        )
        g = self.group(audit(self.path, include_values=True))
        self.assertEqual(g["violations"], 1)
        self.assertEqual(g["witness_status"], "bounded")

    def test_affinity_matrix_matches_engine(self):
        for parent_type in ["TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"]:
            for child_type in ["TEXT", "INTEGER", "BLOB"]:
                with self.subTest(parent=parent_type, child=child_type):
                    path = Path(self.tmp.name) / f"{parent_type}-{child_type}.sqlite"
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute(f"CREATE TABLE p(id {parent_type} UNIQUE)")
                        db.execute(f"CREATE TABLE child(v {child_type} REFERENCES p(id))")
                        db.executemany(
                            "INSERT OR IGNORE INTO p VALUES (?)", [("001",), ("A",), (b"x",)]
                        )
                        db.executemany(
                            "INSERT INTO child VALUES (?)",
                            [(1,), ("001",), (2,), (None,), ("a",), (b"x",)],
                        )
                        expected = len(db.execute("PRAGMA foreign_key_check").fetchall())
                    r = audit(path, include_values=True)
                    g = self.group(r)
                    self.assertEqual(r["violations"], expected)
                    self.assertEqual(len(g["samples"]), expected)

    def test_multiple_fks_count_violations_not_rows(self):
        self.make(
            "CREATE TABLE p(id PRIMARY KEY); CREATE TABLE child(a REFERENCES p(id), "
            "b REFERENCES p(id)); INSERT INTO child VALUES(1,2)"
        )
        r = audit(self.path, include_values=True)
        self.assertEqual(r["violations"], 2)
        self.assertEqual(len(r["constraints"]), 2)

    def test_all_counts_continue_after_global_sample_cap(self):
        self.make(
            "CREATE TABLE p(id PRIMARY KEY); CREATE TABLE child(v REFERENCES p(id)); "
            "INSERT INTO child VALUES(1),(2),(3),(4)"
        )
        g = self.group(audit(self.path, include_values=True, max_samples=1))
        self.assertEqual(g["violations"], 4)
        self.assertEqual(len(g["samples"]), 1)
        self.assertEqual(
            self.group(audit(self.path, include_values=True, max_samples=0))["samples"], []
        )

    def test_missing_parent_with_nonnull_child(self):
        self.make("CREATE TABLE child(v REFERENCES absent(id)); INSERT INTO child VALUES(5),(NULL)")
        r = audit(self.path, include_values=True)
        self.assertTrue(r["complete"])
        self.assertEqual(r["violations"], 1)
        self.assertEqual(self.group(r)["witness_status"], "bounded")

    def test_mismatched_parent_key_incomplete_even_empty(self):
        self.make("CREATE TABLE p(v); CREATE TABLE child(v REFERENCES p(v))")
        r = audit(self.path)
        self.assertFalse(r["complete"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["errors"][0]["code"], "foreign-key-check-failed")

    def test_no_constraints_is_complete(self):
        self.make("CREATE TABLE t(v); CREATE VIEW v AS SELECT * FROM t")
        self.assertTrue(audit(self.path)["ok"])

    def test_quoted_and_case_varied_identifiers(self):
        self.make(
            'CREATE TABLE "P;--" ("K""ey" TEXT PRIMARY KEY); '
            'CREATE TABLE "child" ("x""y" TEXT REFERENCES "p;--"("k""ey")); '
            "INSERT INTO child VALUES ('missing')"
        )
        g = self.group(audit(self.path, include_values=True))
        self.assertEqual(g["parent"], "P;--")
        self.assertEqual(g["witness_status"], "bounded")

    def test_shadowed_rowid_uses_other_alias(self):
        self.make(
            "CREATE TABLE p(k PRIMARY KEY); CREATE TABLE child(_rowid_, v REFERENCES p(k)); "
            "INSERT INTO child VALUES('label',3)"
        )
        g = self.group(audit(self.path, include_values=True))
        self.assertEqual(g["samples"][0]["locator"]["rowid"]["value"], 1)

    def test_all_rowid_aliases_shadowed_explicitly_unavailable(self):
        self.make(
            "CREATE TABLE p(k PRIMARY KEY); CREATE TABLE child(rowid, oid, _rowid_, "
            "v REFERENCES p(k)); INSERT INTO child VALUES(4,5,6,7)"
        )
        g = self.group(audit(self.path, include_values=True))
        self.assertEqual(g["violations"], 1)
        self.assertTrue(g["witness_status"].startswith("unavailable"))

    def test_blob_keys_json(self):
        self.make(
            "CREATE TABLE p(k BLOB PRIMARY KEY); CREATE TABLE child(k BLOB PRIMARY KEY, "
            "v BLOB REFERENCES p(k)) WITHOUT ROWID; INSERT INTO child VALUES(x'00ff',x'03')"
        )
        sample = self.group(audit(self.path, include_values=True))["samples"][0]
        self.assertEqual(sample["locator"]["k"], {"type": "blob", "base64": "AP8="})
        json.dumps(sample, allow_nan=False)

    def test_generated_child_key(self):
        self.make(
            "CREATE TABLE p(k INTEGER PRIMARY KEY); CREATE TABLE child(x INT, "
            "v INT GENERATED ALWAYS AS (x+1) REFERENCES p(k)); INSERT INTO child(x) VALUES(4)"
        )
        g = self.group(audit(self.path, include_values=True))
        self.assertEqual(g["violations"], 1)
        self.assertEqual(g["samples"][0]["child_values"]["v"]["value"], 5)

    def test_readonly_source_hash(self):
        self.make(
            "CREATE TABLE p(k PRIMARY KEY); CREATE TABLE child(v REFERENCES p(k)); "
            "INSERT INTO child VALUES (4)"
        )
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        audit(self.path, include_values=True)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_live_wal(self):
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        db.executescript(
            "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; "
            "CREATE TABLE p(k PRIMARY KEY); CREATE TABLE child(v REFERENCES p(k)); "
            "INSERT INTO child VALUES(99)"
        )
        self.assertEqual(audit(self.path)["violations"], 1)

    def test_table_and_constraint_limits(self):
        self.make(
            "CREATE TABLE p(k PRIMARY KEY); CREATE TABLE child(a REFERENCES p(k), "
            "b REFERENCES p(k))"
        )
        for kwargs in [{"max_tables": 1}, {"max_constraints": 1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                audit(self.path, **kwargs)

    def test_deadline_is_incomplete(self):
        self.make("CREATE TABLE t(v)")
        with patch("sqlite_fk_witness.core.time.perf_counter", side_effect=[0] + [31] * 1000):
            self.assertFalse(audit(self.path)["complete"])

    def test_corrupt_and_missing(self):
        with self.assertRaises(FileNotFoundError):
            audit(self.path)
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b"not a sqlite database")
        self.assertFalse(audit(self.path)["complete"])

    def test_bad_options(self):
        self.make("CREATE TABLE t(v)")
        for kwargs in [
            {"max_tables": 0},
            {"max_samples": -1},
            {"max_seconds": float("nan")},
            {"max_seconds": float("inf")},
            {"max_value_bytes": 10},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                audit(self.path, **kwargs)

    def test_virtual_tables_are_incomplete(self):
        with closing(sqlite3.connect(self.path)) as db:
            if not db.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]:
                self.skipTest("SQLite build has no FTS5")
            db.execute("CREATE VIRTUAL TABLE ft USING fts5(body)")
        r = audit(self.path)
        self.assertFalse(r["complete"])
        self.assertTrue(r["skipped"])

    def test_cli_json_and_exit_codes(self):
        self.make(
            "CREATE TABLE p(k PRIMARY KEY); CREATE TABLE child(v REFERENCES p(k)); "
            "INSERT INTO child VALUES(4)"
        )
        base = [sys.executable, "-m", "sqlite_fk_witness", str(self.path), "--format", "json"]
        for extra, status in [([], 1), (["--include-values"], 1), (["--max-tables", "0"], 2)]:
            p = subprocess.run(base + extra, capture_output=True, text=True, check=False)
            self.assertEqual(p.returncode, status, p.stderr + p.stdout)
            self.assertIsInstance(json.loads(p.stdout), dict)


if __name__ == "__main__":
    unittest.main()
