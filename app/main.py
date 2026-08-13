import asyncio
import uuid
import logging
import json
import os
import hmac
import hashlib
import time
from typing import Dict, Any
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.config import settings
from app.db.supabase import get_incidents, get_incident_by_id, create_incident, update_incident_status, supabase_client
from app.agent.graph.workflow import run_incident_orchestrator
from app.agent.knowledge_service import knowledge_service
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("incidentpilot")

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="IncidentPilot Autonomous AI Production Investigation & Remediation API"
)

allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
if allowed_origins_env:
    origins = [origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()]
else:
    origins = [
        "http://localhost:3000",
        "https://incident-pilot-dashboard.vercel.app",
        "https://incident-pilot.vercel.app"
    ]

# Enable CORS for Next.js Dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AlertPayload(BaseModel):
    service_name: str
    summary: str
    severity: str = "P1"
    environment: str = "production"


# ============================================================
# Health & Incident CRUD Endpoints
# ============================================================

@app.get("/api/v1/health")
def health_check():
    return {"status": "healthy", "version": settings.VERSION, "project": settings.PROJECT_NAME}


@app.get("/api/v1/incidents")
def list_incidents():
    """Fetch list of all production incidents."""
    return get_incidents()


@app.get("/api/v1/incidents/stream_all")
async def stream_all_incidents():
    """SSE streaming endpoint for all incidents to replace 2-second polling."""
    async def event_generator():
        last_data = None
        while True:
            current_data = get_incidents()
            if current_data != last_data:
                yield f"data: {json.dumps(current_data)}\n\n"
                last_data = current_data
            await asyncio.sleep(2)
    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/v1/incidents/{incident_id}")
