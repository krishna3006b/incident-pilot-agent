import logging
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
        try:
            return orchestrator_graph.invoke(initial_state)
        except Exception as e:
            logger.error(f"Agent orchestration failed for incident {incident_id}: {e}")
            from app.db.supabase import update_incident_status
            update_incident_status(incident_id, "FAILED")
            initial_state["status"] = "FAILED"
            initial_state["error"] = str(e)
            return initial_state

    try:
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
    except Exception as e:
        logger.error(f"Agent orchestration failed for incident {incident_id}: {e}")
        from app.db.supabase import update_incident_status
        update_incident_status(incident_id, "FAILED")
        initial_state["status"] = "FAILED"
        initial_state["error"] = str(e)
        return initial_state
