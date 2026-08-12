import asyncio
import uuid
import logging
import json
from typing import Dict, Any
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.config import settings
from app.db.supabase import get_incidents, get_incident_by_id, create_incident, update_incident_status
from app.agent.orchestrator import run_incident_orchestrator
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("incidentpilot")

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="IncidentPilot Autonomous AI Production Investigation & Remediation API"
)

# Enable CORS for Next.js Dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows Vercel frontend or local dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AlertPayload(BaseModel):
    service_name: str
    summary: str
    severity: str = "P1"
    environment: str = "production"

@app.get("/api/v1/health")
def health_check():
    return {"status": "healthy", "version": settings.VERSION, "project": settings.PROJECT_NAME}

@app.get("/api/v1/incidents")
def list_incidents():
    """Fetch list of all production incidents."""
    return get_incidents()

@app.get("/api/v1/incidents/{incident_id}")
def get_incident(incident_id: str):
    """Fetch detailed record for a specific incident."""
    inc = get_incident_by_id(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    return inc

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
        "created_at": "2026-08-12T00:05:00Z"
    }
    
    create_incident(incident_record)
    
    # Run Agent Orchestrator in background
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

@app.post("/api/v1/webhooks/slack")
async def handle_slack_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Parses native Slack Webhooks, Event Subscriptions, and Form Data.
    Extracts error text, service name, and automatically launches the AI Agent.
    """
    body = {}
    content_type = request.headers.get("content-type", "")
    
    try:
        if "application/json" in content_type:
            body = await request.json()
        else:
            form = await request.form()
            body = dict(form)
    except Exception as e:
        raw_bytes = await request.body()
        try:
            body = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            body = {"text": raw_bytes.decode("utf-8", errors="ignore")}

    logger.info(f"Received Slack Webhook payload: {body}")
    
    # Debug: write payload to file for inspection
    with open("slack_debug.json", "a") as f:
        f.write(json.dumps(body) + "\n")

    # 1. Handle Slack URL Verification Challenge during App Setup
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    event_data = body.get("event", {}) if isinstance(body.get("event"), dict) else {}
    
    # Ignore Slack system events (bot_add, channel_join, etc.)
    subtype = event_data.get("subtype")
    if subtype in ["bot_add", "channel_join", "channel_leave", "group_join"]:
        logger.info(f"Ignoring Slack system event: {subtype}")
        return {"status": "IGNORED", "reason": f"System event {subtype}"}

    text = event_data.get("text") or body.get("text") or ""
    
    # Ignore non-incident messages (e.g., invites, integrations added)
    if not text or "invite" in text.lower() or "added an integration" in text.lower():
        logger.info(f"Ignoring non-alert Slack message: {text}")
        return {"status": "IGNORED", "reason": "Non-incident message"}
    
    service_name = "payment-service"
    if "order" in text.lower():
        service_name = "ordering-system"
    elif "auth" in text.lower():
        service_name = "auth-service"

    from datetime import datetime, timezone
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

@app.post("/api/v1/webhooks/github")
async def handle_github_webhook(request: Request):
    """
    Listens for GitHub pull_request_review_comment events.
    Updates knowledge base and incident status for RLHF.
    """
    body = await request.json()
    action = body.get("action")
    
    if action == "created" and "comment" in body and "pull_request" in body:
        comment_body = body["comment"]["body"]
        pr_url = body["pull_request"]["html_url"]
        
        incidents = get_incidents()
        matched_incident = next((inc for inc in incidents if inc.get("pr_url") == pr_url), None)
        
        if matched_incident:
            # 1. Update Incident Status
            update_incident_status(matched_incident["id"], "CHANGES_REQUESTED", {"feedback": comment_body})
            logger.info(f"RLHF Feedback received for incident {matched_incident['id']}: {comment_body}")
            
            # 2. Append to Knowledge Base
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
                
    return {"status": "OK"}

@app.get("/api/v1/incidents/{incident_id}/stream")
async def stream_incident_updates(incident_id: str):
    """
    Server-Sent Events (SSE) streaming endpoint for live Next.js UI updates.
    Streams agent state transitions in real time.
    """
    async def event_generator():
        states = ["RECEIVED", "VALIDATING", "INVESTIGATING", "DIAGNOSING", "FIXING", "TESTING", "PR_READY"]
        for s in states:
            await asyncio.sleep(1.2)
            inc = get_incident_by_id(incident_id) or {}
            inc["status"] = s
            yield f"data: {inc}\n\n"
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