def get_incident(incident_id: str):
    """Fetch detailed record for a specific incident."""
    inc = get_incident_by_id(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    return inc


# ============================================================
# Alert Ingestion
# ============================================================

@app.post("/api/v1/alerts")
def trigger_alert(payload: AlertPayload, background_tasks: BackgroundTasks):
    """
    Ingest a production alert (e.g. from Slack/Datadog).
    Stores incident record and triggers LangGraph agent orchestrator.
    """
    incident_id = str(uuid.uuid4())
    incident_record = {
        "id": incident_id,
        "incident_number": 185,
        "title": f"{payload.summary} in {payload.service_name}",
        "service_name": payload.service_name,
        "severity": payload.severity,
        "status": "RECEIVED",
        "confidence": 0.0,
        "summary": payload.summary,
        "root_cause": "",
        "candidate_patch": "",
        "pr_url": "",
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    create_incident(incident_record)

    background_tasks.add_task(
        run_incident_orchestrator,
        incident_id,
        payload.service_name,
        payload.summary
    )

    return {
        "status": "ACCEPTED",
        "incident_id": incident_id,
        "message": f"Agent dispatched for {payload.service_name}"
    }


# ============================================================
# Slack Webhook
# ============================================================

@app.post("/api/v1/webhooks/slack")
async def handle_slack_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Parses native Slack Webhooks, Event Subscriptions, and Form Data.
    Extracts error text, service name, and automatically launches the AI Agent.
    """
    raw_bytes = await request.body()
    
    if os.getenv("ENFORCE_WEBHOOK_SECRETS", "true").lower() == "true":
        slack_signature = request.headers.get("X-Slack-Signature")
        slack_timestamp = request.headers.get("X-Slack-Request-Timestamp")
        
        if not slack_signature or not slack_timestamp:
            logger.warning("Rejected unauthorized Slack webhook request")
            raise HTTPException(status_code=401, detail="Unauthorized: Missing Slack Signature")
        try:
            if abs(time.time() - int(slack_timestamp)) > 60 * 5:
                raise HTTPException(status_code=401, detail="Unauthorized: Request too old")
        except ValueError:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid Timestamp")
            
        slack_secret = os.getenv("SLACK_SIGNING_SECRET")
        if not slack_secret:
            logger.error("SLACK_SIGNING_SECRET is not configured on the server")
            raise HTTPException(status_code=500, detail="Server configuration error")
            
        sig_basestring = f"v0:{slack_timestamp}:{raw_bytes.decode('utf-8')}"
        my_signature = "v0=" + hmac.new(
            slack_secret.encode(),
            sig_basestring.encode(),
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(my_signature, slack_signature):
            logger.warning("Rejected unauthorized Slack webhook request: Invalid HMAC")
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid Slack Signature")

    body = {}
    content_type = request.headers.get("content-type", "")

    try:
        if "application/json" in content_type:
            body = await request.json()
        else:
            form = await request.form()
            body = dict(form)
    except Exception as e:
        try:
            body = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            body = {"text": raw_bytes.decode("utf-8", errors="ignore")}

    logger.info(f"Received Slack Webhook payload: {body}")

    # Handle Slack URL Verification Challenge
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    event_data = body.get("event", {}) if isinstance(body.get("event"), dict) else {}

    # Ignore Slack system events
    subtype = event_data.get("subtype")
    if subtype in ["bot_add", "channel_join", "channel_leave", "group_join"]:
        logger.info(f"Ignoring Slack system event: {subtype}")
        return {"status": "IGNORED", "reason": f"System event {subtype}"}

    text = event_data.get("text") or body.get("text") or ""
    
    # Convert Slack emoji shortcodes to actual Unicode emojis
    emoji_map = {
        ":rotating_light:": "🚨",
        ":warning:": "⚠️",
        ":fire:": "🔥",
        ":boom:": "💥",
        ":bug:": "🐛",
        ":x:": "❌"
    }
    for code, emoji in emoji_map.items():
        text = text.replace(code, emoji)

    # Ignore non-incident messages
    if not text or "invite" in text.lower() or "added an integration" in text.lower():
        logger.info(f"Ignoring non-alert Slack message: {text}")
        return {"status": "IGNORED", "reason": "Non-incident message"}

    service_name = "unknown-service"
    # Try to extract service name from alert text
    for word in text.lower().split():
        if "-service" in word or "-system" in word:
            service_name = word.strip('*:.')
            break

    incident_id = str(uuid.uuid4())
    inc_num = 186 + len(get_incidents())

    clean_title = text.replace(":rotating_light:", "").replace("*", "").split("\n")[0].strip()
    if not clean_title:
        clean_title = f"HTTP 500 Spike in {service_name}"

    incident_record = {
        "id": incident_id,
        "incident_number": inc_num,
        "title": clean_title[:80],
        "service_name": service_name,
        "severity": "P1",
        "status": "RECEIVED",
        "confidence": 0.0,
        "summary": text,
        "root_cause": "",
        "candidate_patch": "",
        "pr_url": "",
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    create_incident(incident_record)
    background_tasks.add_task(run_incident_orchestrator, incident_id, service_name, text)

    return {
        "status": "OK",
        "incident_id": incident_id,
        "message": "Slack alert parsed and AI Agent dispatched."
    }


# ============================================================
# GitHub Webhook (Verified Human Feedback + PR Merge)
# ============================================================

@app.post("/api/v1/webhooks/github")
async def handle_github_webhook(request: Request):
    """
    Listens for GitHub pull_request_review_comment events.
    Updates Incident Resolution Memory and incident status.
    """
    raw_bytes = await request.body()
    
    if os.getenv("ENFORCE_WEBHOOK_SECRETS", "true").lower() == "true":
        github_signature = request.headers.get("X-Hub-Signature-256")
        if not github_signature:
            logger.warning("Rejected unauthorized GitHub webhook request")
            raise HTTPException(status_code=401, detail="Unauthorized: Missing GitHub Signature")
            
        github_secret = os.getenv("GITHUB_WEBHOOK_SECRET")
        if not github_secret:
            logger.error("GITHUB_WEBHOOK_SECRET is not configured on the server")
            raise HTTPException(status_code=500, detail="Server configuration error")
            
        expected_signature = "sha256=" + hmac.new(
            github_secret.encode(),
            raw_bytes,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(expected_signature, github_signature):
            logger.warning("Rejected unauthorized GitHub webhook request: Invalid HMAC")
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid GitHub Signature")

    try:
        body = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        body = {}

    action = body.get("action")

    # Handle PR Comment Feedback (Verified Human Feedback)
    if action == "created" and "comment" in body and "pull_request" in body:
        comment_body = body["comment"]["body"]
        pr_url = body["pull_request"]["html_url"]

        incidents = get_incidents()
        matched_incident = next((inc for inc in incidents if inc.get("pr_url") == pr_url), None)

        if matched_incident:
            update_incident_status(matched_incident["id"], "CHANGES_REQUESTED")
            logger.info(f"Verified Human Feedback received for incident {matched_incident['id']}: {comment_body}")

            kb_path = "knowledge_base.json"
            kb = []
            if os.path.exists(kb_path):
                try:
                    with open(kb_path, "r") as f:
                        kb = json.load(f)
                except Exception:
                    pass
            kb.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "incident": matched_incident["title"],
                "feedback": comment_body
            })
            with open(kb_path, "w") as f:
                json.dump(kb, f, indent=2)

    # Handle PR Merge -> Set Status to RESOLVED
    elif action == "closed" and body.get("pull_request", {}).get("merged") is True:
        pr_url = body["pull_request"]["html_url"]
        incidents = get_incidents()
        matched_incident = next((inc for inc in incidents if inc.get("pr_url") == pr_url), None)
        if matched_incident:
            update_incident_status(matched_incident["id"], "RESOLVED")
            logger.info(f"PR Merged! Incident {matched_incident['id']} status updated to RESOLVED.")

    return {"status": "OK"}


# ============================================================
# Sandbox Test Result Callback
# ============================================================

@app.post("/api/v1/webhooks/sandbox-result")
async def handle_sandbox_result(request: Request):
    """
    Callback endpoint for GitHub Actions sandbox-test.yml.
    Receives test verdict (PASS/FAIL) and authenticates via secret.
    """
    secret = request.headers.get("X-Sandbox-Secret")
    expected_secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    
    if not expected_secret:
        logger.error("GITHUB_WEBHOOK_SECRET is not configured on the server")
        raise HTTPException(status_code=500, detail="Server configuration error")
    
    if secret != expected_secret:
        logger.warning(f"Rejected unauthorized sandbox webhook request. Received secret length: {len(secret) if secret else 0}, Expected length: {len(expected_secret) if expected_secret else 0}")
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    body = await request.json()
    incident_id = body.get("incident_id")
    sandbox_run_id = body.get("sandbox_run_id")
    verdict = body.get("verdict", "UNKNOWN")
    run_url = body.get("run_url", "")

    logger.info(f"Sandbox result for run {sandbox_run_id} (incident {incident_id}): verdict={verdict}")

    if sandbox_run_id:
        from app.db.supabase import get_sandbox_run, update_sandbox_run
        
        run_record = get_sandbox_run(sandbox_run_id)
        if not run_record:
            logger.warning(f"Webhook received for unknown sandbox run: {sandbox_run_id}")
            raise HTTPException(status_code=404, detail="Sandbox run not found")
            
        if run_record.get("incident_id") != incident_id:
            logger.warning(f"Webhook incident_id mismatch for run {sandbox_run_id}")
            raise HTTPException(status_code=403, detail="Incident ID mismatch")
            
        update_sandbox_run(sandbox_run_id, {
            "status": verdict,
            "verdict": verdict,
            "run_url": run_url,
            "completed_at": datetime.now(timezone.utc).isoformat()
        })

    return {"status": "OK", "verdict": verdict}


# ============================================================
# Repository Knowledge Stats API (Phase 7)
# ============================================================

@app.get("/api/v1/knowledge/stats")
def get_knowledge_stats():
    """
    Returns live repository knowledge index statistics.
    Used by the dashboard's Repository Knowledge Status card.
    """
    manifest = knowledge_service.get_manifest()
    return {
        "repository": manifest.get("name", "unknown"),
        "language": manifest.get("language", "Unknown"),
        "framework": manifest.get("framework", "Unknown"),
        "last_indexed_sha": manifest.get("last_indexed_sha"),
        "symbol_count": manifest.get("symbol_count", 0),
        "embedding_count": manifest.get("embedding_count", 0),
        "status": manifest.get("status", "Not Indexed"),
        "knowledge_version": manifest.get("last_indexed_sha", "not-indexed")[:8] if manifest.get("last_indexed_sha") else "not-indexed"
    }


# ============================================================
# Evidence Trail API (Phase 7)
# ============================================================

@app.get("/api/v1/incidents/{incident_id}/evidence")
def get_incident_evidence(incident_id: str):
    """
    Returns the evidence trail for a specific incident.
    Fetches from the evidence table in Supabase.
    """
    if supabase_client:
        try:
            res = supabase_client.table("evidence").select("*").eq("incident_id", incident_id).order("relevance_score", desc=True).execute()
            if res.data:
                return {"incident_id": incident_id, "evidence": res.data}
        except Exception as e:
            logger.warning(f"Error fetching evidence for incident {incident_id}: {e}")

    # Fallback: return empty
    return {"incident_id": incident_id, "evidence": []}


# ============================================================
# SSE Streaming for Individual Incident
# ============================================================

@app.get("/api/v1/incidents/{incident_id}/stream")
async def stream_incident_updates(incident_id: str):
    """
    Server-Sent Events (SSE) streaming endpoint for live Next.js UI updates.
    Streams actual agent state transitions from the database in real time.
    """
    async def event_generator():
        last_state_json = None
        while True:
            inc = get_incident_by_id(incident_id)
            if inc:
                current_state_json = json.dumps(inc)
                if current_state_json != last_state_json:
                    yield f"data: {current_state_json}\n\n"
                    last_state_json = current_state_json
                    
                if inc.get("status") in ["RESOLVED", "CHANGES_REQUESTED", "FAILED"]:
                    break
                    
            await asyncio.sleep(1.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
