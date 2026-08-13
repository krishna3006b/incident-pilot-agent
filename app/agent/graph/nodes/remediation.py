import logging
import json
import os
import re
import time
from langchain_core.messages import SystemMessage, HumanMessage
from app.db.supabase import update_incident_status
from app.agent.graph.states import IncidentState
from app.agent.graph.utils import initialize_llm, find_and_read_target_code, _detect_language_and_framework
from app.agent.context_builder import packet_builder

logger = logging.getLogger(__name__)

def node_fix(state: IncidentState) -> IncidentState:
    """Generate candidate code fix patch with retrieval budget and evidence tracing."""
    state["status"] = "FIXING"
    state["step_count"] += 1
    state["fix_attempts"] += 1

    alert_summary = state.get("alert_summary", "")
    rel_path, code_content = find_and_read_target_code(alert_summary)
    llm = initialize_llm()
    fixed_code = ""

    if llm and code_content and code_content != "// Target code file":
        for attempt in range(1, 4):
            try:
                logger.info(f"LLM fix generation attempt {attempt}/3 for {rel_path}")
                kb_path = "knowledge_base.json"
                kb_feedback = ""
                if os.path.exists(kb_path):
                    try:
                        with open(kb_path, "r") as f:
                            kb = json.load(f)
                            if kb:
                                feedbacks = [k["feedback"] for k in kb[-3:]]
                                kb_feedback = "\n".join([f"- {fb}" for fb in feedbacks])
                    except Exception:
                        pass

                human_feedback_instruction = f"6. CRITICAL TEAM FEEDBACK (Incident Resolution Memory): Ensure your fix follows this past PR review feedback:\n{kb_feedback}\n" if kb_feedback else ""

                packet = packet_builder.assemble_packet(
                    incident_id=state["incident_id"],
                    service_name=state["service_name"],
                    alert_text=alert_summary
                )

                meta = _detect_language_and_framework(rel_path, code_content)
                evidence_id_list = [e.id for e in packet.all_evidence[:3]]

                prompt = (
                    f"You are a senior Site Reliability & {meta['language']} Engineer AI agent.\n"
                    f"{packet.to_markdown()}\n\n"
                    f"STRICT {meta['framework'].upper()} FIX GUIDELINES:\n"
                    f"{meta['rules']}\n"
                    f"5. Do NOT output unified diffs or git diff markers (`@@ -... @@`).\n"
                    f"{human_feedback_instruction}"
                    f"\nEvidence IDs used: {json.dumps(evidence_id_list)}"
                )
                resp = llm.invoke([SystemMessage(content=f"You are an elite {meta['framework']} SRE AI agent."), HumanMessage(content=prompt)])
                content_str = str(resp.content).strip()

                lang_tag = f"```{meta['code_block_lang']}"
                if lang_tag in content_str:
                    candidate = content_str.split(lang_tag)[1].split("```")[0].strip()
                elif "```" in content_str:
                    candidate = content_str.split("```")[1].split("```")[0].strip()
                else:
                    candidate = content_str.strip()

                if "// Related Module File:" in candidate:
                    candidate = candidate.split("// Related Module File:")[0].strip()
                candidate = re.sub(r'@@\s*-\d+,\d+\s+\+\d+,\d+\s*@@', '', candidate).strip()

                has_safety = "?." in candidate or "if (" in candidate or "if " in candidate or "||" in candidate or "try" in candidate or "Optional" in candidate
                if meta["framework"] == "Next.js App Router":
                    is_valid_framework = "nextresponse" in candidate.lower() and "express" not in candidate.lower()
                elif meta["framework"] == "Express.js":
                    is_valid_framework = "res." in candidate or "req." in candidate
                elif meta["code_block_lang"] == "java":
                    is_valid_framework = "class" in candidate or "public" in candidate or "package" in candidate or "@" in candidate
                elif meta["code_block_lang"] == "python":
                    is_valid_framework = "def " in candidate or "class " in candidate
                else:
                    is_valid_framework = True

                if len(candidate) > 30 and has_safety and is_valid_framework:
                    fixed_code = candidate
                    logger.info(f"LLM fix generation succeeded on attempt {attempt} for {meta['language']}")
                    break
                else:
                    logger.warning(f"LLM attempt {attempt} produced invalid patch for {meta['language']}. Retrying...")
            except Exception as e:
                logger.warning(f"Groq fix generation attempt {attempt} failed: {e}")
                if "rate_limit" in str(e).lower() or "429" in str(e):
                    logger.warning("Rate limit hit on primary model. Switching to fallback model `llama-3.1-8b-instant`...")
                    llm = initialize_llm(model_name="llama-3.1-8b-instant")
                time.sleep(1)

    if not fixed_code:
        logger.error(f"AI Agent was unable to compute a verified fix for {rel_path}. Flagging for human SRE review.")
        state["fixed_code"] = ""
        state["candidate_patch"] = f"// NO_AUTOMATED_FIX: LLM was unable to compute a fix for {rel_path}. Human engineering review required."
    else:
        state["fixed_code"] = fixed_code
        state["candidate_patch"] = fixed_code

    update_incident_status(state["incident_id"], "FIXING", {
        "candidate_patch": state["candidate_patch"]
    })
    return state
