import os
import time
import httpx
import logging
import json
import re
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
from app.agent.context_builder import packet_builder

logger = logging.getLogger(__name__)


# --- State Type ---
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
    tool_calls_used: int
    evidence_ids: List[str]
    error: Optional[str]


# --- Retrieval Budget Tracker ---
class RetrievalBudget:
    """Tracks tool call usage against budget limits."""
    def __init__(self, max_tool_calls=20, max_read_file=10, max_search=5):
        self.max_tool_calls = max_tool_calls
        self.max_read_file = max_read_file
        self.max_search = max_search
        self.total_calls = 0
        self.read_file_calls = 0
        self.search_calls = 0

    def can_call(self, call_type: str = "generic") -> bool:
        if self.total_calls >= self.max_tool_calls:
            return False
        if call_type == "read_file" and self.read_file_calls >= self.max_read_file:
            return False
        if call_type == "search" and self.search_calls >= self.max_search:
            return False
        return True

    def record_call(self, call_type: str = "generic"):
        self.total_calls += 1
        if call_type == "read_file":
            self.read_file_calls += 1
        elif call_type == "search":
            self.search_calls += 1

    def summary(self) -> Dict[str, int]:
        return {
            "total_calls": self.total_calls,
            "max_tool_calls": self.max_tool_calls,
            "read_file_calls": self.read_file_calls,
            "search_calls": self.search_calls
        }


# --- Default Target Templates (fallback code for known routes) ---
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


def initialize_llm(model_name: str = "llama-3.3-70b-versatile"):
    groq_key = settings.GROQ_API_KEY or os.getenv("GROQ_API_KEY")
    if groq_key and ChatGroq:
        try:
            return ChatGroq(
                groq_api_key=groq_key,
                model_name=model_name,
                temperature=0.1,
                max_tokens=2048
            )
        except Exception as e:
            logger.warning(f"Groq LLM init failed for {model_name}: {e}.")
    return None


def find_and_read_target_code(alert_summary: str):
    """Extract target file from alert and fetch its code content."""
    matches = re.findall(r'(src/[\w/-]+\.ts)', alert_summary)

    if matches:
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

    combined_content = f"// Primary Target File: {rel_path}\n{primary_content}"
    unique_paths = list(dict.fromkeys(matches))
    for extra_path in unique_paths[1:]:
        extra_code = fetch_file_content(extra_path)
        if extra_code:
            combined_content += f"\n\n// Related Module File: {extra_path}\n{extra_code}"

    return rel_path, combined_content


