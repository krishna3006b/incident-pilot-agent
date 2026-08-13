from app.core.config import settings
from app.agent.graph.states import IncidentState

def route_after_test(state: IncidentState) -> str:
    test_res = state.get("test_results", "").lower()
    if "fail" in test_res or "error" in test_res:
        if state["fix_attempts"] < settings.MAX_FIX_ATTEMPTS and state.get("fixed_code"):
            return "fix"
        return "failed"
    return "create_pr"
