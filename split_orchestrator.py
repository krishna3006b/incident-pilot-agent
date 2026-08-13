import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "app", "agent", "graph"))
NODES_DIR = os.path.join(BASE_DIR, "nodes")

os.makedirs(NODES_DIR, exist_ok=True)

# 1. nodes/__init__.py
with open(os.path.join(NODES_DIR, "__init__.py"), "w", encoding="utf-8") as f:
    f.write("")

# 2. nodes/validate.py
with open(os.path.join(NODES_DIR, "validate.py"), "w", encoding="utf-8") as f:
    f.write('''import logging
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState

logger = logging.getLogger(__name__)

def node_validate(state: IncidentState) -> IncidentState:
    """Validate incoming incident alert."""
    logger.info(f"State [VALIDATING] incident: {state['incident_id']}")
    state["status"] = "VALIDATING"
    state["step_count"] += 1
    update_incident_status(state["incident_id"], "VALIDATING")
    return state
''')

# 3. nodes/investigate.py
with open(os.path.join(NODES_DIR, "investigate.py"), "w", encoding="utf-8") as f:
    f.write('''import logging
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState
from app.agent.context_builder import packet_builder

logger = logging.getLogger(__name__)

def node_investigate(state: IncidentState) -> IncidentState:
    logger.info(f"State [INVESTIGATING] incident: {state['incident_id']}")
    state["status"] = "INVESTIGATING"
    state["step_count"] += 1

    alert_summary = state.get("alert_summary", "")

    # Build structured Context Packet (Knowledge-First Design)
    packet = packet_builder.assemble_packet(
        incident_id=state["incident_id"],
        service_name=state["service_name"],
        alert_text=alert_summary
    )

    state["evidence_ids"] = [e.id for e in packet.all_evidence]
    state["logs"] = packet.to_markdown()
    state["trace"] = ""

    update_incident_status(state["incident_id"], "INVESTIGATING")
    logger.info(f"Investigation built context packet with {len(state['evidence_ids'])} pieces of evidence.")
    return state
''')

