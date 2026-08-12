import re
import uuid
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from app.agent.knowledge_service import knowledge_service

@dataclass
class EvidenceItem:
    id: str
    type: str  # stack_trace, commit, code_chunk, historical, runbook
    source: str
    reference: str
    content: str
    relevance_score: float

@dataclass
class ContextPacket:
    incident_id: str
    service_name: str
    severity: str
    error_summary: str
    stack_trace: str
    target_file: str
    commit_sha: str
    knowledge_version: str
    evidence_items: List[EvidenceItem] = field(default_factory=list)
    budget_limits: Dict[str, int] = field(default_factory=lambda: {
        "max_files": 15,
        "max_code_lines": 1500,
        "max_log_lines": 500,
        "max_tool_calls": 20,
        "max_read_file_calls": 10,
        "max_search_calls": 5
    })

    def to_markdown(self) -> str:
        """Renders Context Packet into structured Markdown for LLM prompt."""
        evidence_text = ""
        for item in self.evidence_items:
            evidence_text += f"\n### [{item.type.upper()}] {item.reference} (Score: {item.relevance_score:.2f})\n{item.content}\n"

        return f"""# INCIDENT CONTEXT PACKET
**Incident ID:** {self.incident_id}
**Service:** {self.service_name}
**Severity:** {self.severity}
**Knowledge Version:** {self.knowledge_version}
**Target File / Stack Anchor:** {self.target_file}
**Commit SHA:** {self.commit_sha}

## ERROR SUMMARY
{self.error_summary}

## STACK TRACE EVIDENCE
{self.stack_trace}

## RANKED CONTEXT & EVIDENCE
{evidence_text if evidence_text else "No additional evidence retrieved."}

## CONSTRAINTS & BUDGET
- Max Files: {self.budget_limits['max_files']}
- Max Tool Calls Allowed: {self.budget_limits['max_tool_calls']}
- Max Repair Attempts: 3
"""

    def to_dashboard_json(self) -> Dict[str, Any]:
        """Renders evidence list for dashboard visual auditability."""
        return {
            "incident_id": self.incident_id,
            "service_name": self.service_name,
            "knowledge_version": self.knowledge_version,
            "target_file": self.target_file,
            "evidence_count": len(self.evidence_items),
            "evidence_trail": [
                {
                    "id": item.id,
                    "type": item.type,
                    "source": item.source,
                    "reference": item.reference,
                    "score": item.relevance_score,
                    "content_snippet": item.content[:150]
                }
                for item in self.evidence_items
            ]
        }

class ContextPacketBuilder:
    def assemble_packet(
        self,
        incident_id: str,
        service_name: str,
        alert_text: str,
        commit_sha: str = "v1.8.3",
        knowledge_version: str = "kv-42"
    ) -> ContextPacket:
        """
        Assembles a ranked Context Packet by gathering evidence from logs, git diffs, RAG, and memory.
        """
        # 1. Stack Trace Extraction
        target_file = "src/app/api/discount/route.ts"
        file_match = re.search(r'([a-zA-Z0-9_/-]+\.(?:ts|tsx|js|py|java))(?::\d+)?', alert_text)
        if file_match:
            target_file = file_match.group(1)

        evidence_list: List[EvidenceItem] = []

        # 2. Add Stack Trace Evidence Item
        evidence_list.append(EvidenceItem(
            id=f"ev_{uuid.uuid4().hex[:6]}",
            type="stack_trace",
            source="slack_webhook",
            reference=f"{target_file}:line_1",
            content=alert_text,
            relevance_score=0.95
        ))

        # 3. Add Code RAG Evidence
        code_chunks = knowledge_service.search_code(query=alert_text, top_k=2)
        for chunk in code_chunks:
            evidence_list.append(EvidenceItem(
                id=f"ev_{uuid.uuid4().hex[:6]}",
                type="code_chunk",
                source="pgvector_rag",
                reference=chunk.get("file_path", target_file),
                content=chunk.get("content", ""),
                relevance_score=chunk.get("similarity", 0.85)
            ))

        # 4. Add Historical Incident Memory Evidence
        past_incidents = knowledge_service.get_historical_incident_memory(query=alert_text, top_k=2)
        for inc in past_incidents:
            evidence_list.append(EvidenceItem(
                id=f"ev_{uuid.uuid4().hex[:6]}",
                type="historical",
                source="resolution_memory",
                reference=f"Incident #{inc.get('incident_id', '')[:8]}",
                content=f"Root Cause: {inc.get('root_cause')}\nStatus: {inc.get('status')}",
                relevance_score=0.75
            ))

        # Sort evidence items by relevance score descending
        evidence_list.sort(key=lambda x: x.relevance_score, reverse=True)

        return ContextPacket(
            incident_id=incident_id,
            service_name=service_name,
            severity="P1",
            error_summary=alert_text.split('\n')[0][:120],
            stack_trace=alert_text,
            target_file=target_file,
            commit_sha=commit_sha,
            knowledge_version=knowledge_version,
            evidence_items=evidence_list
        )

# Singleton Instance
packet_builder = ContextPacketBuilder()
