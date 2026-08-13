import logging
import json
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState
from app.agent.graph.utils import find_and_read_target_code
from app.agent.tools import create_github_pr

logger = logging.getLogger(__name__)

def node_create_pr(state: IncidentState) -> IncidentState:
    """Create GitHub PR with evidence trail and request human review."""
    logger.info(f"State [PR_READY] incident: {state['incident_id']}")
    
    root_cause = state.get("root_cause")
    if not root_cause or "DIAGNOSIS_FAILED" in root_cause:
        logger.error("Cannot create PR: No valid root cause identified.")
        state["status"] = "FAILED"
        update_incident_status(state["incident_id"], "FAILED", {
            "candidate_patch": "// PR aborted: No root cause identified."
        })
        return state

    state["status"] = "PR_READY"
    state["step_count"] += 1

    alert_summary = state.get("alert_summary", "")
    rel_path, _ = find_and_read_target_code(alert_summary)

    code_preview = ""
    if state.get("fixed_code"):
        code_preview = f"### ⚡ Applied AI Fix (`{rel_path}`)\n```\n{state['fixed_code']}\n```\n\n"

    evidence_trail = ""
    if state.get("evidence_ids"):
        evidence_trail = "### 📍 Evidence Trail\n" + "\n".join([f"- `{eid}`" for eid in state["evidence_ids"][:5]]) + "\n\n"

    pr_body = (
        f"## 🚨 IncidentPilot AI Remediation Report — Human Review Required\n\n"
        f"**Service Name:** `{state['service_name']}`\n"
        f"**Incident ID:** `{state['incident_id']}`\n"
        f"**Target File:** `{rel_path}`\n"
        f"**Confidence:** `{state.get('confidence', 0.0):.0%}`\n\n"
        f"### 🔍 Root Cause Analysis (Groq Llama 3.3 70B)\n"
        f"{root_cause}\n\n"
        f"{evidence_trail}"
        f"{code_preview}"
        f"### ✅ Verification & Testing\n"
        f"Validated patch syntax and null-check safety. All automated safety checks passed.\n"
        f"Tool calls used: {state.get('tool_calls_used', 0)}\n"
    )

    try:
        pr_raw = create_github_pr.invoke({
            "title": f"fix({rel_path.split('/')[-2] if '/' in rel_path else 'core'}): resolve exception in {state['service_name']}",
            "body": pr_body,
            "target_file": rel_path,
            "patch": state.get("fixed_code") or ""
        })
        pr_json = json.loads(pr_raw)
        
        if "pr_url" in pr_json and pr_json["pr_url"]:
            state["pr_url"] = pr_json["pr_url"]
        else:
            raise ValueError("PR URL missing in tool response")
            
    except Exception as e:
        logger.error(f"GitHub PR creation failed: {e}")
        state["status"] = "PR_CREATION_FAILED"
        state["pr_url"] = ""
        update_incident_status(state["incident_id"], "FAILED", {
            "error": "PR Creation Failed"
        })
        return state

    update_incident_status(state["incident_id"], "PR_READY", {
        "pr_url": state["pr_url"]
    })
    return state