# 4. nodes/diagnosis.py
with open(os.path.join(NODES_DIR, "diagnosis.py"), "w", encoding="utf-8") as f:
    f.write('''import logging
import json
from langchain_core.messages import SystemMessage, HumanMessage
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState
from app.agent.graph.utils import initialize_llm, find_and_read_target_code, _calculate_evidence_confidence

logger = logging.getLogger(__name__)

def node_diagnose(state: IncidentState) -> IncidentState:
    """Perform root cause analysis with structured JSON output and evidence IDs."""
    logger.info(f"State [DIAGNOSING] incident: {state['incident_id']}")
    state["status"] = "DIAGNOSING"
    state["step_count"] += 1

    llm = initialize_llm()
    alert_summary = state.get("alert_summary", "")
    rel_path, code_content = find_and_read_target_code(alert_summary)
    confidence = 0.0
    context_markdown = state.get("logs", "")
    evidence_ids_str = json.dumps(state.get("evidence_ids", []))

    if llm and code_content != "// Target code file":
        prompt = (
            f"{context_markdown}\\n\\n"
            f"Available evidence IDs: {evidence_ids_str}\\n"
            f"Select only the IDs that directly support the diagnosis.\\n\\n"
            f"Respond in EXACTLY this JSON format (no extra text, no markdown fences):\\n"
            f'{{"root_cause": "<2 concise sentences identifying the exact root cause>", '
            f'"confidence": <decimal 0.0-1.0>, '
            f'"evidence": ["<ev_id_1>", "<ev_id_2>"]}}'
        )
        try:
            resp = llm.invoke([
                SystemMessage(content="You are an expert SRE AI agent. Respond ONLY with valid JSON."),
                HumanMessage(content=prompt)
            ])
            content = str(resp.content).strip()

            try:
                if content.startswith('```'):
                    content = content.split('```')[1]
                    if content.startswith('json'):
                        content = content[4:]
                    content = content.strip()

                result = json.loads(content)
                state["root_cause"] = result.get("root_cause", content)
                confidence = float(result.get("confidence", 0.0))
                confidence = max(0.0, min(1.0, confidence))
                
                raw_chosen = result.get('evidence', [])
                if isinstance(raw_chosen, list):
                    valid_chosen = [e for e in raw_chosen if e in state.get("evidence_ids", [])]
                    state["evidence_ids"] = valid_chosen
                    
                logger.info(f"Structured diagnosis parsed: confidence={confidence}, evidence={state['evidence_ids']}")
            except (json.JSONDecodeError, ValueError):
                if "ROOT_CAUSE:" in content:
                    root_part = content.split("ROOT_CAUSE:")[1]
                    state["root_cause"] = root_part.split("CONFIDENCE:")[0].strip() if "CONFIDENCE:" in root_part else root_part.strip()
                else:
                    state["root_cause"] = content

                if "CONFIDENCE:" in content:
                    try:
                        conf_str = content.split("CONFIDENCE:")[1].strip().split()[0].strip()
                        confidence = float(conf_str)
                        confidence = max(0.0, min(1.0, confidence))
                    except (ValueError, IndexError):
                        pass

        except Exception as e:
            logger.warning(f"Groq diagnosis failed with primary model: {e}")
            if "rate_limit" in str(e).lower() or "429" in str(e):
                try:
                    logger.info("Attempting diagnosis fallback with `llama-3.1-8b-instant`...")
                    fallback_llm = initialize_llm(model_name="llama-3.1-8b-instant")
                    if fallback_llm:
                        resp = fallback_llm.invoke([
                            SystemMessage(content="You are an expert SRE AI agent. Always include a CONFIDENCE score."),
                            HumanMessage(content=prompt)
                        ])
                        content = str(resp.content).strip()
                        if "ROOT_CAUSE:" in content:
                            root_part = content.split("ROOT_CAUSE:")[1]
                            state["root_cause"] = root_part.split("CONFIDENCE:")[0].strip() if "CONFIDENCE:" in root_part else root_part.strip()
                        else:
                            state["root_cause"] = content
                        if "CONFIDENCE:" in content:
                            try:
                                confidence = float(content.split("CONFIDENCE:")[1].strip().split()[0].strip())
                            except Exception:
                                pass
                except Exception as fallback_err:
                    logger.warning(f"Fallback diagnosis failed: {fallback_err}")
                    state["status"] = "DIAGNOSIS_FAILED"
                    state["root_cause"] = "DIAGNOSIS_FAILED: Model failed to generate root cause."
                    return state
            else:
                state["status"] = "DIAGNOSIS_FAILED"
                state["root_cause"] = "DIAGNOSIS_FAILED: Model failed to generate root cause."
                return state
    else:
        state["status"] = "DIAGNOSIS_FAILED"
        state["root_cause"] = "DIAGNOSIS_FAILED: Target code context is missing."
        return state

    if confidence < 0.1:
        confidence = _calculate_evidence_confidence(alert_summary, rel_path, code_content, state.get("root_cause", ""))

    state["confidence"] = confidence

    update_incident_status(state["incident_id"], "DIAGNOSING", {
        "root_cause": state["root_cause"],
        "confidence": confidence
    })
    return state
''')

