"""
ASES - Design Regenerator (v2.7)
==================================
Regenerates design specifications when visual or interaction review fails,
incorporating failure feedback to produce a corrected spec.

Problem in v2.6:
    When visual review fails ("modal is clipped by parent"), the error goes
    to the coder, not the designer. The design spec remains unchanged across
    iterations, meaning the coder is trying to fix a design-level problem
    with code-level changes.

Solution:
    1. Detect when a failure is design-level (not code-level)
    2. Feed failure context back to design_agent with "regenerate" mode
    3. Design agent produces a corrected spec (e.g., higher z-index, different layout)
    4. Coder receives updated spec on next iteration

Integration: agent_loop.py — called when visual/interaction fails and
journal indicates this is a recurring design issue.
"""

import json
from typing import Dict, Any, List, Optional

import structlog

from agent_loop import call_model

logger = structlog.get_logger()


async def regenerate_design_spec(
    original_spec: Dict[str, Any],
    failure_context: Dict[str, Any],
    task: str,
    tech_stack: str,
    requirements: str,
    config,
    execution_id: str,
) -> Dict[str, Any]:
    """
    Regenerate a design spec incorporating failure feedback.

    Args:
        original_spec: The design spec that failed
        failure_context: {
            "type": "visual" | "interaction",
            "issues": [{"severity": str, "description": str, "component": str}],
            "iteration": int,
            "previous_attempts": int,
        }

    Returns:
        New design_result dict (same format as design_agent output)
    """

    system_prompt = """You are a senior product designer fixing a design specification that failed review.

You will receive:
1. The original design spec (JSON)
2. A list of failures from visual review or interaction testing
3. The number of previous attempts

RULES:
1. Output ONLY valid JSON matching the original schema.
2. Fix the SPECIFIC issues mentioned — do not change unrelated parts.
3. If z-index is too low, increase it explicitly.
4. If a component is clipped, add overflow or positioning rules.
5. If interaction tests fail, add missing states or clarify interaction_rules.
6. If this is the 2nd+ regeneration, make more aggressive changes.
7. Preserve the data_testid values — they must remain stable.

Output format: Same as design_agent.py DESIGN_SCHEMA_DOC."""

    user_prompt = f"""Task: {task}
Tech Stack: {tech_stack}
Requirements: {requirements or "(none)"}

Original Design Spec:
{json.dumps(original_spec, indent=2)}

Failure Context:
Type: {failure_context['type']}
Iteration: {failure_context['iteration']}
Previous Attempts: {failure_context.get('previous_attempts', 0)}

Issues:
{chr(10).join(f"  - [{i.get('severity', '?')}] {i.get('description', '')} (component: {i.get('component', 'unknown')})" for i in failure_context.get('issues', []))}

Generate a CORRECTED design specification that addresses these failures."""

    content, inp_tok, out_tok = await call_model(
        model=config.reviewer_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,  # Slightly higher for creative fixes
        max_tokens=2500,
        execution_id=execution_id,
        call_type="reviewer",
    )

    spec = _parse_design_json(content)

    if not spec:
        logger.warning("design_regenerator.parse_failed", execution_id=execution_id)
        return {
            "has_design": True,  # Keep original
            "spec": original_spec,
            "css_variables": _generate_css_variables(original_spec),
            "issues": ["Design regeneration failed — keeping original spec"],
            "tokens": inp_tok + out_tok,
            "from_cache": False,
            "regenerated": False,
        }

    # Preserve data_testid values from original to maintain interaction test compatibility
    original_testids = _extract_testids(original_spec)
    new_testids = _extract_testids(spec)

    # Merge: use original testids where possible, keep new ones for new components
    for comp_name, testids in original_testids.items():
        for new_comp in spec.get("components", []):
            if new_comp["name"] == comp_name and "data_testid" not in new_comp:
                new_comp["data_testid"] = testids.get("root", comp_name.lower().replace(" ", "-"))

    css_vars = _generate_css_variables(spec)

    logger.info(
        "design_regenerator.complete",
        execution_id=execution_id,
        components=len(spec.get("components", [])),
        tokens=inp_tok + out_tok,
    )

    return {
        "has_design": True,
        "spec": spec,
        "css_variables": css_vars,
        "issues": [],
        "tokens": inp_tok + out_tok,
        "from_cache": False,
        "regenerated": True,
    }


def _parse_design_json(content: str) -> Optional[Dict[str, Any]]:
    """Extract JSON from model output."""
    import re
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    match = re.search(r'```json\s*(.*?)```', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    match = re.search(r'(\{.*\})', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    return None


def _generate_css_variables(spec: Dict[str, Any]) -> str:
    """Generate CSS :root block from spec."""
    ds = spec.get("design_system", {})
    colors = ds.get("colors", {})
    typography = ds.get("typography", {})
    radii = ds.get("radii", {})
    breakpoints = spec.get("responsive_breakpoints", {})

    lines = [":root {"]
    for k, v in colors.items():
        lines.append(f"  --color-{k.replace('_', '-')}: {v};")
    if "font_family" in typography:
        lines.append(f"  --font-family: {typography['font_family']};")
    for k, v in typography.get("heading_sizes", {}).items():
        lines.append(f"  --font-size-{k}: {v};")
    if "body_size" in typography:
        lines.append(f"  --font-size-body: {typography['body_size']};")
    if "line_height" in typography:
        lines.append(f"  --line-height: {typography['line_height']};")
    for k, v in radii.items():
        lines.append(f"  --radius-{k}: {v};")
    for k, v in breakpoints.items():
        lines.append(f"  --breakpoint-{k}: {v};")
    lines.append("}")

    return "\n".join(lines)


def _extract_testids(spec: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """Extract testid mapping from spec."""
    testids = {}
    for comp in spec.get("components", []):
        name = comp["name"]
        base = comp.get("data_testid", name.lower().replace(" ", "-"))
        testids[name] = {
            "root": base,
            "trigger": f"{base}-trigger",
            "content": f"{base}-content",
        }
    return testids


def is_design_level_failure(failure: Dict[str, Any]) -> bool:
    """
    Determine if a failure is design-level (needs spec regeneration)
    vs code-level (coder can fix without changing spec).

    Design-level failures:
    - z-index issues
    - Layout clipping/overflow
    - Color contrast failures
    - Missing responsive breakpoints
    - Component state definition errors

    Code-level failures:
    - Syntax errors
    - Missing imports
    - Logic bugs in handlers
    """
    design_keywords = [
        "z-index", "clip", "overflow", "position", "layout",
        "contrast", "color", "typography", "font", "spacing",
        "breakpoint", "responsive", "grid", "flex",
        "state", "animation", "transition",
    ]

    description = failure.get("description", "").lower()
    return any(kw in description for kw in design_keywords)
