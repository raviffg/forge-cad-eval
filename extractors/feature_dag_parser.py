"""Parametric feature-tree DAG extractor.

Turns a raw CAD feature log into an ordered DAG plus a SHA-256 geometry hash
chain, then emits the ``forge.agentic/1`` sidecar consumed by the grader.

Each node records its operation, ordered parameters, and the topological
references it consumed (face and edge ids on parent bodies). The hash chain is
order sensitive on purpose: swapping two independent fillets produces a
different root hash, which makes the sidecar a reproducible artifact rather than
a rendering.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

OPS = frozenset({"Extrude", "Revolve", "Fillet", "Chamfer", "Cut-Extrude",
                 "Hole", "Pattern"})
REF_KINDS = frozenset({"faces", "edges", "vertices", "sketches"})
SCHEMA = "forge.agentic/1"


class FeatureTreeError(ValueError):
    """Raised when the feature log cannot be represented as an ordered DAG."""


@dataclass(frozen=True)
class FeatureNode:
    fid: str
    op: str
    parents: tuple[str, ...] = ()
    refs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    def canonical(self, index: int, previous_hash: str) -> bytes:
        payload = {"index": index, "id": self.fid, "op": self.op,
                   "parents": list(self.parents),
                   "refs": {k: list(v) for k, v in sorted(self.refs.items())},
                   "params": self.params, "prev": previous_hash}
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def parse_features(raw_features: Iterable[dict[str, Any]]) -> list[FeatureNode]:
    """Parse, validate and topologically order a feature log."""
    nodes: list[FeatureNode] = []
    seen: set[str] = set()
    for raw in raw_features:
        fid, op = str(raw.get("id", "")).strip(), str(raw.get("op", "")).strip()
        if not fid or fid in seen:
            raise FeatureTreeError(f"missing or duplicate feature id {fid!r}")
        if op not in OPS:
            raise FeatureTreeError(f"feature {fid!r}: unsupported op {op!r}")
        refs = {}
        for kind, values in raw.get("refs", {}).items():
            if kind not in REF_KINDS:
                raise FeatureTreeError(f"feature {fid!r}: unknown ref kind {kind!r}")
            refs[kind] = tuple(str(v) for v in values)
        seen.add(fid)
        nodes.append(FeatureNode(fid, op, tuple(str(p) for p in raw.get("parents", ())),
                                 refs, dict(raw.get("params", {}))))

    by_id = {n.fid: n for n in nodes}
    indegree = {n.fid: 0 for n in nodes}
    children: dict[str, list[str]] = {n.fid: [] for n in nodes}
    for node in nodes:
        for parent in node.parents:
            if parent not in by_id:
                raise FeatureTreeError(f"feature {node.fid!r}: unknown parent {parent!r}")
            if parent == node.fid:
                raise FeatureTreeError(f"feature {node.fid!r}: self reference")
            indegree[node.fid] += 1
            children[parent].append(node.fid)

    queue = [n.fid for n in nodes if indegree[n.fid] == 0]
    ordered: list[FeatureNode] = []
    while queue:
        fid = queue.pop(0)
        ordered.append(by_id[fid])
        for child in children[fid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(ordered) != len(nodes):
        raise FeatureTreeError(
            f"cyclic feature graph: {sorted(f for f, d in indegree.items() if d)}")
    return ordered


def hash_chain(nodes: list[FeatureNode]) -> tuple[list[str], str]:
    """Return per-feature hashes and the root geometry hash."""
    previous = "0" * 64
    chain: list[str] = []
    for index, node in enumerate(nodes):
        previous = hashlib.sha256(node.canonical(index, previous)).hexdigest()
        chain.append(previous)
    return chain, previous


def build_sidecar(raw_features: Iterable[dict[str, Any]], *,
                  topology: dict[str, Any] | None = None,
                  datums: list[dict[str, Any]] | None = None,
                  dfm: dict[str, Any] | None = None,
                  assembly: dict[str, Any] | None = None,
                  fea: dict[str, Any] | None = None) -> dict[str, Any]:
    nodes = parse_features(raw_features)
    chain, root = hash_chain(nodes)
    return {
        "schema": SCHEMA,
        "geometry_hash": root,
        "feature_dag": {
            "root_hash": root,
            "nodes": [{"index": i, "id": n.fid, "op": n.op,
                       "parents": list(n.parents),
                       "refs": {k: list(v) for k, v in n.refs.items()},
                       "params": n.params, "hash": chain[i]}
                      for i, n in enumerate(nodes)],
        },
        "topology": topology or {},
        "datums": datums or [],
        "dfm": dfm or {},
        "assembly": assembly or {},
        "fea": fea or {},
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: feature_dag_parser.py <features.json> <sidecar.json>",
              file=sys.stderr)
        return 2
    with open(argv[0], "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    sidecar = build_sidecar(payload.get("features", []),
                            **{k: payload.get(k) for k in
                               ("topology", "datums", "dfm", "assembly", "fea")})
    with open(argv[1], "w", encoding="utf-8") as handle:
        json.dump(sidecar, handle, indent=2)
        handle.write("\n")
    print(sidecar["geometry_hash"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
