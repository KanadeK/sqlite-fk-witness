# Design and review

PRAGMA foreign_key_check is the data-integrity oracle. Each ordinary table is
checked separately so a malformed parent key in one table does not hide every
other table's diagnostics. Errors and unsupported tables still prevent success.
FK IDs are resolved against pragma_foreign_key_list from the same read transaction.

Witnesses are optional. A correlated NOT EXISTS query uses `parent.col = +child.col`.
Unary plus removes child affinity: using `parent.col = child.col` can wrongly
coerce a TEXT parent like '001' to a number when the child is numeric. The left
parent column supplies collation. NULL in any child component exempts that row.
Implicit references resolve to the parent's ordered PK columns. Missing parents
use the non-NULL child predicate. Counts remain the engine's, not query-derived.

Review covered ASCII-only identifier folding, safely quoted names, invisible
rowids, nullable PK fallback, missing parent tables, mismatched parent keys,
WITHOUT ROWID, generated keys, global sample limits, and private diagnostics.
The suite includes a 15-combination parent/child affinity matrix, an explicit
NOCASE-vs-BINARY regression, direct PRAGMA comparisons, live WAL and source hashes.

The runtime contains only Python standard-library code. It does not deserialize
objects, load SQLite extensions, fetch URLs, execute user SQL, or mutate the source.
Resource limits are cooperative and SQLite's native parser is not a sandbox.
