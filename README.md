# forge-cad-eval

Deterministic evaluation harness for generative parametric CAD. It grades a
model the way a design review does: five hard gates, in order, no averaging.

## Why

Generative CAD demos produce plausible geometry. Production needs a verdict
that is reproducible from an artifact, not a screenshot. This repo is that
verdict layer.

## Rungs

1. **Topology** - closed manifold B-Rep, `V - E + F = 2*(S - G)`, zero open shells, zero self-intersecting faces.
2. **Datum** - mounting interface bounding box and coaxiality inside tolerance.
3. **DFM** - thin walls, tool access, hole aspect ratio `L/D <= 8.0`.
4. **Assembly** - mate graph must remove exactly the required DOF; redundant mate cycles fail.
5. **Structure** - linear static FEA von Mises safety factor `SF >= 1.5`.

A failed rung stops the ladder. A part that leaks is not a part with a stress
report; it is a failed part.

## Usage

```
python harness/eval_grader.py path/to/sidecar.json
python extractors/feature_dag_parser.py features.json sidecar.json
```

Pure Python: no CAD kernel, no network, no clock. Same sidecar in, same verdict
out, on any machine.

## Layout

- `harness/eval_grader.py` - five-rung gate and scorecard emitter
- `extractors/feature_dag_parser.py` - ordered feature DAG, SHA-256 hash chain
- `schemas/agentic_sidecar_v1.json` - `forge.agentic/1` contract


## Expanded project documentation

The `docs/` directory contains project-specific design notes, verification scenario specifications, operations guides, data contracts, configuration templates, safety notes, and project reference templates.

**Important:** added documents are planning and review aids. They do not claim that tests have been executed, that placeholder configurations are supported runtime profiles, or that this project is certified for physical/clinical/production deployment. Confirm interfaces and limits against implementation and measured evidence.
