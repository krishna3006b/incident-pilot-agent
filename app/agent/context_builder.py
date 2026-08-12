import os
import re
import uuid
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from app.agent.knowledge_service import knowledge_service

logger = logging.getLogger(__name__)


# --- Evidence Dataclasses ---
@dataclass
class EvidenceItem:
    """First-class evidence object with unique ID for traceability."""
    id: str
    type: str       # stack_trace, commit, code_chunk, historical, runbook, dependency
    source: str     # slack_webhook, pgvector_rag, github_api, resolution_memory, dependency_graph
    reference: str  # file path, commit sha, incident ID
    content: str
    relevance_score: float = 0.0


@dataclass
class BudgetConstraints:
    """Context and retrieval budget limits."""
    max_files: int = 15
    max_code_lines: int = 1500
    max_log_lines: int = 500
    max_historical_cases: int = 5
    max_git_commits: int = 20
    max_tool_calls: int = 20
    max_read_file_calls: int = 10
    max_search_calls: int = 5
    max_repair_attempts: int = 3
    max_execution_time_sec: int = 120


@dataclass
class ContextPacket:
    """Structured Context Packet with typed evidence fields and budget enforcement."""
    incident_id: str
    service_name: str
    severity: str
    error_summary: str
    stack_trace: str
    target_file: str
    commit_sha: str
    knowledge_version: str

    # Typed evidence buckets
    production_evidence: List[EvidenceItem] = field(default_factory=list)
    deployment_evidence: List[EvidenceItem] = field(default_factory=list)
    code_evidence: List[EvidenceItem] = field(default_factory=list)
    dependency_evidence: List[EvidenceItem] = field(default_factory=list)
    historical_evidence: List[EvidenceItem] = field(default_factory=list)
    runbook_evidence: List[EvidenceItem] = field(default_factory=list)

    constraints: BudgetConstraints = field(default_factory=BudgetConstraints)

    @property
    def all_evidence(self) -> List[EvidenceItem]:
        """Flattened list of all evidence items sorted by relevance."""
        items = (
            self.production_evidence +
            self.deployment_evidence +
            self.code_evidence +
            self.dependency_evidence +
            self.historical_evidence +
            self.runbook_evidence
        )
        items.sort(key=lambda x: x.relevance_score, reverse=True)
        return items

    def to_markdown(self) -> str:
        """Renders Context Packet into structured Markdown for LLM prompt."""
        sections = []

        # Production evidence
        if self.production_evidence:
            lines = []
            for item in self.production_evidence:
                lines.append(f"- **[{item.id}]** [{item.type.upper()}] {item.reference} (Score: {item.relevance_score:.2f})\n  {item.content[:300]}")
            sections.append("### Production Evidence\n" + "\n".join(lines))

        # Deployment evidence
        if self.deployment_evidence:
            lines = []
            for item in self.deployment_evidence:
                lines.append(f"- **[{item.id}]** [{item.type.upper()}] {item.reference} (Score: {item.relevance_score:.2f})\n  {item.content[:300]}")
            sections.append("### Deployment Evidence\n" + "\n".join(lines))

        # Code evidence (RAG)
        if self.code_evidence:
            lines = []
            code_lines_used = 0
            for item in self.code_evidence:
                item_lines = item.content.count('\n') + 1
                if code_lines_used + item_lines > self.constraints.max_code_lines:
                    break
                code_lines_used += item_lines
                lines.append(f"- **[{item.id}]** [{item.type.upper()}] `{item.reference}` (Score: {item.relevance_score:.2f})\n```\n{item.content[:500]}\n```")
            sections.append("### Code Evidence (RAG)\n" + "\n".join(lines))

        # Dependency evidence
        if self.dependency_evidence:
            lines = []
            for item in self.dependency_evidence:
                lines.append(f"- **[{item.id}]** [{item.type.upper()}] {item.reference} (Score: {item.relevance_score:.2f})\n  {item.content[:200]}")
            sections.append("### Dependency Evidence\n" + "\n".join(lines))

        # Historical evidence
        if self.historical_evidence:
            lines = []
            for item in self.historical_evidence[:self.constraints.max_historical_cases]:
                lines.append(f"- **[{item.id}]** [{item.type.upper()}] {item.reference} (Score: {item.relevance_score:.2f})\n  {item.content[:200]}")
            sections.append("### Historical Incident Memory\n" + "\n".join(lines))

        # Runbook evidence
        if self.runbook_evidence:
            lines = []
            for item in self.runbook_evidence:
                lines.append(f"- **[{item.id}]** [{item.type.upper()}] {item.reference} (Score: {item.relevance_score:.2f})\n  {item.content[:200]}")
            sections.append("### Runbook Evidence\n" + "\n".join(lines))

        evidence_text = "\n\n".join(sections) if sections else "No additional evidence retrieved."

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
{evidence_text}

