"""Deterministic counting over normalized dimensions. Citations are collected while counting.

Counting rule: a trial counts at most once per bucket, even if it carries the
same value several times (e.g. many sites in one country).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from app.data.dimensions import DIMENSIONS, Dimension, Extracted, brief_title, nct_id
from app.data.normalize import UNKNOWN
from app.schemas.enums import DimensionName
from app.schemas.response import Citation

MAX_CITATIONS = 5
STUDY_URL = "https://clinicaltrials.gov/study/{}"


def make_citation(study: dict[str, Any], value: Extracted) -> Citation:
    nid = nct_id(study)
    return Citation(
        nct_id=nid, field=value.field_path, excerpt=value.excerpt, title=brief_title(study), url=STUDY_URL.format(nid)
    )


@dataclass
class Bucket:
    key: str | float
    label: str
    nct_ids: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    estimated: int = 0
    _seen: set[str] = field(default_factory=set, repr=False)

    @property
    def count(self) -> int:
        return len(self.nct_ids)

    def add(self, study: dict, value: Extracted, max_citations: int = MAX_CITATIONS) -> None:
        nid = nct_id(study)
        if nid in self._seen:  # e.g. the same study returned on two pages
            return
        self._seen.add(nid)
        self.nct_ids.append(nid)
        self.estimated += value.estimated
        if len(self.citations) < max_citations:
            self.citations.append(make_citation(study, value))


@dataclass
class GroupResult:
    dimension: Dimension
    buckets: dict[str | float, Bucket]
    total_studies: int
    missing: int  # studies that produced no value for this dimension

    def nct_ids(self) -> set[str]:
        return {nid for b in self.buckets.values() for nid in b.nct_ids}


def extract_batch(studies: list[dict], dim_name: DimensionName) -> list[list[Extracted]]:
    """Extract a dimension for every study, resolving tentative values against the whole batch.

    A tentative value (e.g. the cleaned name 'morning pembrolizumab infusions' of an OTHER-type
    intervention) is replaced by every confirmed key that appears in it as whole words
    ('pembrolizumab'); if none does, it is dropped.
    """
    dim = DIMENSIONS[dim_name]
    extracted = [dim.extract(s) for s in studies]
    if not any(v.tentative for values in extracted for v in values):
        return extracted

    confirmed: dict[str | float, str] = {}
    for values in extracted:
        for v in values:
            if not v.tentative:
                confirmed.setdefault(v.key, v.label)
    max_words = max((len(str(k).split()) for k in confirmed), default=0)

    def matches_in(text: str) -> list[tuple[str | float, str]]:
        words = text.split()
        found = []
        for size in range(min(max_words, len(words)), 0, -1):
            for start in range(len(words) - size + 1):
                phrase = " ".join(words[start : start + size])
                if phrase in confirmed:
                    found.append((phrase, confirmed[phrase]))
        return found

    resolved = []
    for values in extracted:
        out, seen = [], set()
        for v in values:
            matches = [(v.key, v.label)] if not v.tentative else matches_in(str(v.key))
            for key, label in matches:
                if key not in seen:
                    seen.add(key)
                    out.append(replace(v, key=key, label=label, tentative=False))
        resolved.append(out)
    return resolved


def group_by(studies: list[dict], dim_name: DimensionName, max_citations: int = MAX_CITATIONS) -> GroupResult:
    dim = DIMENSIONS[dim_name]
    buckets: dict[str | float, Bucket] = {}
    missing = 0
    for study, values in zip(studies, extract_batch(studies, dim_name), strict=True):
        if not values:
            missing += 1
        for v in values:
            bucket = buckets.get(v.key)
            if bucket is None:
                bucket = buckets[v.key] = Bucket(v.key, v.label)
            bucket.add(study, v, max_citations)
    return GroupResult(dim, buckets, len(studies), missing)


@dataclass
class Edge:
    source: str  # node id
    target: str
    nct_ids: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)

    @property
    def weight(self) -> int:
        return len(self.nct_ids)


@dataclass
class Node:
    id: str
    label: str
    dimension: DimensionName
    nct_ids: set[str] = field(default_factory=set)
    citations: list[Citation] = field(default_factory=list)


@dataclass
class CoOccurrence:
    nodes: dict[str, Node]
    edges: dict[tuple[str, str], Edge]
    studies_with_edges: int


def node_id(dim: DimensionName, key: str | float) -> str:
    return f"{dim.value}:{key}"


def co_occurrence(
    studies: list[dict], source: DimensionName, target: DimensionName, max_citations: int = MAX_CITATIONS
) -> CoOccurrence:
    """Undirected edges between values that appear in the same trial; weight = number of distinct trials.

    source == target gives a co-occurrence network (e.g. drugs combined in one trial).
    Unknown values are not nodes.
    """
    sources = extract_batch(studies, source)
    targets = sources if target == source else extract_batch(studies, target)
    nodes: dict[str, Node] = {}
    edges: dict[tuple[str, str], Edge] = {}
    with_edges = 0

    def node(dim: DimensionName, v: Extracted, study: dict) -> str:
        nid = node_id(dim, v.key)
        n = nodes.get(nid) or nodes.setdefault(nid, Node(nid, v.label, dim))
        if nct_id(study) not in n.nct_ids:
            n.nct_ids.add(nct_id(study))
            if len(n.citations) < max_citations:
                n.citations.append(make_citation(study, v))
        return nid

    for study, src_vals, tgt_vals in zip(studies, sources, targets, strict=True):
        src_vals = [v for v in src_vals if v.key != UNKNOWN]
        tgt_vals = [v for v in tgt_vals if v.key != UNKNOWN]
        if source == target:
            pairs = [(a, b) for i, a in enumerate(src_vals) for b in src_vals[i + 1 :]]
        else:
            pairs = [(a, b) for a in src_vals for b in tgt_vals]
        if not pairs:
            continue
        with_edges += 1
        nid = nct_id(study)
        for a, b in pairs:
            ida, idb = node(source, a, study), node(target, b, study)
            key = (ida, idb) if source != target else tuple(sorted((ida, idb)))
            edge = edges.get(key) or edges.setdefault(key, Edge(*key))
            if nid in edge.nct_ids:
                continue
            edge.nct_ids.append(nid)
            if len(edge.citations) < max_citations:
                edge.citations.append(
                    Citation(
                        nct_id=nid,
                        field=f"{a.field_path} | {b.field_path}",
                        excerpt=f"{a.excerpt} | {b.excerpt}",
                        title=brief_title(study),
                        url=STUDY_URL.format(nid),
                    )
                )
    return CoOccurrence(nodes, edges, with_edges)


def partition(studies: list[dict], dim_name: DimensionName) -> list[tuple[str, list[dict]]]:
    """Split studies by a dimension's values (for split_by). Multi-valued studies appear in several parts."""
    parts: dict[str | float, tuple[str, list[dict]]] = {}
    for study, values in zip(studies, extract_batch(studies, dim_name), strict=True):
        for v in values:
            parts.setdefault(v.key, (v.label, []))[1].append(study)
    return list(parts.values())
