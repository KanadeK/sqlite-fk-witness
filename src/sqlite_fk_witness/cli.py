import argparse
import json
import sqlite3
import sys

from . import __version__
from .core import audit


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explain SQLite orphan foreign key violations.")
    parser.add_argument("database")
    parser.add_argument("--include-values", action="store_true", help="include keys and FK values")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--max-samples", type=int, default=20)
    parser.add_argument("--max-seconds", type=float, default=30)
    parser.add_argument("--max-tables", type=int, default=1000)
    parser.add_argument("--max-constraints", type=int, default=10000)
    parser.add_argument("--max-value-bytes", type=int, default=1_048_576)
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    try:
        result = audit(
            args.database,
            include_values=args.include_values,
            max_samples=args.max_samples,
            max_seconds=args.max_seconds,
            max_tables=args.max_tables,
            max_constraints=args.max_constraints,
            max_value_bytes=args.max_value_bytes,
        )
    except (OSError, ValueError) as exc:
        result = {"schema_version": 1, "ok": False, "complete": False, "error": str(exc)}
    except sqlite3.Error:
        result = {
            "schema_version": 1,
            "ok": False,
            "complete": False,
            "error": "SQLite could not open the input",
        }
    except KeyboardInterrupt:
        return 130
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    else:
        print(
            f"violations={result.get('violations', 0)} complete={result['complete']} "
            f"ok={result['ok']}"
        )
        for group in result.get("constraints", []):
            if group["violations"] or not group["checked"]:
                print(json.dumps(group, ensure_ascii=True, allow_nan=False))
        for error in result.get("errors", []) + result.get("skipped", []):
            print(json.dumps(error), file=sys.stderr)
        if "error" in result:
            print(result["error"], file=sys.stderr)
    return 2 if not result["complete"] else (0 if result["ok"] else 1)