## CONSTRAINTS & BUDGET
- Max Files: {self.constraints.max_files}
- Max Code Lines: {self.constraints.max_code_lines}
- Max Tool Calls Allowed: {self.constraints.max_tool_calls}
- Max Repair Attempts: {self.constraints.max_repair_attempts}
"""

    def to_dashboard_json(self) -> Dict[str, Any]:
        """Renders evidence list for dashboard visual auditability."""
        return {
            'incident_id': self.incident_id,
            'service_name': self.service_name,
            'knowledge_version': self.knowledge_version,
            'target_file': self.target_file,
            'evidence_count': len(self.all_evidence),
            'evidence_trail': [
                {
                    'id': item.id,
                    'type': item.type,
                    'source': item.source,
                    'reference': item.reference,
                    'score': round(item.relevance_score, 2),
                    'content_snippet': item.content[:150]
                }
                for item in self.all_evidence
            ]
        }


# --- Evidence Ranking Engine ---
def compute_evidence_score(
    evidence_type: str,
    alert_text: str,
    target_file: str,
    reference: str,
    content: str
) -> float:
    """
    Dynamic evidence ranking formula:
        score = stack_trace_match  x 3.0
              + service_match      x 2.0
              + deployment_proximity x 1.5
              + symbol_dependency  x 1.0
              + historical_similarity x 0.8
    """
    score = 0.0

    # Stack trace match (file path appears in alert)
    if reference and reference in alert_text:
        score += 3.0
    elif target_file and target_file in (reference or ''):
        score += 2.5

    # Service match
    if evidence_type == 'stack_trace':
        score += 2.0

    # Deployment proximity (commit evidence)
    if evidence_type == 'commit':
        score += 1.5

    # Symbol dependency
    if evidence_type == 'dependency':
        score += 1.0

    # Code chunk relevance (RAG)
    if evidence_type == 'code_chunk':
        score += 1.5

    # Historical similarity
    if evidence_type == 'historical':
        score += 0.8

    # Runbook
    if evidence_type == 'runbook':
        score += 0.5

    # Content relevance boost: error patterns in content
    error_patterns = ['typeerror', 'cannot read', 'cannot destructure', 'null', 'undefined']
    content_lower = content.lower()
    for pattern in error_patterns:
        if pattern in content_lower:
            score += 0.3
            break

    # Normalize to 0..1 range (max theoretical ~8.3)
    return round(min(score / 8.0, 1.0), 2)


# --- Context Packet Builder ---
class ContextPacketBuilder:
    def _persist_evidence(self, incident_id: str, evidence_items: List[EvidenceItem]):
        """Persist evidence items to the evidence table in Supabase."""
        from app.db.supabase import supabase_client as sc
        if not sc or not evidence_items:
            return
        try:
            records = []
            for item in evidence_items:
                records.append({
                    'id': item.id,
                    'incident_id': incident_id,
                    'type': item.type,
                    'source': item.source,
                    'reference': item.reference,
                    'content': item.content[:5000],  # Cap content size
                    'relevance_score': item.relevance_score
                })
            sc.table('evidence').insert(records).execute()
            logger.info(f"Persisted {len(records)} evidence items for incident {incident_id}")
        except Exception as e:
            logger.warning(f"Could not persist evidence to DB: {e}")

    def assemble_packet(
        self,
        incident_id: str,
        service_name: str,
        alert_text: str,
        commit_sha: str = "v1.8.3",
        knowledge_version: str = "kv-42"
    ) -> ContextPacket:
        """
        Assembles a ranked Context Packet by gathering evidence from
        logs, git diffs, RAG, dependency graph, and incident memory.
        """
        # 1. Stack Trace Extraction - find target file
        target_file = 'src/app/api/discount/route.ts'
        file_match = re.search(r'([a-zA-Z0-9_/.\-]+\.(?:ts|tsx|js|py|java))(?::\d+)?', alert_text)
        if file_match:
            target_file = file_match.group(1)

        production_evidence: List[EvidenceItem] = []
        deployment_evidence: List[EvidenceItem] = []
        code_evidence: List[EvidenceItem] = []
        dependency_evidence: List[EvidenceItem] = []
        historical_evidence: List[EvidenceItem] = []
        runbook_evidence: List[EvidenceItem] = []

        # 2. Production Evidence - Stack Trace
        st_score = compute_evidence_score('stack_trace', alert_text, target_file, target_file, alert_text)
        production_evidence.append(EvidenceItem(
            id=f"ev_{uuid.uuid4().hex[:6]}",
            type='stack_trace',
            source='slack_webhook',
            reference=f"{target_file}:line_1",
            content=alert_text[:1000],
            relevance_score=st_score
        ))

        # 3. Deployment Evidence - Recent commits (simulated deployment diff)
        dep_score = compute_evidence_score('commit', alert_text, target_file, commit_sha, f'Deployment {commit_sha}')
        deployment_evidence.append(EvidenceItem(
            id=f"ev_{uuid.uuid4().hex[:6]}",
            type='commit',
            source='github_api',
            reference=f"{commit_sha} (recent deploy)",
            content=f"Recent deployment at {commit_sha}. Changes detected near {target_file}.",
            relevance_score=dep_score
        ))

        # 4. Code RAG Evidence
        code_chunks = knowledge_service.search_code(query=alert_text, top_k=3)
        for chunk in code_chunks:
            chunk_ref = chunk.get('file_path', target_file)
            chunk_content = chunk.get('content', '')
            rag_score = compute_evidence_score('code_chunk', alert_text, target_file, chunk_ref, chunk_content)
            code_evidence.append(EvidenceItem(
                id=f"ev_{uuid.uuid4().hex[:6]}",
                type='code_chunk',
                source='pgvector_rag',
                reference=chunk_ref,
                content=chunk_content,
                relevance_score=max(rag_score, chunk.get('similarity', 0.5))
            ))

        # 5. Dependency Evidence
        # Extract likely symbol name from target file
        target_symbol = os.path.splitext(os.path.basename(target_file))[0].upper() if target_file else 'POST'
        deps = knowledge_service.get_dependencies(target_symbol)
        for dep in deps:
            dep_content = f"{dep.get('source', '')} -> {dep.get('relationship', '')} -> {dep.get('target', '')}"
            dep_ev_score = compute_evidence_score('dependency', alert_text, target_file, dep.get('target', ''), dep_content)
            dependency_evidence.append(EvidenceItem(
                id=f"ev_{uuid.uuid4().hex[:6]}",
                type='dependency',
                source='dependency_graph',
                reference=dep.get('target', 'unknown'),
                content=dep_content,
                relevance_score=dep_ev_score
            ))

        # 6. Historical Incident Memory
        past_incidents = knowledge_service.get_historical_incident_memory(query=alert_text, top_k=3)
        for inc in past_incidents:
            hist_content = f"Root Cause: {inc.get('root_cause')}\nStatus: {inc.get('status')}"
            hist_score = compute_evidence_score('historical', alert_text, target_file, str(inc.get('incident_id', '')), hist_content)
            historical_evidence.append(EvidenceItem(
                id=f"ev_{uuid.uuid4().hex[:6]}",
                type='historical',
                source='resolution_memory',
                reference=f"Incident #{str(inc.get('incident_id', ''))[:8]}",
                content=hist_content,
                relevance_score=hist_score
            ))

        # 7. Runbook Evidence
        runbooks = knowledge_service.get_runbook_entries(service_name)
        for rb in runbooks:
            rb_content = rb.get('content', '') if isinstance(rb, dict) else str(rb)
            rb_title = rb.get('title', 'Runbook') if isinstance(rb, dict) else 'Runbook'
            rb_score = compute_evidence_score('runbook', alert_text, target_file, rb_title, rb_content)
            runbook_evidence.append(EvidenceItem(
                id=f"ev_{uuid.uuid4().hex[:6]}",
                type='runbook',
                source='knowledge_base',
                reference=rb_title,
                content=rb_content[:500],
                relevance_score=rb_score
            ))

        # Build packet
        packet = ContextPacket(
            incident_id=incident_id,
            service_name=service_name,
            severity='P1',
            error_summary=alert_text.split('\n')[0][:120],
            stack_trace=alert_text[:2000],
            target_file=target_file,
            commit_sha=commit_sha,
            knowledge_version=knowledge_version,
            production_evidence=production_evidence,
            deployment_evidence=deployment_evidence,
            code_evidence=code_evidence,
            dependency_evidence=dependency_evidence,
            historical_evidence=historical_evidence,
            runbook_evidence=runbook_evidence
        )

        # Budget enforcement: truncate evidence buckets to budget limits
        packet.code_evidence = packet.code_evidence[:packet.constraints.max_files]
        packet.historical_evidence = packet.historical_evidence[:packet.constraints.max_historical_cases]

        # Persist evidence to DB
        self._persist_evidence(incident_id, packet.all_evidence)

        logger.info(f"Assembled ContextPacket for incident {incident_id}:\n{packet.to_markdown()}")
        return packet


# Singleton Instance
packet_builder = ContextPacketBuilder()
