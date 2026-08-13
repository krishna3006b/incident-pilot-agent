import logging
import json
from langchain_core.messages import SystemMessage, HumanMessage
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState
from app.agent.graph.utils import initialize_llm, _calculate_evidence_confidence

logger = logging.getLogger(__name__)

def node_diagnose(state: IncidentState) -> IncidentState:
    """Perform root cause analysis with structured JSON output and evidence IDs."""
    logger.info(f"State [DIAGNOSING] incident: {state['incident_id']}")
    state["status"] = "DIAGNOSING"
    state["step_count"] += 1

    llm = initialize_llm()
    alert_summary = state.get("alert_summary", "")
    confidence = 0.0
    context_markdown = state.get("logs", "")
    evidence_ids_str = json.dumps(state.get("evidence_ids", []))

    if not context_markdown:
        state["status"] = "DIAGNOSIS_FAILED"
        state["root_cause"] = "DIAGNOSIS_FAILED: Knowledge context packet is missing."
        return state

    if llm:
        prompt = (
            f"{context_markdown}\n\n"
            f"Available evidence IDs: {evidence_ids_str}\n"
            f"Select only the IDs that directly support the diagnosis.\n\n"
            f"Respond in EXACTLY this JSON format (no extra text, no markdown fences):\n"
            f'{{"root_cause": "<2 concise sentences identifying the exact root cause>", '
            f'"confidence": <decimal 0.0-1.0>, '
            f'"evidence": ["<ev_id_1>", "<ev_id_2>"]}}'
        )

        def parse_json_response(llm_to_use) -> bool:
            nonlocal confidence
            try:
                resp = llm_to_use.invoke([
                    SystemMessage(content="You are an expert SRE AI agent. Respond ONLY with valid JSON."),
                    HumanMessage(content=prompt)
                ])
                content = str(resp.content).strip()

                if content.startswith('```'):
                    content = content.split('```')[1]
                    if content.startswith('json'):
                        content = content[4:]
                    content = content.strip()

                result = json.loads(content)
                state["root_cause"] = result.get("root_cause", content)
                confidence = float(result.get("confidence", 0.0))
                confidence = max(0.0, min(1.0, confidence))
                
                raw_chosen = result.get('evidence', [])
                if isinstance(raw_chosen, list):
                    valid_chosen = [e for e in raw_chosen if e in state.get("evidence_ids", [])]
                    state["evidence_ids"] = valid_chosen
                    
                logger.info(f"Structured diagnosis parsed: confidence={confidence}, evidence={state['evidence_ids']}")
                return True
            except Exception as parse_err:
                logger.warning(f"Failed to parse structured JSON: {parse_err}")
                return False

        try:
            success = parse_json_response(llm)
        except Exception as e:
            logger.warning(f"Groq diagnosis failed with primary model: {e}")
            success = False

        if not success:
            logger.info("Attempting diagnosis fallback with `llama-3.1-8b-instant`...")
            fallback_llm = initialize_llm(model_name="llama-3.1-8b-instant")
            if fallback_llm:
                fallback_success = parse_json_response(fallback_llm)
                if not fallback_success:
                    logger.warning("Fallback diagnosis also failed.")
                    state["status"] = "DIAGNOSIS_FAILED"
                    state["root_cause"] = "DIAGNOSIS_FAILED: Model failed to generate valid root cause JSON."
                    return state
            else:
                state["status"] = "DIAGNOSIS_FAILED"
                state["root_cause"] = "DIAGNOSIS_FAILED: Fallback model unavailable."
                return state

    if confidence < 0.1:
        confidence = _calculate_evidence_confidence(alert_summary, "", "", state.get("root_cause", ""))

    state["confidence"] = confidence

    update_incident_status(state["incident_id"], "DIAGNOSING", {
        "root_cause": state["root_cause"],
        "confidence": confidence
    })
    return state
