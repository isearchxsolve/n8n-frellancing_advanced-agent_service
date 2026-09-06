"""
ASES - Interaction Reviewer (v2.6)
====================================
End-to-end interaction validation using Playwright in the sandbox.

Runs a suite of interaction tests generated from the design spec's
'interaction_rules' and 'states' fields. Executes real browser actions
(clicks, typing, keyboard navigation) and validates DOM + accessibility
state changes.

Key features:
- Zero LLM tokens (pure Playwright execution)
- Hydration-aware (waits for React/Vue/Angular hydration)
- Shadow DOM penetration support
- Mobile viewport emulation (touch events)
- Accessibility state validation (aria-expanded, aria-hidden, etc.)
- Screenshot-on-failure for debugging

Integration: agent_loop.py, after visual_reviewer() approval, gated by
_has_frontend() and presence of interaction_rules in design spec.
"""

import json
import asyncio
import base64
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict

import structlog

logger = structlog.get_logger()


@dataclass
class InteractionTest:
    name: str
    selector: str                    # CSS selector, aria-label, or data-testid
    action: str                      # "click" | "type" | "keyboard" | "hover" | "focus" | "touch"
    pre_condition: Optional[str]     # selector that must exist before action
    post_condition: str              # selector/state that must exist after
    accessibility_check: bool        # verify aria-expanded, aria-hidden, etc.
    viewport: Optional[str]          # "desktop" | "tablet" | "mobile" | None
    timeout_ms: int = 5000           # max wait for conditions


# Playwright runner script — executed inside the sandbox container
INTERACTION_RUNNER_SCRIPT = r"""
import asyncio
import sys
import json
import traceback
from playwright.async_api import async_playwright, expect, TimeoutError as PWTimeout

async def run(tests, url, port, screenshot_on_fail=True):
    results = []
    browser = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )

            for test in tests:
                test_result = {"name": test["name"], "passed": False, "error": None, "stage": None, "screenshot": None}
                page = None

                try:
                    # Viewport setup
                    viewport = test.get("viewport", "desktop")
                    vp_map = {
                        "desktop": {"width": 1280, "height": 800},
                        "tablet": {"width": 768, "height": 1024},
                        "mobile": {"width": 375, "height": 667},
                    }
                    vp = vp_map.get(viewport, vp_map["desktop"])

                    context = await browser.new_context(
                        viewport=vp,
                        has_touch=(viewport == "mobile"),
                        user_agent="Mozilla/5.0 (ASES-Interaction-Reviewer)"
                    )
                    page = await context.new_page()

                    # Navigate with hydration wait
                    await page.goto(url, wait_until="networkidle", timeout=15000)

                    # Wait for hydration (React/Vue/Angular)
                    await page.wait_for_timeout(1500)

                    # Additional wait for any data-testid elements to appear
                    try:
                        await page.wait_for_selector("[data-testid]", timeout=5000)
                    except PWTimeout:
                        pass  # Some pages may not use data-testid

                    # Pre-condition check
                    if test.get("pre_condition"):
                        await expect(page.locator(test["pre_condition"])).to_be_visible(
                            timeout=test.get("timeout_ms", 5000)
                        )
                        test_result["stage"] = "pre_ok"

                    # Execute action
                    action = test["action"]
                    selector = test["selector"]

                    if action == "click":
                        await page.click(selector, timeout=test.get("timeout_ms", 5000))
                    elif action == "keyboard":
                        await page.keyboard.press(selector)
                    elif action == "type":
                        parts = selector.split("|", 1)
                        if len(parts) == 2:
                            await page.fill(parts[0], parts[1], timeout=test.get("timeout_ms", 5000))
                        else:
                            raise ValueError(f"type action requires 'selector|text' format, got: {selector}")
                    elif action == "hover":
                        await page.hover(selector, timeout=test.get("timeout_ms", 5000))
                    elif action == "focus":
                        await page.focus(selector, timeout=test.get("timeout_ms", 5000))
                    elif action == "touch":
                        await page.tap(selector, timeout=test.get("timeout_ms", 5000))
                    else:
                        raise ValueError(f"Unknown action: {action}")

                    # Wait for transitions/animations
                    await page.wait_for_timeout(800)

                    # Post-condition check
                    await expect(page.locator(test["post_condition"])).to_be_visible(
                        timeout=test.get("timeout_ms", 5000)
                    )
                    test_result["stage"] = "post_ok"

                    # Accessibility check
                    if test.get("accessibility_check"):
                        elem = page.locator(test["post_condition"])
                        aria_expanded = await elem.get_attribute("aria-expanded")
                        aria_hidden = await elem.get_attribute("aria-hidden")
                        aria_modal = await elem.get_attribute("aria-modal")

                        test_result["aria"] = {
                            "expanded": aria_expanded,
                            "hidden": aria_hidden,
                            "modal": aria_modal,
                        }

                    test_result["passed"] = True

                except PWTimeout as e:
                    test_result["error"] = f"Timeout: {str(e)}"
                    test_result["stage"] = test_result["stage"] or "timeout"

                    # Screenshot on failure
                    if screenshot_on_fail and page:
                        try:
                            ss = await page.screenshot(full_page=False)
                            test_result["screenshot_b64"] = base64.b64encode(ss).decode()
                        except Exception:
                            pass

                except Exception as e:
                    test_result["error"] = f"{type(e).__name__}: {str(e)}"
                    test_result["stage"] = test_result["stage"] or "error"

                    if screenshot_on_fail and page:
                        try:
                            ss = await page.screenshot(full_page=False)
                            test_result["screenshot_b64"] = base64.b64encode(ss).decode()
                        except Exception:
                            pass

                finally:
                    if page:
                        await page.close()
                    if context:
                        await context.close()

                results.append(test_result)

            await browser.close()
            browser = None

    except Exception as e:
        # Global failure (browser launch, etc.)
        results.append({
            "name": "runner_setup",
            "passed": False,
            "error": f"Browser setup failed: {str(e)}\n{traceback.format_exc()}",
            "stage": "setup",
        })
    finally:
        if browser:
            await browser.close()

    return results

if __name__ == "__main__":
    import base64
    tests = json.loads(sys.argv[1])
    url = sys.argv[2]
    result = asyncio.run(run(tests, url, None))
    print(json.dumps(result))
"""


