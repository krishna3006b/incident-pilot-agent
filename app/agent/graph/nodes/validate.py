import logging
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
