"""Read-only view of the standards index.

Only summary statistics are exposed for now. Retrieval over the 401 standard
records is the next piece of work and will live alongside this module.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .config import get_settings


@lru_cache
def load_index() -> dict[str, Any]:
    path = get_settings().standards_index
    if not path.exists():
        raise FileNotFoundError(f"Standards index not found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def summary() -> dict[str, Any]:
    """Per-subject counts, derived from the standard records themselves."""
    index = load_index()
    metadata = index.get("metadata", {})
    standards = index.get("standards", [])

    subjects: dict[str, dict[str, Any]] = {}
    for standard in standards:
        subject = standard.get("subject") or "Unspecified"
        bucket = subjects.setdefault(
            subject, {"subject": subject, "total": 0, "essential": 0, "courses": set()}
        )
        bucket["total"] += 1
        bucket["essential"] += bool(standard.get("is_essential"))
        if course := standard.get("grade_or_course"):
            bucket["courses"].add(course)

    rows = [
        {**bucket, "courses": len(bucket["courses"])}
        for bucket in sorted(subjects.values(), key=lambda b: -b["total"])
    ]

    return {
        "title": metadata.get("title", "Standards index"),
        "generated_at": metadata.get("generated_at"),
        "total": len(standards),
        "essential": sum(row["essential"] for row in rows),
        "subjects": rows,
        "retrieval_connected": False,
    }
