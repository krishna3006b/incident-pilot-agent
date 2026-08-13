import logging
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