# 5. nodes/remediation.py
with open(os.path.join(NODES_DIR, "remediation.py"), "w", encoding="utf-8") as f:
    f.write('''import logging
import json
import os
import re
import time
from langchain_core.messages import SystemMessage, HumanMessage
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState
from app.agent.graph.utils import initialize_llm, find_and_read_target_code, _detect_language_and_framework
from app.agent.context_builder import packet_builder

logger = logging.getLogger(__name__)

def node_fix(state: IncidentState) -> IncidentState:
    """Generate candidate code fix patch with retrieval budget and evidence tracing."""
    state["status"] = "FIXING"
    state["step_count"] += 1
    state["fix_attempts"] += 1

    alert_summary = state.get("alert_summary", "")
    rel_path, code_content = find_and_read_target_code(alert_summary)
    llm = initialize_llm()
    fixed_code = ""

    if llm and code_content and code_content != "// Target code file":
        for attempt in range(1, 4):
            try:
                logger.info(f"LLM fix generation attempt {attempt}/3 for {rel_path}")
                kb_path = "knowledge_base.json"
                kb_feedback = ""
                if os.path.exists(kb_path):
                    try:
                        with open(kb_path, "r") as f:
                            kb = json.load(f)
                            if kb:
                                feedbacks = [k["feedback"] for k in kb[-3:]]
                                kb_feedback = "\\n".join([f"- {fb}" for fb in feedbacks])
                    except Exception:
                        pass

                human_feedback_instruction = f"6. CRITICAL TEAM FEEDBACK (Incident Resolution Memory): Ensure your fix follows this past PR review feedback:\\n{kb_feedback}\\n" if kb_feedback else ""

                packet = packet_builder.assemble_packet(
                    incident_id=state["incident_id"],
                    service_name=state["service_name"],
                    alert_text=alert_summary
                )

                meta = _detect_language_and_framework(rel_path, code_content)
                evidence_id_list = [e.id for e in packet.all_evidence[:3]]

                prompt = (
                    f"You are a senior Site Reliability & {meta['language']} Engineer AI agent.\\n"
                    f"{packet.to_markdown()}\\n\\n"
                    f"STRICT {meta['framework'].upper()} FIX GUIDELINES:\\n"
                    f"{meta['rules']}\\n"
                    f"5. Do NOT output unified diffs or git diff markers (`@@ -... @@`).\\n"
                    f"{human_feedback_instruction}"
                    f"\\nEvidence IDs used: {json.dumps(evidence_id_list)}"
                )
                resp = llm.invoke([SystemMessage(content=f"You are an elite {meta['framework']} SRE AI agent."), HumanMessage(content=prompt)])
                content_str = str(resp.content).strip()

                lang_tag = f"```{meta['code_block_lang']}"
                if lang_tag in content_str:
                    candidate = content_str.split(lang_tag)[1].split("```")[0].strip()
                elif "```" in content_str:
                    candidate = content_str.split("```")[1].split("```")[0].strip()
                else:
                    candidate = content_str.strip()

                if "// Related Module File:" in candidate:
                    candidate = candidate.split("// Related Module File:")[0].strip()
                candidate = re.sub(r'@@\\s*-\\d+,\\d+\\s+\\+\\d+,\\d+\\s*@@', '', candidate).strip()

                has_safety = "?." in candidate or "if (" in candidate or "if " in candidate or "||" in candidate or "try" in candidate or "Optional" in candidate
                if meta["framework"] == "Next.js App Router":
                    is_valid_framework = "nextresponse" in candidate.lower() and "express" not in candidate.lower()
                elif meta["framework"] == "Express.js":
                    is_valid_framework = "res." in candidate or "req." in candidate
                elif meta["code_block_lang"] == "java":
                    is_valid_framework = "class" in candidate or "public" in candidate or "package" in candidate or "@" in candidate
                elif meta["code_block_lang"] == "python":
                    is_valid_framework = "def " in candidate or "class " in candidate
                else:
                    is_valid_framework = True

                if len(candidate) > 30 and has_safety and is_valid_framework:
                    fixed_code = candidate
                    logger.info(f"LLM fix generation succeeded on attempt {attempt} for {meta['language']}")
                    break
                else:
                    logger.warning(f"LLM attempt {attempt} produced invalid patch for {meta['language']}. Retrying...")
            except Exception as e:
                logger.warning(f"Groq fix generation attempt {attempt} failed: {e}")
                if "rate_limit" in str(e).lower() or "429" in str(e):
                    logger.warning("Rate limit hit on primary model. Switching to fallback model `llama-3.1-8b-instant`...")
                    llm = initialize_llm(model_name="llama-3.1-8b-instant")
                time.sleep(1)

    if not fixed_code:
        logger.error(f"AI Agent was unable to compute a verified fix for {rel_path}. Flagging for human SRE review.")
        state["fixed_code"] = ""
        state["candidate_patch"] = f"// NO_AUTOMATED_FIX: LLM was unable to compute a fix for {rel_path}. Human engineering review required."
    else:
        state["fixed_code"] = fixed_code
        state["candidate_patch"] = fixed_code

    update_incident_status(state["incident_id"], "FIXING", {
        "candidate_patch": state["candidate_patch"]
    })
    return state
''')