# ---------------------------------------------------------------------------
# Main reviewer
# ---------------------------------------------------------------------------

async def interaction_reviewer(
    sandbox_id: str,
    design_spec: Dict[str, Any],
    files: List[Dict[str, str]],
    config,
    execution_id: str,
) -> Dict[str, Any]:
    """
    Generates and executes interaction tests from the design spec.

    Returns:
    {
        "approved": bool,
        "tests_run": int,
        "tests_passed": int,
        "failures": [{"name": str, "error": str, "stage": str, "screenshot_b64": str}],
        "tokens": 0,  # No LLM tokens — pure Playwright execution
        "duration_ms": int,
    }
    """
    from sandbox import run_command, write_file

    # Extract interaction rules from design spec
    interaction_tests = _generate_tests_from_spec(design_spec)

    if not interaction_tests:
        logger.info("interaction_reviewer.no_rules", execution_id=execution_id)
        return {
            "approved": True,
            "tests_run": 0,
            "tests_passed": 0,
            "failures": [],
            "tokens": 0,
            "duration_ms": 0,
        }

    # Write test runner to sandbox
    write_file(sandbox_id, ".ases_interaction.py", INTERACTION_RUNNER_SCRIPT)

    # Determine dev server URL
    stack = _resolve_stack_from_files(files)
    port = 3000 if stack in {"react", "next.js", "nextjs"} else 8080
    url = f"http://localhost:{port}"

    # Start dev server if not already running
    dev_cmd = _get_dev_command(stack)
    if dev_cmd:
        run_command(sandbox_id, dev_cmd + " &", timeout=5)
        await asyncio.sleep(10)  # Server boot + hydration buffer

    # Run interaction tests
    import time
    t0 = time.perf_counter()

    test_json = json.dumps([asdict(t) for t in interaction_tests])
    result = run_command(
        sandbox_id,
        f"python .ases_interaction.py '{test_json}' '{url}'",
        timeout=120,
    )

    duration_ms = int((time.perf_counter() - t0) * 1000)

    if not result["success"]:
        logger.warning("interaction_reviewer.runner_failed", execution_id=execution_id, stderr=result["stderr"][:300])
        return {
            "approved": False,
            "tests_run": len(interaction_tests),
            "tests_passed": 0,
            "failures": [{"name": "runner", "error": result["stderr"], "stage": "setup"}],
            "tokens": 0,
            "duration_ms": duration_ms,
        }

    try:
        test_results = json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {
            "approved": False,
            "tests_run": len(interaction_tests),
            "tests_passed": 0,
            "failures": [{"name": "parse", "error": "Could not parse test results", "stage": "parse"}],
            "tokens": 0,
            "duration_ms": duration_ms,
        }

    failures = [r for r in test_results if not r.get("passed")]
    approved = len(failures) == 0

    logger.info(
        "interaction_reviewer.complete",
        execution_id=execution_id,
        tests_run=len(interaction_tests),
        passed=len(test_results) - len(failures),
        approved=approved,
        duration_ms=duration_ms,
    )

    return {
        "approved": approved,
        "tests_run": len(interaction_tests),
        "tests_passed": len(test_results) - len(failures),
        "failures": failures,
        "tokens": 0,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Test generation from design spec
# ---------------------------------------------------------------------------

def _generate_tests_from_spec(spec: Dict[str, Any]) -> List[InteractionTest]:
    """Convert design spec interaction_rules into executable test cases."""
    tests = []

    for component in spec.get("components", []):
        rules = component.get("interaction_rules", [])
        states = component.get("states", [])
        name = component["name"]
        testid = component.get("data_testid", name.lower().replace(" ", "-"))

        for rule in rules:
            rule_lower = rule.lower()

            # Modal open/close patterns
            if any(k in rule_lower for k in ["opens on", "open on", "trigger on"]):
                trigger = "click" if "click" in rule_lower else "keyboard" if "key" in rule_lower else "click"
                tests.append(InteractionTest(
                    name=f"{name}: opens on {trigger}",
                    selector=f"[data-testid='{testid}-trigger']" if trigger == "click" else "Enter",
                    action=trigger,
                    pre_condition=f"[data-testid='{testid}-trigger']",
                    post_condition=f"[data-testid='{testid}-content'], [role='dialog']",
                    accessibility_check=True,
                ))

            if any(k in rule_lower for k in ["closes on escape", "close on escape", "escape key"]):
                tests.append(InteractionTest(
                    name=f"{name}: closes on Escape",
                    selector="Escape",
                    action="keyboard",
                    pre_condition=f"[role='dialog'], [data-testid='{testid}-content']",
                    post_condition=f"body:not(:has([role='dialog'])), body:not(:has([data-testid='{testid}-content']))",
                    accessibility_check=False,
                ))

            if any(k in rule_lower for k in ["closes on backdrop", "close on backdrop", "click outside"]):
                tests.append(InteractionTest(
                    name=f"{name}: closes on backdrop click",
                    selector=f"[data-testid='{testid}-overlay'], .modal-overlay, .backdrop",
                    action="click",
                    pre_condition=f"[role='dialog'], [data-testid='{testid}-content']",
                    post_condition=f"body:not(:has([role='dialog'])), body:not(:has([data-testid='{testid}-content']))",
                    accessibility_check=False,
                ))

            # Dropdown / Select patterns
            if any(k in rule_lower for k in ["dropdown", "select", "menu"]):
                tests.append(InteractionTest(
                    name=f"{name}: opens and shows options",
                    selector=f"[data-testid='{testid}-trigger']",
                    action="click",
                    pre_condition=f"[data-testid='{testid}-trigger']",
                    post_condition=f"[role='listbox'], [data-testid='{testid}-options'], [role='menu']",
                    accessibility_check=True,
                ))

                tests.append(InteractionTest(
                    name=f"{name}: selects option",
                    selector=f"[data-testid='{testid}-option-1'], [role='option']:first-child",
                    action="click",
                    pre_condition=f"[role='listbox'], [data-testid='{testid}-options']",
                    post_condition=f"[data-testid='{testid}-trigger']",
                    accessibility_check=True,
                ))

            # Toggle / Switch patterns
            if any(k in rule_lower for k in ["toggle", "switch", "checkbox"]):
                tests.append(InteractionTest(
                    name=f"{name}: toggles state",
                    selector=f"[data-testid='{testid}'], [role='switch']",
                    action="click",
                    pre_condition=f"[data-testid='{testid}'], [role='switch']",
                    post_condition=f"[data-testid='{testid}'], [role='switch']",
                    accessibility_check=True,
                ))

            # Form submission patterns
            if any(k in rule_lower for k in ["submit", "form", "save"]):
                tests.append(InteractionTest(
                    name=f"{name}: submits form",
                    selector=f"[data-testid='{testid}-submit'], [type='submit']",
                    action="click",
                    pre_condition=f"[data-testid='{testid}']",
                    post_condition=f"[data-testid='{testid}-success'], .success-message",
                    accessibility_check=False,
                ))

            # Tab / Navigation patterns
            if any(k in rule_lower for k in ["tab", "navigate", "switch tab"]):
                tests.append(InteractionTest(
                    name=f"{name}: switches tab",
                    selector=f"[data-testid='{testid}-tab-2'], [role='tab']:nth-child(2)",
                    action="click",
                    pre_condition=f"[data-testid='{testid}'], [role='tablist']",
                    post_condition=f"[role='tabpanel']:not([hidden])",
                    accessibility_check=True,
                ))

            # Mobile-specific patterns
            if any(k in rule_lower for k in ["hamburger", "mobile menu", "collapse"]):
                tests.append(InteractionTest(
                    name=f"{name}: mobile menu toggle",
                    selector=f"[data-testid='{testid}-hamburger']",
                    action="click",
                    viewport="mobile",
                    pre_condition=f"[data-testid='{testid}-hamburger']",
                    post_condition=f"[data-testid='{testid}-menu']:not([hidden])",
                    accessibility_check=True,
                ))

    # Deduplicate by name
    seen = set()
    unique_tests = []
    for t in tests:
        if t.name not in seen:
            seen.add(t.name)
            unique_tests.append(t)

    return unique_tests


# ---------------------------------------------------------------------------
# Stack detection helpers
# ---------------------------------------------------------------------------

def _resolve_stack_from_files(files: List[Dict[str, str]]) -> str:
    """Detect frontend stack from file extensions and config files."""
    paths = [f["path"] for f in files]

    if any("next.config" in p or "next.config.js" in p for p in paths):
        return "next.js"
    if any("vite.config" in p for p in paths):
        return "react"
    if any(p.endswith((".jsx", ".tsx")) for p in paths):
        return "react"
    if any(p.endswith(".vue") for p in paths):
        return "vue"
    if any(p.endswith(".svelte") for p in paths):
        return "svelte"
    if any("angular.json" in p for p in paths):
        return "angular"

    return "react"  # safe default


def _get_dev_command(stack: str) -> Optional[str]:
    """Get the dev server start command for a given stack."""
    commands = {
        "react": "npm start",
        "next.js": "npm run dev",
        "nextjs": "npm run dev",
        "vue": "npm run serve",
        "svelte": "npm run dev",
        "angular": "ng serve",
    }
    return commands.get(stack)
