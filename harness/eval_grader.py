"""Deterministic five-rung evaluation grader for generative parametric CAD.

Reads a ``forge.agentic/1`` sidecar and returns a gated scorecard. Every rung is
a hard gate: the first failure stops the ladder, because a part with a
non-manifold shell should never reach a stress report.

1 topology  closed manifold B-Rep, V - E + F = 2*(S - G)
2 datum     mounting interface bbox and coaxiality inside tolerance
3 dfm       thin wall, tool access, hole aspect ratio L/D <= 8.0
4 assembly  mate graph removes exactly the required DOF
5 structure linear static FEA von Mises safety factor >= 1.5

Pure Python: no CAD kernel, no network, no clock.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

SCHEMA = "forge.agentic/1"
MIN_SAFETY_FACTOR = 1.5
MAX_HOLE_ASPECT = 8.0
COAXIALITY_TOL_MM = 0.05
BBOX_TOL_MM = 0.10


@dataclass(frozen=True)
class RungResult:
    rung: int
    name: str
    passed: bool
    metrics: dict[str, float] = field(default_factory=dict)
    failures: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"rung": self.rung, "name": self.name, "passed": self.passed,
                "metrics": {k: round(float(v), 6) for k, v in self.metrics.items()},
                "failures": list(self.failures)}


@dataclass(frozen=True)
class Scorecard:
    schema: str
    verdict: str
    blocked_at: int | None
    rungs: list[RungResult]

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "verdict": self.verdict,
                "blocked_at": self.blocked_at,
                "rungs": [r.as_dict() for r in self.rungs]}


def _norm(v: Sequence[float]) -> float:
    return math.sqrt(sum(c * c for c in v))


def _axis_offset(point: Sequence[float], origin: Sequence[float],
                 direction: Sequence[float]) -> float:
    """Perpendicular distance from ``point`` to the line (origin, direction)."""
    length = _norm(direction) or 1.0
    unit = [c / length for c in direction]
    delta = [p - o for p, o in zip(point, origin)]
    along = sum(d * u for d, u in zip(delta, unit))
    return _norm([d - along * u for d, u in zip(delta, unit)])


def rung_topology(sc: dict[str, Any]) -> RungResult:
    topo = sc.get("topology", {})
    v, e, f = (int(topo.get(k, 0)) for k in ("vertices", "edges", "faces"))
    shells, genus = int(topo.get("shells", 0)), int(topo.get("genus", 0))
    open_shells = int(topo.get("open_shells", 0))
    self_int = int(topo.get("self_intersecting_faces", 0))
    euler, expected = v - e + f, 2 * (shells - genus)
    bad: list[str] = []
    if shells <= 0:
        bad.append("no closed shell: body is not a solid")
    if euler != expected:
        bad.append(f"Euler-Poincare imbalance: V-E+F={euler} != 2*(S-G)={expected}")
    if open_shells:
        bad.append(f"{open_shells} open shell(s): not watertight")
    if self_int:
        bad.append(f"{self_int} self-intersecting face(s)")
    return RungResult(1, "manifold_brep_topology", not bad,
                      {"euler": float(euler), "expected_euler": float(expected),
                       "shells": float(shells), "genus": float(genus)}, tuple(bad))


def rung_datums(sc: dict[str, Any]) -> RungResult:
    datums, bad = sc.get("datums", []), []
    if not datums:
        bad.append("no datum or mounting interface declared")
    worst_bbox = worst_coax = 0.0
    for datum in datums:
        name = datum.get("name", "unnamed")
        nominal = datum.get("nominal_bbox", [0.0, 0.0, 0.0])
        measured = datum.get("measured_bbox", nominal)
        delta = max((abs(m - n) for m, n in zip(measured, nominal)), default=0.0)
        worst_bbox = max(worst_bbox, delta)
        if delta > BBOX_TOL_MM:
            bad.append(f"{name}: bbox deviation {delta:.3f} > {BBOX_TOL_MM:.2f} mm")
        axis, origin = datum.get("nominal_axis"), datum.get("measured_axis_origin")
        if axis and origin:
            off = _axis_offset(origin, axis["origin"], axis["direction"])
            worst_coax = max(worst_coax, off)
            if off > COAXIALITY_TOL_MM:
                bad.append(f"{name}: coaxiality {off:.3f} > {COAXIALITY_TOL_MM:.2f} mm")
    return RungResult(2, "datum_interface", not bad,
                      {"datums": float(len(datums)), "worst_bbox_mm": worst_bbox,
                       "worst_coaxiality_mm": worst_coax}, tuple(bad))


def rung_dfm(sc: dict[str, Any]) -> RungResult:
    dfm, bad = sc.get("dfm", {}), []
    min_wall = float(dfm.get("min_wall_mm", 0.8))
    worst_ld, worst_wall = 0.0, math.inf
    for hole in dfm.get("holes", []):
        diameter = float(hole.get("diameter_mm", 0.0))
        ratio = float(hole.get("depth_mm", 0.0)) / diameter if diameter > 0 else math.inf
        worst_ld = max(worst_ld, ratio) if math.isfinite(ratio) else worst_ld
        if not math.isfinite(ratio):
            bad.append(f"hole {hole.get('id', '?')}: non-positive diameter")
        elif ratio > MAX_HOLE_ASPECT:
            bad.append(f"hole {hole.get('id', '?')}: L/D {ratio:.2f} > "
                       f"{MAX_HOLE_ASPECT:.1f} (drill deflection risk)")
    for wall in dfm.get("walls", []):
        thickness = float(wall.get("thickness_mm", min_wall))
        worst_wall = min(worst_wall, thickness)
        if thickness < min_wall:
            bad.append(f"wall {wall.get('id', '?')}: {thickness:.3f} < {min_wall:.2f} mm")
    if dfm.get("inaccessible_faces", 0):
        bad.append(f"{dfm['inaccessible_faces']} face(s) unreachable by tool approach")
    return RungResult(3, "dfm_machinability", not bad,
                      {"holes": float(len(dfm.get("holes", []))), "max_hole_aspect": worst_ld,
                       "min_wall_mm": 0.0 if math.isinf(worst_wall) else worst_wall}, tuple(bad))


def rung_assembly(sc: dict[str, Any]) -> RungResult:
    asm = sc.get("assembly", {})
    bodies = [str(b) for b in asm.get("bodies", [])]
    mates, bad = asm.get("mates", []), []
    required = 6 * max(len(bodies) - 1, 0)  # first body is grounded
    parent = {b: b for b in bodies}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    removed = redundant = 0
    for mate in mates:
        a, b = (str(x) for x in mate.get("bodies", ("", ""))[:2])
        if a not in parent or b not in parent:
            bad.append(f"mate {mate.get('id', '?')}: unknown body reference")
            continue
        if find(a) == find(b):
            redundant += 1
            bad.append(f"mate {mate.get('id', '?')}: redundant cycle {a} <-> {b}")
            continue
        parent[find(a)] = find(b)
        removed += int(mate.get("dof_removed", 0))

    residual = required - removed
    if residual > 0:
        bad.append(f"under-constrained: {residual} DOF left free")
    elif residual < 0:
        bad.append(f"over-constrained: {-residual} DOF removed in excess")
    return RungResult(4, "assembly_dof_lock", not bad,
                      {"bodies": float(len(bodies)), "mates": float(len(mates)),
                       "dof_required": float(required), "dof_removed": float(removed),
                       "redundant_mates": float(redundant)}, tuple(bad))


def rung_structure(sc: dict[str, Any]) -> RungResult:
    fea, bad = sc.get("fea", {}), []
    peak = float(fea.get("max_von_mises_mpa", 0.0))
    yield_strength = float(fea.get("yield_strength_mpa", 0.0))
    if not fea.get("converged", False):
        bad.append("solver did not converge: stress field is not admissible")
    if peak <= 0.0:
        bad.append("no peak von Mises stress reported")
    if yield_strength <= 0.0:
        bad.append("no material yield strength reported")
    sf = yield_strength / peak if peak > 0.0 else 0.0
    if peak > 0.0 and sf < MIN_SAFETY_FACTOR:
        bad.append(f"safety factor {sf:.2f} < {MIN_SAFETY_FACTOR:.1f} "
                   f"(peak {peak:.1f} MPa, yield {yield_strength:.1f} MPa)")
    return RungResult(5, "structural_safety_factor", not bad,
                      {"max_von_mises_mpa": peak, "yield_strength_mpa": yield_strength,
                       "safety_factor": sf}, tuple(bad))


RUNGS: tuple[Callable[[dict[str, Any]], RungResult], ...] = (
    rung_topology, rung_datums, rung_dfm, rung_assembly, rung_structure,
)


def grade(sidecar: dict[str, Any]) -> Scorecard:
    if sidecar.get("schema") != SCHEMA:
        raise ValueError(f"expected schema {SCHEMA!r}, got {sidecar.get('schema')!r}")
    results: list[RungResult] = []
    for rung in RUNGS:
        results.append(rung(sidecar))
        if not results[-1].passed:
            break
    blocked = results[-1].rung if results and not results[-1].passed else None
    return Scorecard(SCHEMA, "pass" if blocked is None else "fail", blocked, results)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: eval_grader.py <sidecar.json>", file=sys.stderr)
        return 2
    with open(argv[0], "r", encoding="utf-8") as handle:
        card = grade(json.load(handle))
    print(json.dumps(card.as_dict(), indent=2))
    return 0 if card.verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