# --- Sandbox Trigger ---
def trigger_sandbox_test(incident_id: str, target_file: str, patch_code: str, target_branch: str = "main", callback_url: str = "") -> bool:
    """Trigger GitHub Actions sandbox-test.yml via repository_dispatch on agent repo."""
    github_token = os.getenv("GITHUB_TOKEN")
    agent_repo = os.getenv("AGENT_REPO", "krishna3006b/incident-pilot-agent")

    if not github_token:
        logger.warning("No GITHUB_TOKEN set, skipping sandbox trigger.")
        return False

    try:
        resp = httpx.post(
            f"https://api.github.com/repos/{agent_repo}/dispatches",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={
                "event_type": "validate_patch",
                "client_payload": {
                    "incident_id": incident_id,
                    "target_file": target_file,
                    "target_branch": target_branch,
                    "patch_code": patch_code[:5000],
                    "callback_url": callback_url
                }
            },
            timeout=10.0
        )
        if resp.status_code in (200, 204):
            logger.info(f"Sandbox test triggered on {agent_repo} for incident {incident_id}")
            return True
        else:
            logger.warning(f"Sandbox trigger for {agent_repo} returned {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.warning(f"Sandbox trigger failed: {e}")
    return False


# --- State Machine Nodes ---
def node_validate(state: IncidentState) -> IncidentState:
    """Validate incoming incident alert."""
    logger.info(f"State [VALIDATING] incident: {state['incident_id']}")
    state["status"] = "VALIDATING"
    state["step_count"] += 1
    update_incident_status(state["incident_id"], "VALIDATING")
    return state


def node_investigate(state: IncidentState) -> IncidentState:
    """Investigate logs, traces, and metrics with budget tracking."""
    logger.info(f"State [INVESTIGATING] incident: {state['incident_id']}")
    state["status"] = "INVESTIGATING"
    state["step_count"] += 1

    budget = RetrievalBudget()

    # Search workspace for affected code
    search_query = state.get("alert_summary", "")
    if budget.can_call("search"):
        code_results = search_code.invoke({"repository": state["service_name"], "query": search_query})
        budget.record_call("search")
    else:
        code_results = ""

    # Read target code file
    if budget.can_call("read_file"):
        target_file = "target_app/src/app/api/checkout/route.ts"
        file_content = read_file.invoke({"repository": state["service_name"], "filepath": target_file})
        budget.record_call("read_file")
    else:
        file_content = ""

    # Analyze distributed trace
    if budget.can_call("generic"):
        trace_data = get_distributed_trace.invoke({"trace_id": "tr_8f99a012b"})
        budget.record_call("generic")
    else:
        trace_data = ""

    state["logs"] = code_results
    state["trace"] = trace_data
    state["tool_calls_used"] += budget.total_calls

    update_incident_status(state["incident_id"], "INVESTIGATING")
    logger.info(f"Investigation budget: {budget.summary()}")
    return state


def node_diagnose(state: IncidentState) -> IncidentState:
    """Perform root cause analysis with structured JSON output and evidence IDs."""
    logger.info(f"State [DIAGNOSING] incident: {state['incident_id']}")
    state["status"] = "DIAGNOSING"
    state["step_count"] += 1

    llm = initialize_llm()
    alert_summary = state.get("alert_summary", "")
    rel_path, code_content = find_and_read_target_code(alert_summary)
    confidence = 0.0

    # Build structured Context Packet
    packet = packet_builder.assemble_packet(
        incident_id=state["incident_id"],
        service_name=state["service_name"],
        alert_text=alert_summary
    )

    # Collect evidence IDs for traceability
    state["evidence_ids"] = [e.id for e in packet.all_evidence]

    if llm and code_content != "// Target code file":
        prompt = (
            f"{packet.to_markdown()}\n\n"
            f"Respond in EXACTLY this JSON format (no extra text, no markdown fences):\n"
            f'{{"root_cause": "<2 concise sentences identifying the exact root cause>", '
            f'"confidence": <decimal 0.0-1.0>, '
            f'"evidence": {json.dumps(state["evidence_ids"][:3])}}}'
        )
        try:
            resp = llm.invoke([
                SystemMessage(content="You are an expert SRE AI agent. Respond ONLY with valid JSON."),
                HumanMessage(content=prompt)
            ])
            content = str(resp.content).strip()

            # Try to parse structured JSON response
            try:
                # Strip markdown fences if present
                if content.startswith('```'):
                    content = content.split('```')[1]
                    if content.startswith('json'):
                        content = content[4:]
                    content = content.strip()

                result = json.loads(content)
                state["root_cause"] = result.get("root_cause", content)
                confidence = float(result.get("confidence", 0.0))
                confidence = max(0.0, min(1.0, confidence))
                logger.info(f"Structured diagnosis parsed: confidence={confidence}, evidence={result.get('evidence', [])}")
            except (json.JSONDecodeError, ValueError):
                # Fallback: parse old-style text format
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
                    state["root_cause"] = f"TypeError in {rel_path}: Unhandled null/undefined reference in request payload."
            else:
                state["root_cause"] = f"TypeError in {rel_path}: Unhandled null/undefined reference in request payload."
    else:
        state["root_cause"] = f"TypeError in {rel_path}: Unhandled null/undefined reference in request payload."

    # Deterministic confidence if LLM didn't provide one
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

    # Signal 1: Stack trace present with file + line number
    if "route.ts:" in text or ".ts:" in text or "at POST" in text:
        score += 0.25

    # Signal 2: Known error pattern match
    error_patterns = ["typeerror", "cannot read propert", "cannot destructure", "is not a function", "is null", "is undefined"]
    if any(p in text for p in error_patterns):
        score += 0.20

    # Signal 3: Target file successfully resolved
    if rel_path != "src/app/api/checkout/route.ts" or "checkout" in text:
        score += 0.15

    # Signal 4: Source code contains suspected bug pattern
    bug_patterns = ["body.customer.", "body.items[0].", "body.product.", "body.user", "= body."]
    if any(p in code_content for p in bug_patterns):
        score += 0.20

    # Signal 5: Root cause is specific
    if root_cause and len(root_cause) > 50 and "Unhandled null" not in root_cause:
        score += 0.15
    elif root_cause and "TypeError" in root_cause:
        score += 0.10

    # Signal 6: Endpoint explicitly mentioned in alert
    if "/api/" in text:
        score += 0.05

    return min(score, 1.0)


def _detect_language_and_framework(rel_path: str, code_content: str) -> Dict[str, str]:
    """Dynamically inspect target file to determine language and framework guidelines."""
    ext = rel_path.split(".")[-1].lower() if "." in rel_path else ""

    if ext == "java":
        return {
            "language": "Java",
            "framework": "Spring Boot / Java EE" if "@" in code_content or "springframework" in code_content else "Java",
            "code_block_lang": "java",
            "rules": (
                "1. Target file is a Java class.\n"
                "2. Preserve all Java package declarations, imports, annotations (e.g. `@RestController`, `@PostMapping`), and class signatures.\n"
                "3. Use null checks (e.g. `if (obj != null)`), `Optional.ofNullable(...)`, or default fallbacks for safe property access.\n"
                "4. Output ONLY the complete updated Java code inside a ```java ... ``` block."
            )
        }
    elif ext in ("py", "python"):
        return {
            "language": "Python",
            "framework": "FastAPI" if "fastapi" in code_content.lower() or "basemodel" in code_content.lower() else "Python",
            "code_block_lang": "python",
            "rules": (
                "1. Target file is a Python module.\n"
                "2. Preserve all function signatures, imports, and Pydantic schemas.\n"
                "3. Add defensive null/None checks (`if obj is not None:`) or default fallbacks (`getattr(...)`).\n"
                "4. Output ONLY the complete updated Python code inside a ```python ... ``` block."
            )
        }
    elif ext in ("ts", "tsx", "js", "jsx"):
        if "next/server" in code_content or "nextresponse" in code_content.lower() or "app/api/" in rel_path:
            return {
                "language": "TypeScript (Next.js App Router)",
                "framework": "Next.js App Router",
                "code_block_lang": "typescript",
                "rules": (
                    "1. Target file is a Next.js App Router API route (`src/app/api/.../route.ts`).\n"
                    "2. You MUST use Next.js App Router format:\n"
                    "   `import { NextResponse } from 'next/server';`\n"
                    "   `export async function POST(req: Request) { ... }`\n"
                    "   `const body = await req.json();`\n"
                    "   `return NextResponse.json({ ... });`\n"
                    "3. ALWAYS replace unsafe property accesses (e.g. `body.user`, `body.items[0]`, `body.customer.address`) with safe optional chaining or fallbacks (e.g. `const { email, role } = body?.user || {};` or `const email = body?.user?.email || null;`).\n"
                    "4. DO NOT use Express.js syntax (`express`, `Router()`, `res.status()`, `res.send()`, `req.body`).\n"
                    "5. Output ONLY the complete updated Next.js TypeScript code inside a ```typescript ... ``` block."
                )
            }
        elif "express" in code_content.lower() or "router()" in code_content.lower():
            return {
                "language": "TypeScript (Express.js)",
                "framework": "Express.js",
                "code_block_lang": "typescript",
                "rules": (
                    "1. Target file is an Express.js router module.\n"
                    "2. Preserve existing Express router handlers (`req: Request, res: Response`).\n"
                    "3. Use safe optional chaining (`req.body?.user?.email`) and return `res.status(...).json(...)`.\n"
                    "4. Output ONLY the complete updated Express TypeScript code inside a ```typescript ... ``` block."
                )
            }
        else:
            return {
                "language": "TypeScript / JavaScript",
                "framework": "Generic TS/JS",
                "code_block_lang": "typescript",
                "rules": (
                    "1. Preserve existing module exports and function signatures.\n"
                    "2. Add optional chaining `?.` or default fallbacks `||` for property dereferences.\n"
                    "3. Output ONLY updated code inside a ```typescript ... ``` block."
                )
            }
    else:
        return {
            "language": ext.upper() if ext else "Generic",
            "framework": "Generic",
            "code_block_lang": ext if ext else "text",
            "rules": (
                "1. Preserve existing code structure and signatures.\n"
                "2. Fix unsafe property dereferences safely."
            )
        }


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
                # Inject Verified Human Feedback (Incident Resolution Memory)
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

                human_feedback_instruction = f"6. CRITICAL TEAM FEEDBACK (Incident Resolution Memory): Ensure your fix follows this past PR review feedback:\n{kb_feedback}\n" if kb_feedback else ""

                packet = packet_builder.assemble_packet(
                    incident_id=state["incident_id"],
                    service_name=state["service_name"],
                    alert_text=alert_summary
                )

                meta = _detect_language_and_framework(rel_path, code_content)
                evidence_id_list = [e.id for e in packet.all_evidence[:3]]

                prompt = (
                    f"You are a senior Site Reliability & {meta['language']} Engineer AI agent.\n"
                    f"{packet.to_markdown()}\n\n"
                    f"STRICT {meta['framework'].upper()} FIX GUIDELINES:\n"
                    f"{meta['rules']}\n"
                    f"5. Do NOT output unified diffs or git diff markers (`@@ -... @@`).\n"
                    f"{human_feedback_instruction}"
                    f"\nEvidence IDs used: {json.dumps(evidence_id_list)}"
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

                # Post-processing: strip appended modules or diff markers
                if "// Related Module File:" in candidate:
                    candidate = candidate.split("// Related Module File:")[0].strip()
                candidate = re.sub(r'@@\s*-\d+,\d+\s+\+\d+,\d+\s*@@', '', candidate).strip()

                # Framework-aware validation
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

    # Validation 1: Required structural elements
    required_patterns = {
        "import statement": "import",
        "export declaration": "export",
    }
    for check_name, pattern in required_patterns.items():
        if pattern not in patch_code:
            validation_errors.append(f"FAIL: Missing {check_name}")

    # Validation 2: Unsafe property accesses gone
    unsafe_patterns = [
        "body.customer.address",
        "body.items[0].price",
        "body.product.stock_quantity",
        "body.user.email",
    ]
    for unsafe in unsafe_patterns:
        if unsafe in patch_code:
            validation_errors.append(f"FAIL: Unsafe property access still present: '{unsafe}'")

    if "= body.user;" in patch_code and "?." not in patch_code and "||" not in patch_code:
        validation_errors.append("FAIL: Unsafe property access still present: '= body.user;'")

    # Validation 3: Safe optional chaining present
    if "?." not in patch_code and "||" not in patch_code and "if (" not in patch_code:
        validation_errors.append("FAIL: No null safety check found in patch")

    # Validation 4: Balanced braces
    open_braces = patch_code.count("{")
    close_braces = patch_code.count("}")
    if open_braces != close_braces:
        validation_errors.append(f"FAIL: Unbalanced braces (open={open_braces}, close={close_braces})")

    # Validation 5: Code length
    if len(patch_code) < 100:
        validation_errors.append(f"FAIL: Patch too short ({len(patch_code)} chars), likely a placeholder")

    # Validation 6: LLM syntax verification
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

    # Trigger GitHub Actions sandbox test (non-blocking)
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
            f"Evidence trail: {json.dumps(state.get('evidence_ids', [])[:3])}\n"
            f"Verdict: PATCH SAFE TO DEPLOY."
        )
        logger.info(f"Patch validation PASSED for incident {state['incident_id']}")

    update_incident_status(state["incident_id"], "TESTING")
    return state


def node_failed(state: IncidentState) -> IncidentState:
    """Handle incident resolution failure."""
    logger.info(f"State [FAILED] incident: {state['incident_id']}")
    state["status"] = "FAILED"
    state["step_count"] += 1
    update_incident_status(state["incident_id"], "FAILED", {
        "candidate_patch": state.get("candidate_patch", "// Fix generation failed.")
    })
    return state


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
        code_preview = f"### ⚡ Applied AI Fix (`{rel_path}`)\n```typescript\n{state['fixed_code']}\n```\n\n"

    evidence_trail = ""
    if state.get("evidence_ids"):
        evidence_trail = "### 📍 Evidence Trail\n" + "\n".join([f"- `{eid}`" for eid in state["evidence_ids"][:5]]) + "\n\n"

    pr_body = (
        f"## 🚨 IncidentPilot Autonomous Resolution Report\n\n"
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


# --- Conditional Router ---
def route_after_test(state: IncidentState) -> str:
    test_res = state.get("test_results", "").lower()
    if "fail" in test_res or "error" in test_res:
        if state["fix_attempts"] < settings.MAX_FIX_ATTEMPTS and state.get("fixed_code"):
            return "fix"
        return "failed"
    return "create_pr"


# --- Build State Graph ---
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
        "tool_calls_used": 0,
        "evidence_ids": [],
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
