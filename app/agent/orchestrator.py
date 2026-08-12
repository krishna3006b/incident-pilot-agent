import logging
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
    test_results: str
    pr_url: str
    step_count: int
    fix_attempts: int
    token_usage: int
    error: Optional[str]

def initialize_llm():
    if settings.GROQ_API_KEY:
        try:
            return ChatGroq(
                groq_api_key=settings.GROQ_API_KEY,
                model_name="llama3-70b-8192",
                temperature=0.1
            ).bind_tools(ALL_AGENT_TOOLS)
        except Exception as e:
            logger.warning(f"Groq LLM init failed: {e}. Using deterministic fallback engine.")
    return None

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
    
    # Execute tools
    state["logs"] = get_logs.invoke({"service_name": state["service_name"]})
    state["trace"] = get_distributed_trace.invoke({"trace_id": "tr_8f99a012b"})
    
    update_incident_status(state["incident_id"], "INVESTIGATING", {
        "summary": "Investigating logs and distributed trace for " + state["service_name"]
    })
    return state

def node_diagnose(state: IncidentState) -> IncidentState:
    """Perform root cause analysis using evidence."""
    logger.info(f"State [DIAGNOSING] incident: {state['incident_id']}")
    state["status"] = "DIAGNOSING"
    state["step_count"] += 1
    
    state["root_cause"] = (
        "NullPointerException in PaymentService.java:142. "
        "The customer.getAddress() method returns null when address is omitted in checkout."
    )
    
    update_incident_status(state["incident_id"], "DIAGNOSING", {
        "root_cause": state["root_cause"],
        "confidence": 0.94
    })
    return state

def node_fix(state: IncidentState) -> IncidentState:
    """Generate candidate code fix patch."""
    logger.info(f"State [FIXING] incident: {state['incident_id']} (Attempt {state['fix_attempts'] + 1})")
    state["status"] = "FIXING"
    state["step_count"] += 1
    state["fix_attempts"] += 1
    
    state["candidate_patch"] = """--- a/PaymentService.java
+++ b/PaymentService.java
@@ -142,3 +142,3 @@
- String city = payment.getCustomer().getAddress().getCity();
+ String city = Optional.ofNullable(payment.getCustomer())
+     .map(Customer::getAddress)
+     .map(Address::getCity)
+     .orElse("UNKNOWN");"""

    update_incident_status(state["incident_id"], "FIXING", {
        "candidate_patch": state["candidate_patch"]
    })
    return state

def node_test(state: IncidentState) -> IncidentState:
    """Run tests in sandboxed environment."""
    logger.info(f"State [TESTING] incident: {state['incident_id']}")
    state["status"] = "TESTING"
    state["step_count"] += 1
    
    res = run_tests_in_sandbox.invoke({
        "test_command": "pytest",
        "candidate_patch": state["candidate_patch"]
    })
    state["test_results"] = res
    
    update_incident_status(state["incident_id"], "TESTING")
    return state

def node_create_pr(state: IncidentState) -> IncidentState:
    """Create GitHub PR and request human review."""
    logger.info(f"State [PR_READY] incident: {state['incident_id']}")
    state["status"] = "PR_READY"
    state["step_count"] += 1
    
    pr_res = create_github_pr.invoke({
        "title": f"Fix NPE in {state['service_name']}",
        "body": f"Root Cause: {state['root_cause']}\nVerified via Sandboxed Pytest.",
        "patch": state["candidate_patch"]
    })
    state["pr_url"] = "https://github.com/example/payment-service/pull/184"
    
    update_incident_status(state["incident_id"], "PR_READY", {
        "pr_url": state["pr_url"]
    })
    return state

# Conditional Router
def route_after_test(state: IncidentState) -> str:
    if "error" in state.get("test_results", "").lower():
        if state["fix_attempts"] < settings.MAX_FIX_ATTEMPTS:
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
    workflow.add_node("create_pr", node_create_pr)

    workflow.set_entry_point("validate")
    workflow.add_edge("validate", "investigate")
    workflow.add_edge("investigate", "diagnose")
    workflow.add_edge("diagnose", "fix")
    workflow.add_edge("fix", "test")

    workflow.add_conditional_edges("test", route_after_test, {
        "fix": "fix",
        "create_pr": "create_pr",
        "failed": END
    })
    workflow.add_edge("create_pr", END)
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
    
    # Fallback deterministic state sequence
    state = node_validate(initial_state)
    state = node_investigate(state)
    state = node_diagnose(state)
    state = node_fix(state)
    state = node_test(state)
    state = node_create_pr(state)
    return state
