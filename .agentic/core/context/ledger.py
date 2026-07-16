"""Context-package ledger: one JSONL record per built package.

Records contain ids, categories, token counts, and inclusion/omission
reasons — never content bodies, so no secrets can land here. Everything is
still passed through redact() as defence in depth.
"""
import json
import os

from ..redact import redact

LEDGER_FILENAME = "context-ledger.jsonl"


def ledger_path(memory_dir):
    return os.path.join(memory_dir, LEDGER_FILENAME)


def ledger_appender(memory_dir):
    """Returns a callable(record dict) that appends to the ledger."""
    def write(record):
        os.makedirs(memory_dir, exist_ok=True)
        line = redact(json.dumps(record, default=str, ensure_ascii=False))
        with open(ledger_path(memory_dir), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return write


def read_packages(memory_dir, package_id=None, limit=50):
    """Read ledger records, newest last. Filter by package_id if given."""
    path = ledger_path(memory_dir)
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if package_id and record.get("package_id") != package_id:
                continue
            records.append(record)
    return records[-limit:]
