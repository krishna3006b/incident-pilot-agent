import logging
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
