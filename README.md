# sqlite-fk-witness

Turn SQLite foreign key violations into an actionable report: child/parent column
names, counts per constraint, and optional row locators and key values. In particular,
recover primary-key witnesses for **WITHOUT ROWID** tables, where SQLite's native
`PRAGMA foreign_key_check` returns a NULL rowid.

The engine's PRAGMA remains the authority for violation counts. Optional witness
queries respect the **parent's** affinity and collation, including composite keys,
implicit parent primary keys and NULL exemption. The source opens read-only.

## Install and reproduce

Python 3.11+ with SQLite 3.37+ is required. No runtime packages, server or network.
Clone this repository, then:

```sh
python -m venv .venv
# POSIX: source .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
python -m pip install .
python examples/create_demo.py demo.sqlite
sqlite-fk-witness demo.sqlite --include-values --format json
```

This exits **1**, identifies `tasks(tenant, project)` referencing
`projects(tenant, id)`, and locates the orphan at primary key `task_id='orphan'`.
The nullable unassigned task is valid and is not reported.

```sh
python examples/create_demo.py healthy.sqlite --healthy
sqlite-fk-witness healthy.sqlite
```

This exits 0. Example generation refuses to overwrite files. Remove only the two
generated database files when done. Uninstall with
`python -m pip uninstall sqlite-fk-witness`; remove your virtual environment if
unneeded. `python -m sqlite_fk_witness` is an equivalent entry point. Release wheels
and source archives have SHA256SUMS; install a wheel with
`python -m pip install path/to/file.whl`. No PyPI publication is claimed.

## Privacy and automation

```sh
sqlite-fk-witness production-backup.sqlite --format json
sqlite-fk-witness production-backup.sqlite --include-values --max-samples 10 --format json
sqlite-fk-witness production-backup.sqlite --max-seconds 60 --max-tables 2000
```

The default report contains schema names and counts, **no row values**. Explicit
`--include-values` adds locators and child FK values, which can contain sensitive
information. Do not share unredacted reports. BLOBs use tagged base64; numeric and
text keys retain their type. SQL error messages are replaced with stable codes so
engine messages cannot accidentally echo record content.

Exit codes: **0** all supported checks complete and no violations; **1** complete
with orphan violations; **2** invalid input, failed engine checks, skipped tables
or resource limits; **130** interrupted. A violation count counts **constraint
violations**, not distinct child rows: one row may violate multiple foreign keys.

JSON `schema_version: 1` reports `constraints`, `violations`, `tables_checked`,
`errors`, `skipped`, `complete`, and `ok`. Each constraint has `fk_id`, column lists,
`checked`, `violations`, `samples`, and `witness_status`. IDs are SQLite's local FK
IDs for this schema; they are not stable identifiers across migrations. Failed or
interrupted checks may have partial counts, always with `complete: false`.

The sample cap is global (default 20) and does not stop counting. `witness_status`
is `disabled`, `not-needed`, `bounded`, `sample-cap`, or an `unavailable: ...` reason.
`complete` describes the authoritative orphan scan, **not** whether every orphan
has a witness. Witness errors never erase engine findings. A count/query disagreement
discards that group's samples. Rowid locators are preferred for ordinary tables;
WITHOUT ROWID uses the primary key. If every rowid alias is shadowed, a primary key
is used when present. A nullable fallback key is marked `locator_unique: false`.
Without any accessible key the violation is reported with unavailable witnesses.

```python
from sqlite_fk_witness.core import audit

report = audit("backup.sqlite", include_values=False)
assert report["complete"] and report["ok"]
```

## Boundaries

Only `main` and ordinary user tables are covered. Virtual/shadow tables are
explicit skips and prevent a complete result; views are not tables with declared
FKs and are ignored. Generated child columns work when the local SQLite build can
evaluate them safely. Custom collations/functions and extensions can prevent
checking. The checker loads no extensions and registers no custom functions.

This is not a repair tool, structural integrity check, schema migration verifier,
or a test of deferred transactional behavior. A missing parent with no non-NULL
referencing rows may yield no orphan findings; that does not prove future inserts
will succeed. Malformed parent-key definitions often make PRAGMA fail even on
empty tables. Such failures produce exit 2. Use SQLite integrity checks and
application migration tests alongside this tool.

The connection uses `mode=ro`, `query_only=ON`, `trusted_schema=OFF`, and a single
read transaction. Committed WAL data remains visible; SQLite can access/create
WAL/SHM coordination sidecars. Use a coherent backup for filesystem isolation.
The source is never repaired or modified. No database URI is accepted from the
caller; paths are safely converted to read-only file URIs.

Defaults: 1,000 tables, 10,000 constraints, 30-second cooperative budget, 1 MiB
SQLite value/record length limit. The length limit can also reject wide records or
large schema expressions. A full orphan scan can be expensive; witnesses require
additional bounded-result queries. Limits are not an OS sandbox or hard deadline.

SQLite's [FK semantics](https://www.sqlite.org/foreignkeys.html) and
[foreign_key_check](https://www.sqlite.org/pragma.html#pragma_foreign_key_check)
are the references. This complements the native PRAGMA with joined metadata and
optional locators. Connection-enforcement helpers and migration frameworks remain
the appropriate tools for preventing future violations.

## 中文说明

将 SQLite 外键错误 ID 解析为父子表、列名和数量，显式启用后给出有界的记录定位。
支持 WITHOUT ROWID、复合键、隐式父主键、父列类型亲和性与排序规则。默认只输出
结构和计数，不输出数据值；不会自动修复或修改源数据库。退出 2 表示未完成或环境
不支持，不能当作检查通过。计数表示违反约束的次数，不一定是不同记录数。
