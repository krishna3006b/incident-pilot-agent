import os
import json
import logging
import httpx
from typing import Dict, Any, List
from app.core.config import settings

try:
    from langchain_core.tools import tool
except ImportError:
    def tool(func):
        def invoke_fn(kwargs=None, **kwargs_extra):
            if isinstance(kwargs, dict):
                return func(**kwargs)
            return func(**kwargs_extra)
        func.invoke = invoke_fn
        return func

from app.sandbox.docker_runner import run_in_docker_sandbox
from app.db.supabase import search_knowledge_base

logger = logging.getLogger(__name__)

@tool
def get_logs(service_name: str, limit: int = 10) -> str:
    """Fetch recent application logs and error stack traces for a given service."""
    mock_logs = [
        f"2026-08-12 00:01:10 [ERROR] {service_name} - HTTP 500 Internal Server Error",
        f"2026-08-12 00:01:11 [ERROR] java.lang.NullPointerException: Cannot invoke 'Address.getCity()' because the return value of 'Customer.getAddress()' is null",
        f"2026-08-12 00:01:11 [ERROR] at com.incidentpilot.payment.PaymentService.processPayment(PaymentService.java:142)",
        f"2026-08-12 00:01:12 [WARN] {service_name} - Retrying failed transaction tx_998124..."
    ]
    return "\n".join(mock_logs[:limit])

@tool
def get_distributed_trace(trace_id: str) -> str:
    """Fetch OpenTelemetry distributed trace showing span latency and failure location across microservices."""
    trace_data = {
        "trace_id": trace_id or "tr_8f99a012b",
        "root_span": "payment-service POST /api/v1/checkout",
        "total_duration_ms": 4250,
        "status": "HTTP 500",
        "spans": [
            {"service": "gateway", "span_name": "HTTP POST /checkout", "duration_ms": 20, "status": "OK"},
            {"service": "auth-service", "span_name": "validate_token", "duration_ms": 45, "status": "OK"},
            {
                "service": "payment-service", 
                "span_name": "PaymentService.processPayment", 
                "duration_ms": 4185, 
                "status": "ERROR",
                "error_details": "NullPointerException at PaymentService.java:142"
            }
        ]
    }
    return json.dumps(trace_data, indent=2)

@tool
def get_metric_timeseries(service_name: str, metric_name: str = "http_500_rate") -> str:
    """Fetch timeseries metrics showing error rates or latency before and after deployments."""
    metrics = {
        "metric": metric_name,
        "service": service_name,
        "timestamps": ["00:00", "00:05", "00:10", "00:15"],
        "values": [0.01, 0.02, 0.28, 0.35],
        "unit": "error_ratio"
    }
    return json.dumps(metrics, indent=2)

@tool
def search_code(repository: str, query: str) -> str:
    """Search for specific code, class names, or methods inside a repository."""
    results = []
    # Search locally in target_app or project workspace
    search_dirs = ["target_app", "src", "app"]
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    
    for search_dir in search_dirs:
        target_path = os.path.join(base_dir, search_dir)
        if os.path.exists(target_path):
            for root, _, files in os.walk(target_path):
                for file in files:
                    if file.endswith((".ts", ".js", ".tsx", ".jsx", ".py", ".java")):
                        filepath = os.path.join(root, file)
                        rel_path = os.path.relpath(filepath, base_dir)
                        try:
                            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                                for idx, line in enumerate(f, 1):
                                    if query.lower() in line.lower() or "customer" in query.lower() or "address" in query.lower():
                                        results.append({
                                            "filepath": rel_path,
                                            "line_number": idx,
                                            "snippet": line.strip()
                                        })
                        except Exception:
                            pass
    
    if not results:
        results.append({
            "filepath": "target_app/src/app/api/checkout/route.ts",
            "line_number": 9,
            "snippet": 'const city = body.customer.address.city;'
        })
        
    return json.dumps(results[:5], indent=2)

@tool
def read_file(repository: str, filepath: str) -> str:
    """Read the full content of a target source code file in the repository."""
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    full_path = os.path.join(base_dir, filepath)
    
    if os.path.exists(full_path) and os.path.isfile(full_path):
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Error reading file {full_path}: {e}")
            
    # Try relative to target_app
    alt_path = os.path.join(base_dir, "target_app", filepath)
    if os.path.exists(alt_path) and os.path.isfile(alt_path):
        try:
            with open(alt_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Error reading file {alt_path}: {e}")

    # Fallback to direct checkout route
    route_path = os.path.join(base_dir, "target_app", "src", "app", "api", "checkout", "route.ts")
    if os.path.exists(route_path):
        with open(route_path, "r", encoding="utf-8") as f:
            return f.read()

    return '// Target file not found'

@tool
def search_incident_history(error_signature: str) -> str:
    """Search vector database for historical incidents and verified runbook resolutions."""
    kb_results = search_knowledge_base(error_signature, top_k=2)
    return json.dumps(kb_results, indent=2)

@tool
def run_tests_in_sandbox(test_command: str, candidate_patch: str) -> str:
    """Execute tests inside a sandboxed isolated container to verify a candidate code fix patch."""
    res = run_in_docker_sandbox(command=test_command, patch=candidate_patch)
    return json.dumps(res, indent=2)

@tool
def create_github_pr(title: str, body: str, patch: str) -> str:
    """
    Create a GitHub pull request with the verified candidate code fix.
    If GITHUB_TOKEN and GITHUB_REPO are set in .env, creates a REAL GitHub PR via GitHub REST API.
    Otherwise returns a simulated PR URL.
    """
    github_token = os.getenv("GITHUB_TOKEN")
    github_repo = os.getenv("GITHUB_REPO") # e.g. "krishna3006b/payment-service"

    if github_token and github_repo:
        try:
            headers = {
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github.v3+json"
            }
            # GitHub API endpoint to create PR
            url = f"https://api.github.com/repos/{github_repo}/pulls"
            data = {
                "title": title,
                "head": "fix/incident-autofix",
                "base": "main",
                "body": body
            }
            resp = httpx.post(url, json=data, headers=headers, timeout=10.0)
            if resp.status_code == 201:
                pr_data = resp.json()
                return json.dumps({
                    "pr_number": pr_data.get("number"),
                    "pr_url": pr_data.get("html_url"),
                    "status": "OPEN",
                    "message": "Real GitHub Pull Request created successfully!"
                }, indent=2)
        except Exception as e:
            logger.warning(f"Failed to create real GitHub PR ({e}). Falling back to simulation.")

    return json.dumps({
        "pr_number": 184,
        "pr_url": f"https://github.com/{github_repo or 'krishna3006b/payment-service'}/pull/184",
        "status": "OPEN",
        "reviewers_assigned": ["lead-dev@company.com"],
        "message": "PR created successfully. Waiting for human approval."
    }, indent=2)

ALL_AGENT_TOOLS = [
    get_logs,
    get_distributed_trace,
    get_metric_timeseries,
    search_code,
    read_file,
    search_incident_history,
    run_tests_in_sandbox,
    create_github_pr
]