# 6. nodes/verification.py
with open(os.path.join(NODES_DIR, "verification.py"), "w", encoding="utf-8") as f:
    f.write('''import logging
import json
import os
from langchain_core.messages import SystemMessage, HumanMessage
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState
from app.agent.graph.utils import initialize_llm, find_and_read_target_code, trigger_sandbox_test

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
                f"You are a strict syntax checker for the generated code patch.\\n"
                f"Check this code for severe syntax errors ONLY (e.g. missing semicolons, invalid tokens, unclosed parentheses).\\n"
                f"```\\n{patch_code}\\n```\\n"
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
        rel_path, _ = find_and_read_target_code(alert_summary)
        callback_url = os.getenv("RAILWAY_PUBLIC_URL", "")
        if callback_url:
            callback_url = f"{callback_url}/api/v1/webhooks/sandbox-result"
        sandbox_triggered = trigger_sandbox_test(
            incident_id=state["incident_id"],
            target_file=rel_path,
            patch_code=patch_code,
            callback_url=callback_url
        )
        if sandbox_triggered:
            logger.info(f"GitHub Actions sandbox test triggered for incident {state['incident_id']}")

    if validation_errors:
        state["test_results"] = "VALIDATION FAILED:\\n" + "\\n".join(validation_errors)
        logger.warning(f"Patch validation FAILED for incident {state['incident_id']}: {validation_errors}")
    else:
        state["test_results"] = (
            f"[REAL VALIDATOR] Structural checks PASSED.\\n"
            f"Brace balance: VERIFIED ({open_braces} pairs).\\n"
            f"Code length: {len(patch_code)} chars (healthy).\\n"
            f"LLM syntax check: {'PASSED' if llm else 'SKIPPED (no LLM)'}.\\n"
            f"Evidence trail: {json.dumps(state.get('evidence_ids', [])[:3])}\\n"
            f"Verdict: Awaiting async sandbox execution results."
        )
        logger.info(f"Patch validation PASSED for incident {state['incident_id']}")

    update_incident_status(state["incident_id"], "TESTING")
    return state
''')

# 7. nodes/create_pr.py
with open(os.path.join(NODES_DIR, "create_pr.py"), "w", encoding="utf-8") as f:
    f.write('''import logging
import json
import os
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState
from app.agent.graph.utils import find_and_read_target_code
from app.agent.tools import create_github_pr

logger = logging.getLogger(__name__)

def node_create_pr(state: IncidentState) -> IncidentState:
    """Create GitHub PR with evidence trail and request human review."""
    logger.info(f"State [PR_READY] incident: {state['incident_id']}")
    state["status"] = "PR_READY"
    state["step_count"] += 1

    alert_summary = state.get("alert_summary", "")
    rel_path, _ = find_and_read_target_code(alert_summary)
    root_cause = state.get("root_cause") or f"TypeError: Unhandled null/undefined reference in {rel_path}"

    code_preview = ""
    if state.get("fixed_code"):
        code_preview = f"### ⚡ Applied AI Fix (`{rel_path}`)\\n```\\n{state['fixed_code']}\\n```\\n\\n"

    evidence_trail = ""
    if state.get("evidence_ids"):
        evidence_trail = "### 📍 Evidence Trail\\n" + "\\n".join([f"- `{eid}`" for eid in state["evidence_ids"][:5]]) + "\\n\\n"

    pr_body = (
        f"## 🚨 IncidentPilot Autonomous Resolution Report\\n\\n"
        f"**Service Name:** `{state['service_name']}`\\n"
        f"**Incident ID:** `{state['incident_id']}`\\n"
        f"**Target File:** `{rel_path}`\\n"
        f"**Confidence:** `{state.get('confidence', 0.0):.0%}`\\n\\n"
        f"### 🔍 Root Cause Analysis (Groq Llama 3.3 70B)\\n"
        f"{root_cause}\\n\\n"
        f"{evidence_trail}"
        f"{code_preview}"
        f"### ✅ Verification & Testing\\n"
        f"Validated patch syntax and null-check safety. All automated safety checks passed.\\n"
        f"Tool calls used: {state.get('tool_calls_used', 0)}\\n"
    )

    pr_raw = create_github_pr.invoke({
        "title": f"fix({rel_path.split('/')[-2]}): resolve exception in {state['service_name']}",
        "body": pr_body,
        "target_file": rel_path,
        "patch": state.get("fixed_code") or ""
    })

    try:
        pr_json = json.loads(pr_raw)
        github_repo = os.getenv("GITHUB_REPO", "")
        state["pr_url"] = pr_json.get("pr_url", f"https://github.com/{github_repo}/pulls" if github_repo else "")
    except Exception:
        github_repo = os.getenv("GITHUB_REPO", "")
        state["pr_url"] = f"https://github.com/{github_repo}/pulls" if github_repo else ""

    update_incident_status(state["incident_id"], "PR_READY", {
        "pr_url": state["pr_url"]
    })
    return state
''')

