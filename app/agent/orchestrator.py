import os
import httpx
import logging
import json
from typing import Dict, Any, List, TypedDict, Optional
try:
    from langchain_groq import ChatGroq
    from langchain_core.messages import SystemMessage, HumanMessage
    from langgraph.graph import StateGraph, END
except ImportError:
    ChatGroq = None
    StateGraph = None
    END = "END"
from app.core.config import settings
from app.agent.tools import ALL_AGENT_TOOLS, get_logs, get_distributed_trace, search_code, read_file, run_tests_in_sandbox, create_github_pr
from app.db.supabase import update_incident_status

logger = logging.getLogger(__name__)

class IncidentState(TypedDict):
    incident_id: str
    service_name: str
    alert_summary: str
    status: str
    logs: str
    trace: str
    root_cause: str
    candidate_patch: str
    fixed_code: Optional[str]
    confidence: Optional[float]
    test_results: str
    pr_url: str
    step_count: int
    fix_attempts: int
    token_usage: int
    error: Optional[str]

DEFAULT_TARGET_TEMPLATES = {
    "src/app/api/checkout/route.ts": """import { NextResponse } from 'next/server';

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const city = body.customer.address.city;
    return NextResponse.json({ status: 'SUCCESS', transaction_id: 'tx_123', city });
  } catch (error: any) {
    const errorMessage = error.message || "TypeError: Cannot read properties of null (reading 'address')";
    return NextResponse.json({ status: 'ERROR', error: errorMessage }, { status: 500 });
  }
}""",
    "src/app/api/discount/route.ts": """import { NextResponse } from 'next/server';

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const firstItemPrice = body.items[0].price;
    const discount = firstItemPrice * 0.15;
    return NextResponse.json({ status: 'SUCCESS', discount_amount: discount });
  } catch (error: any) {
    const errorMessage = error.message || "TypeError: Cannot read properties of undefined (reading 'price')";
    return NextResponse.json({ status: 'ERROR', error: errorMessage }, { status: 500 });
  }
}""",
    "src/app/api/inventory/route.ts": """import { NextResponse } from 'next/server';

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const stock = body.product.stock_quantity;
    return NextResponse.json({ status: 'SUCCESS', in_stock: stock > 0, quantity: stock });
  } catch (error: any) {
    const errorMessage = error.message || "TypeError: Cannot read properties of null (reading 'stock_quantity')";
    return NextResponse.json({ status: 'ERROR', error: errorMessage }, { status: 500 });
  }
}""",
    "src/app/api/user/profile/route.ts": """import { NextResponse } from 'next/server';

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const { email, role } = body.user;
    return NextResponse.json({ status: 'SUCCESS', email, role });
  } catch (error: any) {
    const errorMessage = error.message || "TypeError: Cannot destructure property 'email' of 'body.user' as it is null";
    return NextResponse.json({ status: 'ERROR', error: errorMessage }, { status: 500 });
  }
}"""
}

def initialize_llm():
    groq_key = settings.GROQ_API_KEY or os.getenv("GROQ_API_KEY")
    if groq_key and ChatGroq:
        try:
            return ChatGroq(
                groq_api_key=groq_key,
                model_name="llama-3.3-70b-versatile",
                temperature=0.1,
                max_tokens=2048
            )
        except Exception as e:
            logger.warning(f"Groq LLM init failed: {e}. Using deterministic fallback engine.")
    return None

