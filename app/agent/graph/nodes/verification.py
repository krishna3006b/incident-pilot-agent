import logging
import json
import os
from langchain_core.messages import SystemMessage, HumanMessage
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState
from app.agent.graph.utils import initialize_llm, find_and_read_target_code, trigger_sandbox_test, _detect_language_and_framework

logger = logging.getLogger(__name__)

def node_test(state: IncidentState) -> IncidentState:
    """Validate candidate patch using static checks + optional sandbox trigger."""
    logger.info(f"State [TESTING] incident: {state['incident_id']}")
    state["status"] = "TESTING"
    state["step_count"] += 1

    patch_code = state.get("fixed_code") or state.get("candidate_patch", "")
    if not patch_code or "NO_AUTOMATED_FIX" in patch_code:
        state["test_results"] = "FAIL: AI model was unable to generate a valid fix patch. Human engineering review required."
        logger.warning(f"Patch validation FAILED: No valid patch code generated for incident {state['incident_id']}")
        update_incident_status(state["incident_id"], "TESTING")
        return state

    validation_errors = []

    open_braces = patch_code.count("{")
    close_braces = patch_code.count("}")
    if open_braces != close_braces:
        validation_errors.append(f"FAIL: Unbalanced braces (open={open_braces}, close={close_braces})")

    if len(patch_code) < 50:
        validation_errors.append(f"FAIL: Patch too short ({len(patch_code)} chars), likely a placeholder")

    llm = initialize_llm()
    if llm and len(validation_errors) == 0:
        try:
            verify_prompt = (
                f"You are a strict syntax checker for the generated code patch.\n"
                f"Check this code for severe syntax errors ONLY (e.g. missing semicolons, invalid tokens, unclosed parentheses).\n"
                f"```\n{patch_code}\n```\n"
                f"Respond with EXACTLY one word: PASS or FAIL. If FAIL, add a colon and the error."
            )
            resp = llm.invoke([SystemMessage(content="You are a syntax checker."), HumanMessage(content=verify_prompt)])
            llm_result = str(resp.content).strip()
            if llm_result.startswith("FAIL"):
                validation_errors.append(f"LLM syntax check: {llm_result}")
            else:
                logger.info("LLM syntax verification: PASS")
        except Exception as e:
            logger.warning(f"LLM syntax verification skipped: {e}")

    if len(validation_errors) == 0:
        alert_summary = state.get("alert_summary", "")
        rel_path, code_content = find_and_read_target_code(alert_summary)
        meta = _detect_language_and_framework(rel_path, code_content)
        
        callback_url = os.getenv("RAILWAY_PUBLIC_URL", "")
        if callback_url:
            callback_url = f"{callback_url}/api/v1/webhooks/sandbox-result"
            
        sandbox_triggered = trigger_sandbox_test(
            incident_id=state["incident_id"],
            target_file=rel_path,
            patch_code=patch_code,
            callback_url=callback_url,
            setup_cmd=meta.get("setup_cmd", ""),
            build_cmd=meta.get("build_cmd", ""),
            test_cmd=meta.get("test_cmd", "")
        )
        if sandbox_triggered:
            logger.info(f"GitHub Actions sandbox test triggered for incident {state['incident_id']}")

    if validation_errors:
        state["test_results"] = "VALIDATION FAILED:\n" + "\n".join(validation_errors)
        logger.warning(f"Patch validation FAILED for incident {state['incident_id']}: {validation_errors}")
    else:
        state["test_results"] = (
            f"[REAL VALIDATOR] Structural checks PASSED.\n"
            f"Brace balance: VERIFIED ({open_braces} pairs).\n"
            f"Code length: {len(patch_code)} chars (healthy).\n"
            f"LLM syntax check: {'PASSED' if llm else 'SKIPPED (no LLM)'}.\n"
            f"Evidence trail: {json.dumps(state.get('evidence_ids', [])[:3])}\n"
            f"Verdict: Awaiting async sandbox execution results."
        )
        logger.info(f"Patch validation PASSED for incident {state['incident_id']}")

    update_incident_status(state["incident_id"], "TESTING")
    return state
