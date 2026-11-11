"""Generates `frontend2/src/api/mixplan.generated.ts` from the plan's JSON Schema and the transition styles, so the frontend's types can't drift
from what the backend validates (tests/test_seedbox2_mix_plan.py fails when the file is stale). Run after changing
`plan.py` or a style's file:

    .venv/bin/python -m seedbox2.mix.typescript
"""

from __future__ import annotations

import json
from pathlib import Path

from . import plan, styles

OUTPUT = Path(__file__).resolve().parents[2] / "frontend2" / "src" / "api" / "mixplan.generated.ts"
_CONSTANTS = {
    "PLAN_VERSION": plan.PLAN_VERSION, "LANES": plan.LANES, "STEM_LANES": plan.STEM_LANES,
    "LANE_RANGES": plan.LANE_RANGES, "LANE_DEFAULTS": plan.LANE_DEFAULTS, "TEMPO_RANGE": plan.TEMPO_RANGE,
    "KEY_SHIFT_MAX": plan.KEY_SHIFT_MAX, "LOOP_DIVISIONS": plan.LOOP_DIVISIONS,
    "STEM_EQ_FALLBACK": plan.STEM_EQ_FALLBACK,
    # the transition styles (#57, seedbox2/mix/styles/*.toml): one technique list for the planner, renderer, API and UI
    "TECHNIQUES": styles.TECHNIQUES, "TECHNIQUE_KINDS": {t: styles.kind_of(t) for t in styles.TECHNIQUES},
    "TECHNIQUE_LABELS": {s.technique: s.label for s in styles.all_styles()},
}  # fmt: skip


def _type(node: dict) -> str:
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    if "const" in node:
        return json.dumps(node["const"])
    if "enum" in node:
        return " | ".join(json.dumps(v) for v in node["enum"])
    for key in ("anyOf", "oneOf"):
        if key in node:
            return " | ".join(_type(n) for n in node[key])
    kind = node.get("type")
    if kind == "array":
        item = _type(node["items"])
        return f"({item})[]" if " | " in item else f"{item}[]"
    simple = {"string": "string", "number": "number", "integer": "number", "boolean": "boolean", "null": "null"}
    if kind in simple:
        return simple[kind]
    raise ValueError(f"no TypeScript for this schema: {node}")


def _doc(text: str | None, indent: str = "") -> list[str]:
    if not text:
        return []
    lines = [" ".join(p.split()) for p in text.split("\n\n")]
    if len(lines) == 1:
        return [f"{indent}/** {lines[0]} */"]
    return [f"{indent}/**", *(f"{indent} * {x}" for x in lines), f"{indent} */"]


def _interface(name: str, node: dict) -> list[str]:
    required = set(node.get("required", ()))
    out = [*_doc(node.get("description")), f"export interface {name} {{"]
    for prop, sub in node["properties"].items():
        out += [*_doc(sub.get("description"), "  "), f"  {prop}{'' if prop in required else '?'}: {_type(sub)};"]
    return [*out, "}", ""]


def render() -> str:
    """The file's contents. Types of a validated plan as the API sends it, with every default filled in."""
    schema = plan.MixPlan.model_json_schema(mode="serialization")
    out = [
        "// Generated from seedbox2/mix/plan.py by `python -m seedbox2.mix.typescript`. Don't edit it by hand.",
        "",
    ]
    for name, value in _CONSTANTS.items():
        out += [f"export const {name} = {json.dumps(value)} as const;"]
    out.append("")
    for name, node in sorted(schema["$defs"].items()):
        out += _interface(name, node)
    return "\n".join([*out, *_interface(schema["title"], schema)])


if __name__ == "__main__":
    OUTPUT.write_text(render(), encoding="utf-8")
    print(OUTPUT)
