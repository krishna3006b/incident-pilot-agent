from typing import List, Optional, TypedDict

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
    sandbox_status: str
    pr_url: str
    step_count: int
    fix_attempts: int
    token_usage: int
    tool_calls_used: int
    evidence_ids: List[str]
    error: Optional[str]

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

    def summary(self) -> dict:
        return {
            "total_calls": self.total_calls,
            "max_tool_calls": self.max_tool_calls,
            "read_file_calls": self.read_file_calls,
            "search_calls": self.search_calls
        }