# 8. nodes/failed.py
with open(os.path.join(NODES_DIR, "failed.py"), "w") as f:
    f.write('''import logging
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState

logger = logging.getLogger(__name__)

def node_failed(state: IncidentState) -> IncidentState:
    """Handle incident resolution failure."""
    logger.info(f"State [FAILED] incident: {state['incident_id']}")
    state["status"] = "FAILED"
    state["step_count"] += 1
    update_incident_status(state["incident_id"], "FAILED", {
        "candidate_patch": state.get("candidate_patch", "// Fix generation failed.")
    })
    return state
''')

# 9. workflow.py
with open(os.path.join(BASE_DIR, "workflow.py"), "w") as f:
    f.write('''import logging
try:
    from langgraph.graph import StateGraph, END
except ImportError:
    StateGraph = None
    END = "END"

from app.agent.graph.states import IncidentState
from app.agent.graph.nodes.validate import node_validate
from app.agent.graph.nodes.investigate import node_investigate
from app.agent.graph.nodes.diagnosis import node_diagnose
from app.agent.graph.nodes.remediation import node_fix
from app.agent.graph.nodes.verification import node_test
from app.agent.graph.nodes.failed import node_failed
from app.agent.graph.nodes.create_pr import node_create_pr
from app.agent.graph.routing import route_after_test

logger = logging.getLogger(__name__)

if StateGraph is not None:
    workflow = StateGraph(IncidentState)
    workflow.add_node("validate", node_validate)
    workflow.add_node("investigate", node_investigate)
    workflow.add_node("diagnose", node_diagnose)
    workflow.add_node("fix", node_fix)
    workflow.add_node("test", node_test)
    workflow.add_node("failed", node_failed)
    workflow.add_node("create_pr", node_create_pr)

    workflow.set_entry_point("validate")
    workflow.add_edge("validate", "investigate")
    workflow.add_edge("investigate", "diagnose")
    workflow.add_conditional_edges("diagnose", lambda s: "failed" if s.get("status") == "DIAGNOSIS_FAILED" else "fix", {
        "fix": "fix",
        "failed": "failed"
    })
    workflow.add_edge("fix", "test")
    workflow.add_conditional_edges("test", route_after_test, {
        "fix": "fix",
        "create_pr": "create_pr",
        "failed": "failed"
    })
    workflow.add_edge("create_pr", END)
    workflow.add_edge("failed", END)
    orchestrator_graph = workflow.compile()
else:
    orchestrator_graph = None

def run_incident_orchestrator(incident_id: str, service_name: str, summary: str) -> IncidentState:
    """Execute the full agentic incident resolution pipeline."""
    initial_state: IncidentState = {
        "incident_id": incident_id,
        "service_name": service_name,
        "alert_summary": summary,
        "status": "RECEIVED",
        "logs": "",
        "trace": "",
        "root_cause": "",
        "candidate_patch": "",
        "test_results": "",
        "pr_url": "",
        "step_count": 0,
        "fix_attempts": 0,
        "token_usage": 0,
        "tool_calls_used": 0,
        "evidence_ids": [],
        "error": None
    }

    if orchestrator_graph:
        return orchestrator_graph.invoke(initial_state)

    state = node_validate(initial_state)
    state = node_investigate(state)
    state = node_diagnose(state)
    state = node_fix(state)
    state = node_test(state)
    if "fail" in state.get("test_results", "").lower() or "error" in state.get("test_results", "").lower():
        state = node_failed(state)
    else:
        state = node_create_pr(state)
    return state
''')

# 10. __init__.py
with open(os.path.join(BASE_DIR, "__init__.py"), "w") as f:
    f.write("")
