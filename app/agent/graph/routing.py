from app.core.config import settings
from app.agent.graph.states import IncidentState

def route_after_test(state: IncidentState) -> str:
    sandbox_status = state.get("sandbox_status", "")
    
    if sandbox_status == "PASS":
        return "create_pr"
        
    if sandbox_status == "FAIL":
        if state["fix_attempts"] < settings.MAX_FIX_ATTEMPTS and state.get("fixed_code"):
            return "fix"
        return "failed"
        
    # Any other status (SYSTEM_FAILED, TIMEOUT, PENDING, RUNNING, UNKNOWN, empty) fails closed.
    return "failed"
