"""Exact lookup and course-filtered lexical search over manifest-enabled standards."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from .schemas import StandardEvidence


class CorpusError(ValueError):
    pass


class StandardsCatalog:
    def __init__(self, index_path: Path, manifest_path: Path):
        index_bytes = index_path.read_bytes()
        self.index_sha256 = hashlib.sha256(index_bytes).hexdigest()
        self.index = json.loads(index_bytes)
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.documents = {d["course_key"]: d for d in self.manifest["documents"]}
        counts = Counter(r["standard_key"] for r in self.index["standards"])
        self.available = {}
        for record in self.index["standards"]:
            course_key = record["standard_key"].rsplit("|", 1)[0]
            doc = self.documents.get(course_key)
            if doc is None:
                raise CorpusError("An indexed course is missing from the manifest. Rebuild the index.")
            for record_field, document_field in (
                ("document_sha256", "sha256"), ("source_url", "source_url"),
                ("standards_version", "standards_version"), ("source_file", "local_path"),
                ("extraction_review_status", "extraction_review_status"),
            ):
                if record.get(record_field) != doc.get(document_field):
                    raise CorpusError("Index provenance differs from the manifest. Rebuild the index.")
            if doc["served"] and counts[record["standard_key"]] == 1 and not record["standard_key_ambiguous"]:
                self.available[record["standard_key"]] = record

    def courses(self):
        return [dict(course_key=d["course_key"], course=d["course"], subject=d["subject"],
                     standards_version=d["standards_version"], served=d["served"], notes=d.get("notes"),
                     available_standards=sum(k.rsplit("|", 1)[0] == d["course_key"] for k in self.available))
                for d in self.manifest["documents"]]

    def search(self, course_key: str, query: str = "", limit: int = 40):
        doc = self.documents.get(course_key)
        if not doc:
            raise CorpusError("Select a course from the course list.")
        if not doc["served"]:
            raise CorpusError(f"{doc['course']} is unavailable: {doc.get('notes')}")
        query = query.strip().casefold()
        terms = set(re.findall(r"[\w.]+", query))
        ranked = []
        for key, record in self.available.items():
            if key.rsplit("|", 1)[0] != course_key:
                continue
            body = " ".join(str(record.get(k) or "") for k in
                            ("printed_code", "description", "domain", "title")).casefold()
            exact = query in {key.casefold(), record["printed_code"].casefold()}
            score = 1000 if exact else sum(term in body for term in terms)
            if not query or score:
                ranked.append((score, key, record))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [StandardEvidence.from_index_record(r, f"search-{i}")
                for i, (_, _, r) in enumerate(ranked[:limit], 1)]

    def select(self, keys: list[str]):
        if not 1 <= len(keys) <= 3 or len(set(keys)) != len(keys):
            raise CorpusError("Select one to three distinct standards for this draft.")
        if len({key.rsplit("|", 1)[0] for key in keys}) != 1:
            raise CorpusError("Select standards from one course for this draft.")
        if any(key not in self.available for key in keys):
            raise CorpusError("A selected standard is unavailable or ambiguous. Check the course restrictions.")
        return [StandardEvidence.from_index_record(self.available[key], f"S{i}")
                for i, key in enumerate(keys, 1)]

    def check_snapshot(self, evidence):
        for saved in evidence.standards:
            current = self.select([saved.standard_key])[0]
            current.evidence_ref = saved.evidence_ref
            if current != saved:
                raise CorpusError("A saved standard changed. Save the planning context again before generating.")