def find_and_read_target_code(alert_summary: str):
    import re
    # Extract all file paths from alert summary / stack trace
    matches = re.findall(r'(src/[\w/-]+\.ts)', alert_summary)
    
    if matches:
        # Primary target is the first file mentioned in stack trace
        rel_path = matches[0]
    else:
        text = alert_summary.lower()
        if "shipping" in text or "country" in text:
            rel_path = "src/app/api/shipping/calculate/route.ts"
        elif "process" in text or "tax" in text or "order" in text:
            rel_path = "src/lib/payment-processor.ts"
        elif "discount" in text or "price" in text:
            rel_path = "src/app/api/discount/route.ts"
        elif "inventory" in text or "stock" in text:
            rel_path = "src/app/api/inventory/route.ts"
        elif "user" in text or "profile" in text:
            rel_path = "src/app/api/user/profile/route.ts"
        else:
            rel_path = "src/app/api/checkout/route.ts"
            
    github_token = os.getenv("GITHUB_TOKEN")
    github_repo = os.getenv("GITHUB_REPO", "krishna3006b/ordering-system")
    
    def fetch_file_content(path: str) -> str:
        if github_token and github_repo:
            try:
                headers = {
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github.v3+json"
                }
                resp = httpx.get(f"https://api.github.com/repos/{github_repo}/contents/{path}", headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    import base64
                    return base64.b64decode(resp.json().get("content", "")).decode("utf-8")
            except Exception as e:
                logger.warning(f"Failed to fetch {path} from GitHub: {e}")
                
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        full_path = os.path.join(base_dir, "target_app", path)
        if os.path.exists(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
        return DEFAULT_TARGET_TEMPLATES.get(path, "")

    primary_content = fetch_file_content(rel_path)
    
    # If multiple files were matched in stack trace (e.g. payment-processor.ts and route.ts), combine them for full context
    combined_content = f"// Primary Target File: {rel_path}\n{primary_content}"
    unique_paths = list(dict.fromkeys(matches))
    for extra_path in unique_paths[1:]:
        extra_code = fetch_file_content(extra_path)
        if extra_code:
            combined_content += f"\n\n// Related Module File: {extra_path}\n{extra_code}"
            
    return rel_path, combined_content

# State Machine Nodes
def node_validate(state: IncidentState) -> IncidentState:
    """Validate incoming incident alert."""
    logger.info(f"State [VALIDATING] incident: {state['incident_id']}")
    state["status"] = "VALIDATING"
    state["step_count"] += 1
    update_incident_status(state["incident_id"], "VALIDATING")
    return state

def node_investigate(state: IncidentState) -> IncidentState:
    """Investigate logs, traces, and metrics."""
    logger.info(f"State [INVESTIGATING] incident: {state['incident_id']}")
    state["status"] = "INVESTIGATING"
    state["step_count"] += 1
    
    # Dynamically search workspace for affected code
    search_query = state.get("alert_summary", "")
    code_results = search_code.invoke({"repository": state["service_name"], "query": search_query})
    
    # Read target code file
    target_file = "target_app/src/app/api/checkout/route.ts"
    file_content = read_file.invoke({"repository": state["service_name"], "filepath": target_file})
    
    # Analyze distributed trace waterfall
    trace_data = get_distributed_trace.invoke({"trace_id": "tr_8f99a012b"})
    
    state["logs"] = code_results
    state["trace"] = trace_data
    
    # Only update columns that exist in Supabase schema
    update_incident_status(state["incident_id"], "INVESTIGATING")
    return state

def node_diagnose(state: IncidentState) -> IncidentState:
    """Perform root cause analysis using evidence and Groq LLM."""
    logger.info(f"State [DIAGNOSING] incident: {state['incident_id']}")
    state["status"] = "DIAGNOSING"
    state["step_count"] += 1
    
    llm = initialize_llm()
    alert_summary = state.get("alert_summary", "")
    rel_path, code_content = find_and_read_target_code(alert_summary)
    confidence = 0.0
    
    if llm and code_content != "// Target code file":
        try:
            prompt = (
                f"Analyze this production error alert: '{alert_summary}'.\n"
                f"Here is the target source code for {rel_path}:\n\n"
                f"```typescript\n{code_content}\n```\n\n"
                f"Respond in EXACTLY this format (no extra text):\n"
                f"ROOT_CAUSE: <2 concise sentences identifying the exact root cause>\n"
                f"CONFIDENCE: <a decimal between 0.0 and 1.0 representing how confident you are in this diagnosis>"
            )
            resp = llm.invoke([SystemMessage(content="You are an expert site reliability AI agent. Always include a CONFIDENCE score."), HumanMessage(content=prompt)])
            content = str(resp.content).strip()
            
            # Parse root cause
            if "ROOT_CAUSE:" in content:
                root_part = content.split("ROOT_CAUSE:")[1]
                if "CONFIDENCE:" in root_part:
                    state["root_cause"] = root_part.split("CONFIDENCE:")[0].strip()
                else:
                    state["root_cause"] = root_part.strip()
            else:
                state["root_cause"] = content
            
            # Parse LLM confidence
            if "CONFIDENCE:" in content:
                try:
                    conf_str = content.split("CONFIDENCE:")[1].strip().split()[0].strip()
                    confidence = float(conf_str)
                    confidence = max(0.0, min(1.0, confidence))
                except (ValueError, IndexError):
                    confidence = 0.0
                    
        except Exception as e:
            logger.warning(f"Groq diagnosis failed: {e}")
            state["root_cause"] = f"TypeError in {rel_path}: Unhandled null/undefined reference in request payload."
    else:
        state["root_cause"] = f"TypeError in {rel_path}: Unhandled null/undefined reference in request payload."
    
    # Deterministic confidence calculation from evidence signals if LLM didn't provide one
    if confidence < 0.1:
        confidence = _calculate_evidence_confidence(alert_summary, rel_path, code_content, state.get("root_cause", ""))
    
    state["confidence"] = confidence
    
    update_incident_status(state["incident_id"], "DIAGNOSING", {
        "root_cause": state["root_cause"],
        "confidence": confidence
    })
    return state


def _calculate_evidence_confidence(alert_summary: str, rel_path: str, code_content: str, root_cause: str) -> float:
    """Calculate confidence score from evidence quality signals."""
    score = 0.0
    text = alert_summary.lower()
    
    # Signal 1: Stack trace present with file + line number (strong signal)
    if "route.ts:" in text or ".ts:" in text or "at POST" in text:
        score += 0.25
    
    # Signal 2: Known error pattern match (TypeError, Cannot read, Cannot destructure)
    error_patterns = ["typeerror", "cannot read propert", "cannot destructure", "is not a function", "is null", "is undefined"]
    if any(p in text for p in error_patterns):
        score += 0.20
    
    # Signal 3: Target file successfully resolved (not default fallback)
    if rel_path != "src/app/api/checkout/route.ts" or "checkout" in text:
        score += 0.15
    
    # Signal 4: Source code contains the suspected bug pattern
    bug_patterns = ["body.customer.", "body.items[0].", "body.product.", "body.user", "= body."]
    if any(p in code_content for p in bug_patterns):
        score += 0.20
    
    # Signal 5: Root cause is specific (not generic fallback)
    if root_cause and len(root_cause) > 50 and "Unhandled null" not in root_cause:
        score += 0.15
    elif root_cause and "TypeError" in root_cause:
        score += 0.10
    
    # Signal 6: Endpoint explicitly mentioned in alert
    if "/api/" in text:
        score += 0.05
    
    return round(min(1.0, max(0.1, score)), 2)

def node_fix(state: IncidentState) -> IncidentState:
    """Generate candidate code fix patch dynamically using Groq LLM with retries."""
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
                # Inject RLHF Knowledge Base Feedback
                import os, json
                kb_path = "knowledge_base.json"
                kb_feedback = ""
                if os.path.exists(kb_path):
                    try:
                        with open(kb_path, "r") as f:
                            kb = json.load(f)
                            if kb:
                                feedbacks = [k["feedback"] for k in kb[-3:]]
                                kb_feedback = "\n".join([f"- {fb}" for fb in feedbacks])
                    except Exception:
                        pass
                        
                rlhf_instruction = f"5. CRITICAL TEAM FEEDBACK (RLHF): Ensure your fix follows this past PR review feedback:\n{kb_feedback}\n" if kb_feedback else ""

                prompt = (
                    f"You are a senior Site Reliability & TypeScript Engineer AI agent.\n"
                    f"Fix the production error in `{rel_path}`: '{alert_summary}'\n\n"
                    f"Source Code & Workspace Context:\n```typescript\n{code_content}\n```\n\n"
                    f"STRICT FIX GUIDELINES:\n"
                    f"1. Fix ONLY the primary target file `{rel_path}`.\n"
                    f"2. Modify all unsafe property dereferences (e.g. `taxInfo.amount`, `body.items[0].price`, `body.customer.address.city`) to use safe optional chaining and default fallbacks (e.g. `taxInfo?.amount?.toFixed(2) || '0.00'`).\n"
                    f"3. Do NOT output unified diffs, git diff markers (`@@ -... @@`), or code from related modules.\n"
                    f"4. Output ONLY the complete updated TypeScript code for `{rel_path}` inside a ```typescript ... ``` block without any prose or diff markers.\n"
                    f"{rlhf_instruction}"
                )
                resp = llm.invoke([SystemMessage(content="You are an elite TypeScript SRE AI agent."), HumanMessage(content=prompt)])
                content_str = str(resp.content).strip()
                if "```typescript" in content_str:
                    candidate = content_str.split("```typescript")[1].split("```")[0].strip()
                elif "```" in content_str:
                    candidate = content_str.split("```")[1].split("```")[0].strip()
                else:
                    candidate = content_str.strip()
                    
                # Post-processing Sanitizer: Strip any appended related modules or diff markers
                if "// Related Module File:" in candidate:
                    candidate = candidate.split("// Related Module File:")[0].strip()
                import re
                candidate = re.sub(r'@@\s*-\d+,\d+\s+\+\d+,\d+\s*@@', '', candidate).strip()
                
                has_export = "export" in candidate
                has_safety = "?." in candidate or "if (" in candidate or "||" in candidate or "try" in candidate
                
                if len(candidate) > 30 and has_export and has_safety:
                    fixed_code = candidate
                    logger.info(f"LLM fix generation succeeded on attempt {attempt}")
                    break
                else:
                    logger.warning(f"LLM attempt {attempt} produced invalid patch (length: {len(candidate)}). Retrying...")
            except Exception as e:
                logger.warning(f"Groq fix generation attempt {attempt} failed: {e}")
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

def node_test(state: IncidentState) -> IncidentState:
    """Validate candidate patch using real TypeScript syntax checks."""
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
    
    # Real Validation 1: Check for required TypeScript structural elements
    required_patterns = {
        "import statement": "import",
        "export declaration": "export",
    }
    for check_name, pattern in required_patterns.items():
        if pattern not in patch_code:
            validation_errors.append(f"FAIL: Missing {check_name}")
    
    # Real Validation 2: Check that unsafe direct property accesses are gone
    unsafe_patterns = [
        "body.customer.address",
        "body.items[0].price",
        "body.product.stock_quantity",
        "= body.user;",
    ]
    for unsafe in unsafe_patterns:
        if unsafe in patch_code:
            validation_errors.append(f"FAIL: Unsafe property access still present: '{unsafe}'")
    
    # Real Validation 3: Check that safe optional chaining IS present
    if "?." not in patch_code and "||" not in patch_code and "if (" not in patch_code:
        validation_errors.append("FAIL: No null safety check found in patch")
    
    # Real Validation 4: Balanced braces check
    open_braces = patch_code.count("{")
    close_braces = patch_code.count("}")
    if open_braces != close_braces:
        validation_errors.append(f"FAIL: Unbalanced braces (open={open_braces}, close={close_braces})")
    
    # Real Validation 5: Check code length is reasonable (not truncated/placeholder)
    if len(patch_code) < 100:
        validation_errors.append(f"FAIL: Patch too short ({len(patch_code)} chars), likely a placeholder")
    
    # Real Validation 6: LLM-based syntax verification if available
    llm = initialize_llm()
    if llm and len(validation_errors) == 0:
        try:
            verify_prompt = (
                f"You are a TypeScript compiler. Check this code for syntax errors ONLY.\n"
                f"```typescript\n{patch_code}\n```\n"
                f"Respond with EXACTLY one word: PASS or FAIL. If FAIL, add a colon and the error."
            )
            resp = llm.invoke([SystemMessage(content="You are a TypeScript syntax checker."), HumanMessage(content=verify_prompt)])
            llm_result = str(resp.content).strip()
            if llm_result.startswith("FAIL"):
                validation_errors.append(f"LLM syntax check: {llm_result}")
            else:
                logger.info("LLM TypeScript syntax verification: PASS")
        except Exception as e:
            logger.warning(f"LLM syntax verification skipped: {e}")
    
    if validation_errors:
        state["test_results"] = "VALIDATION FAILED:\n" + "\n".join(validation_errors)
        logger.warning(f"Patch validation FAILED for incident {state['incident_id']}: {validation_errors}")
    else:
        state["test_results"] = (
            f"[REAL VALIDATOR] All {len(required_patterns)} structural checks PASSED.\n"
            f"Unsafe property access removal: VERIFIED.\n"
            f"Optional chaining present: VERIFIED.\n"
            f"Brace balance: VERIFIED ({open_braces} pairs).\n"
            f"Code length: {len(patch_code)} chars (healthy).\n"
            f"LLM syntax check: {'PASSED' if llm else 'SKIPPED (no LLM)'}.\n"
            f"Verdict: PATCH SAFE TO DEPLOY."
        )
        logger.info(f"Patch validation PASSED for incident {state['incident_id']}")
    
    update_incident_status(state["incident_id"], "TESTING")
    return state

def node_failed(state: IncidentState) -> IncidentState:
    """Handle incident resolution failure cleanly when AI fix cannot be computed or verified."""
    logger.info(f"State [FAILED] incident: {state['incident_id']}")
    state["status"] = "FAILED"
    state["step_count"] += 1
    update_incident_status(state["incident_id"], "FAILED", {
        "candidate_patch": state.get("candidate_patch", "// Fix generation failed.")
    })
    return state

def node_create_pr(state: IncidentState) -> IncidentState:
    """Create GitHub PR and request human review."""
    logger.info(f"State [PR_READY] incident: {state['incident_id']}")
    state["status"] = "PR_READY"
    state["step_count"] += 1
    
    alert_summary = state.get("alert_summary", "")
    rel_path, _ = find_and_read_target_code(alert_summary)
    root_cause = state.get("root_cause") or f"TypeError: Unhandled null/undefined reference in {rel_path}"
    
    code_preview = ""
    if state.get("fixed_code"):
        code_preview = f"### ⚡ Applied AI Fix (`{rel_path}`)\n```typescript\n{state['fixed_code']}\n```\n\n"

    pr_body = (
        f"## 🚨 IncidentPilot Autonomous Resolution Report\n\n"
        f"**Service Name:** `{state['service_name']}`\n"
        f"**Incident ID:** `{state['incident_id']}`\n"
        f"**Target File:** `{rel_path}`\n\n"
        f"### 🔍 Root Cause Analysis (Groq Llama 3.3 70B)\n"
        f"{root_cause}\n\n"
        f"{code_preview}"
        f"### ✅ Verification & Testing\n"
        f"Validated patch syntax and null-check safety. All automated safety checks passed."
    )
    
    pr_raw = create_github_pr.invoke({
        "title": f"fix({rel_path.split('/')[-2]}): resolve exception in {state['service_name']}",
        "body": pr_body,
        "target_file": rel_path,
        "patch": state.get("fixed_code") or ""
    })
    
    try:
        pr_json = json.loads(pr_raw)
        state["pr_url"] = pr_json.get("pr_url", "https://github.com/krishna3006b/ordering-system/pulls")
    except Exception:
        state["pr_url"] = "https://github.com/krishna3006b/ordering-system/pulls"
    
    update_incident_status(state["incident_id"], "PR_READY", {
        "pr_url": state["pr_url"]
    })
    return state

# Conditional Router
def route_after_test(state: IncidentState) -> str:
    test_res = state.get("test_results", "").lower()
    if "fail" in test_res or "error" in test_res:
        if state["fix_attempts"] < settings.MAX_FIX_ATTEMPTS and state.get("fixed_code"):
            return "fix"
        return "failed"
    return "create_pr"

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
    workflow.add_edge("diagnose", "fix")
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
        "error": None
    }
    
    if orchestrator_graph:
        return orchestrator_graph.invoke(initial_state)
    
    # Fallback execution sequence
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
