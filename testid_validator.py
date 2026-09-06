"""
ASES - TestID Validator (v2.7)
================================
Validates that generated code uses the EXACT data-testid values specified
in the design spec. Prevents interaction test failures due to naming mismatches.

Problem in v2.6:
    Design spec says: data_testid: "modal"
    Coder generates:  data-testid="modalTrigger" or data-testid="open-modal"
    Interaction reviewer expects: [data-testid='modal-trigger']
    Result: Test fails with "element not found" — cryptic error for the coder.

Solution:
    1. Extract expected testids from design spec components
    2. Parse generated JSX/TSX/Vue files for data-testid attributes
    3. Validate exact matches and suggest corrections
    4. Run BEFORE interaction reviewer to fail fast

Integration: static_reviewer.py Layer 4 (design compliance) or as a
pre-interaction gate in agent_loop.py.
"""

import re
from typing import Dict, Any, List, Optional, Set, Tuple
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()


@dataclass
class TestIDValidation:
    expected: str
    found: Optional[str]
    component: str
    file: str
    line: int
    severity: str  # "error" | "warning" | "info"
    suggestion: str


def extract_expected_testids(design_spec: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """
    Extract expected testids from design spec components.
    Returns: {component_name: {"root": "...", "trigger": "...", "content": "..."}}
    """
    expected = {}
    for component in design_spec.get("components", []):
        name = component["name"]
        base_testid = component.get("data_testid", name.lower().replace(" ", "-"))

        expected[name] = {
            "root": base_testid,
            "trigger": f"{base_testid}-trigger",
            "content": f"{base_testid}-content",
            "overlay": f"{base_testid}-overlay",
            "options": f"{base_testid}-options",
            "menu": f"{base_testid}-menu",
        }

    return expected


def extract_found_testids(files: List[Dict[str, str]]) -> Dict[str, List[Tuple[str, int]]]:
    """
    Extract all data-testid values found in generated code.
    Returns: {testid_value: [(file_path, line_number), ...]}
    """
    found = {}
    testid_pattern = re.compile(r'data-testid=["\']([^"\']+)["\']')

    for f in files:
        path = f["path"]
        if not any(path.endswith(ext) for ext in [".jsx", ".tsx", ".vue", ".svelte", ".js", ".ts"]):
            continue

        for i, line in enumerate(f["content"].splitlines(), 1):
            matches = testid_pattern.findall(line)
            for match in matches:
                if match not in found:
                    found[match] = []
                found[match].append((path, i))

    return found


def validate_testids(
    design_spec: Dict[str, Any],
    files: List[Dict[str, str]],
    execution_id: str,
) -> Dict[str, Any]:
    """
    Validate that generated testids match design spec expectations.

    Returns:
    {
        "valid": bool,
        "validations": [TestIDValidation, ...],
        "missing": [{"component": str, "expected": str, "suggestion": str}, ...],
        "extra": [{"testid": str, "locations": [(file, line), ...]}, ...],
        "suggestions": str,  # Formatted error message for coder
    }
    """
    if not design_spec or not design_spec.get("has_design"):
        return {"valid": True, "validations": [], "missing": [], "extra": [], "suggestions": ""}

    expected = extract_expected_testids(design_spec["spec"])
    found = extract_found_testids(files)

    validations = []
    missing = []
    used_expected = set()

    # Check for missing expected testids
    for comp_name, testids in expected.items():
        for role, expected_id in testids.items():
            if expected_id in found:
                used_expected.add(expected_id)
                validations.append(TestIDValidation(
                    expected=expected_id,
                    found=expected_id,
                    component=comp_name,
                    file=found[expected_id][0][0],
                    line=found[expected_id][0][1],
                    severity="info",
                    suggestion="",
                ))
            else:
                # Check for fuzzy matches (e.g., "modal-trigger" vs "modalTrigger")
                fuzzy_match = None
                for found_id in found:
                    if _fuzzy_match(expected_id, found_id):
                        fuzzy_match = found_id
                        break

                if fuzzy_match:
                    used_expected.add(expected_id)
                    validations.append(TestIDValidation(
                        expected=expected_id,
                        found=fuzzy_match,
                        component=comp_name,
                        file=found[fuzzy_match][0][0],
                        line=found[fuzzy_match][0][1],
                        severity="warning",
                        suggestion=f"Change data-testid=\"{fuzzy_match}\" to data-testid=\"{expected_id}\"",
                    ))
                else:
                    missing.append({
                        "component": comp_name,
                        "role": role,
                        "expected": expected_id,
                        "suggestion": f"Add data-testid=\"{expected_id}\" to the {role} element of {comp_name}",
                    })

    # Check for extra (unexpected) testids
    extra = []
    for found_id, locations in found.items():
        if found_id not in used_expected:
            extra.append({
                "testid": found_id,
                "locations": locations,
            })

    # Build suggestions for coder
    suggestions = []
    if missing:
        suggestions.append("MISSING data-testid ATTRIBUTES (required for interaction tests):")
        for m in missing:
            suggestions.append(f"  [{m['component']}/{m['role']}] Add: data-testid=\"{m['expected']}\"")

    if [v for v in validations if v.severity == "warning"]:
        suggestions.append("\\nINCORRECT data-testid VALUES (must match design spec exactly):")
        for v in validations:
            if v.severity == "warning":
                suggestions.append(f"  {v.file}:{v.line} — {v.suggestion}")

    valid = len(missing) == 0 and len([v for v in validations if v.severity == "error"]) == 0

    logger.info(
        "testid_validator.complete",
        execution_id=execution_id,
        valid=valid,
        missing=len(missing),
        warnings=len([v for v in validations if v.severity == "warning"]),
        extra=len(extra),
    )

    return {
        "valid": valid,
        "validations": [v.__dict__ for v in validations],
        "missing": missing,
        "extra": extra,
        "suggestions": "\\n".join(suggestions) if suggestions else "",
    }


def _fuzzy_match(expected: str, found: str) -> bool:
    """Check if two testid strings are likely the same with different casing/formatting."""
    # Normalize: lowercase, remove hyphens/underscores
    norm_expected = expected.lower().replace("-", "").replace("_", "")
    norm_found = found.lower().replace("-", "").replace("_", "")

    # Exact match after normalization
    if norm_expected == norm_found:
        return True

    # One is substring of other
    if norm_expected in norm_found or norm_found in norm_expected:
        return True

    # Levenshtein distance <= 2 for short strings, <= 3 for longer
    max_dist = 2 if len(norm_expected) < 10 else 3
    return _levenshtein(norm_expected, norm_found) <= max_dist


def _levenshtein(s1: str, s2: str) -> int:
    """Calculate Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]
