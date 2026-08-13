import os
import re
import json
import httpx
import logging
from typing import Dict, Tuple

try:
    from langchain_groq import ChatGroq
except ImportError:
    ChatGroq = None

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TARGET_TEMPLATES = {}

def initialize_llm(model_name: str = "llama-3.3-70b-versatile"):
    groq_key = settings.GROQ_API_KEY or os.getenv("GROQ_API_KEY")
    if groq_key and ChatGroq:
        try:
            return ChatGroq(
                groq_api_key=groq_key,
                model_name=model_name,
                temperature=0.1,
                max_tokens=2048
            )
        except Exception as e:
            logger.warning(f"Groq LLM init failed for {model_name}: {e}.")
    return None

def find_and_read_target_code(alert_summary: str) -> Tuple[str, str]:
    """Extract target file from alert and fetch its code content."""
    matches = re.findall(r'(src/[\w/-]+\.ts)', alert_summary)

    if matches:
        rel_path = matches[0]
    else:
        rel_path = ""

    github_token = os.getenv("GITHUB_TOKEN")
    github_repo = os.getenv("GITHUB_REPO", "")

    def fetch_file_content(path: str) -> str:
        if github_token and github_repo:
            try:
                headers = {
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github.v3+json"
                }
                resp = httpx.get(f"https://api.github.com/repos/{github_repo}/contents/{path}", headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    import base64
                    return base64.b64decode(resp.json().get("content", "")).decode("utf-8")
            except Exception as e:
                logger.warning(f"Failed to fetch {path} from GitHub: {e}")

        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
        full_path = os.path.join(base_dir, "target_app", path)
        if os.path.exists(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
        return DEFAULT_TARGET_TEMPLATES.get(path, "")

    primary_content = fetch_file_content(rel_path)

    combined_content = f"// Primary Target File: {rel_path}\n{primary_content}"
    unique_paths = list(dict.fromkeys(matches))
    for extra_path in unique_paths[1:]:
        extra_code = fetch_file_content(extra_path)
        if extra_code:
            combined_content += f"\n\n// Related Module File: {extra_path}\n{extra_code}"

    return rel_path, combined_content

def trigger_sandbox_test(incident_id: str, target_file: str, patch_code: str, target_branch: str = "main", callback_url: str = "", setup_cmd: str = "", build_cmd: str = "", test_cmd: str = "") -> str:
    """Trigger GitHub Actions sandbox-test.yml via repository_dispatch on agent repo and track the run."""
    import uuid
    from datetime import datetime, timezone
    from app.db.supabase import create_sandbox_run
    
    github_token = os.getenv("GITHUB_TOKEN")
    agent_repo = os.getenv("AGENT_REPO", "")

    sandbox_run_id = f"{incident_id}-{str(uuid.uuid4())[:8]}"
    
    # Pre-register the sandbox run in the database
    create_sandbox_run({
        "id": sandbox_run_id,
        "incident_id": incident_id,
        "status": "PENDING",
        "verdict": "UNKNOWN",
        "started_at": datetime.now(timezone.utc).isoformat()
    })

    if not github_token:
        logger.warning("No GITHUB_TOKEN set, skipping sandbox trigger.")
        return sandbox_run_id

    try:
        resp = httpx.post(
            f"https://api.github.com/repos/{agent_repo}/dispatches",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={
                "event_type": "validate_patch",
                "client_payload": {
                    "incident_id": incident_id,
                    "sandbox_run_id": sandbox_run_id,
                    "target_file": target_file,
                    "target_branch": target_branch,
                    "patch_code": patch_code[:5000],
                    "callback_url": callback_url,
                    "setup_command": setup_cmd,
                    "build_command": build_cmd,
                    "test_command": test_cmd
                }
            },
            timeout=10.0
        )
        if resp.status_code in (200, 204):
            logger.info(f"Sandbox test triggered on {agent_repo} for run {sandbox_run_id}")
            from app.db.supabase import update_sandbox_run
            update_sandbox_run(sandbox_run_id, {"status": "RUNNING"})
        else:
            logger.warning(f"Sandbox trigger for {agent_repo} returned {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.warning(f"Sandbox trigger failed: {e}")
        
    return sandbox_run_id

def _calculate_evidence_confidence(alert_summary: str, rel_path: str, code_content: str, root_cause: str) -> float:
    """Calculate confidence score from evidence quality signals."""
    score = 0.0
    text = alert_summary.lower()

    if "route.ts:" in text or ".ts:" in text or "at POST" in text:
        score += 0.25

    error_patterns = ["typeerror", "cannot read propert", "cannot destructure", "is not a function", "is null", "is undefined"]
    if any(p in text for p in error_patterns):
        score += 0.20

    if rel_path:
        score += 0.15

    if "null" in code_content.lower() or "undefined" in code_content.lower() or "error" in code_content.lower():
        score += 0.20

    if root_cause and len(root_cause) > 50 and "Unhandled null" not in root_cause:
        score += 0.15
    elif root_cause and "TypeError" in root_cause:
        score += 0.10

    if "/api/" in text:
        score += 0.05

    return min(score, 1.0)

def _detect_language_and_framework(rel_path: str, code_content: str) -> Dict[str, str]:
    """Dynamically inspect target file to determine language and framework guidelines."""
    ext = rel_path.split(".")[-1].lower() if "." in rel_path else ""

    if ext == "java":
        return {
            "language": "Java",
            "framework": "Spring Boot / Java EE" if "@" in code_content or "springframework" in code_content else "Java",
            "code_block_lang": "java",
            "setup_cmd": "",
            "build_cmd": "./mvnw package -DskipTests",
            "test_cmd": "./mvnw test",
            "rules": (
                "1. Target file is a Java class.\n"
                "2. Preserve all Java package declarations, imports, annotations (e.g. `@RestController`, `@PostMapping`), and class signatures.\n"
                "3. Use null checks (e.g. `if (obj != null)`), `Optional.ofNullable(...)`, or default fallbacks for safe property access.\n"
                "4. Output ONLY the complete updated Java code inside a ```java ... ``` block."
            )
        }
    elif ext in ("py", "python"):
        return {
            "language": "Python",
            "framework": "FastAPI" if "fastapi" in code_content.lower() or "basemodel" in code_content.lower() else "Python",
            "code_block_lang": "python",
            "setup_cmd": "pip install -r requirements.txt",
            "build_cmd": "",
            "test_cmd": "pytest",
            "rules": (
                "1. Target file is a Python module.\n"
                "2. Preserve all function signatures, imports, and Pydantic schemas.\n"
                "3. Add defensive null/None checks (`if obj is not None:`) or default fallbacks (`getattr(...)`).\n"
                "4. Output ONLY the complete updated Python code inside a ```python ... ``` block."
            )
        }
    elif ext in ("ts", "tsx", "js", "jsx"):
        base_ts = {
            "setup_cmd": "npm ci || npm install",
            "build_cmd": "npx tsc --noEmit",
            "test_cmd": "npm test"
        }
        if "next/server" in code_content or "nextresponse" in code_content.lower() or "app/api/" in rel_path:
            return {
                "language": "TypeScript (Next.js App Router)",
                "framework": "Next.js App Router",
                "code_block_lang": "typescript",
                **base_ts,
                "rules": (
                    "1. Target file is a Next.js App Router API route (`src/app/api/.../route.ts`).\n"
                    "2. You MUST use Next.js App Router format:\n"
                    "   `import { NextResponse } from 'next/server';`\n"
                    "   `export async function POST(req: Request) { ... }`\n"
                    "   `const body = await req.json();`\n"
                    "   `return NextResponse.json({ ... });`\n"
                    "3. ALWAYS replace unsafe property accesses (e.g. `body.user`, `body.items[0]`, `body.customer.address`) with safe optional chaining or fallbacks.\n"
                    "4. DO NOT use Express.js syntax (`express`, `Router()`, `res.status()`, `res.send()`, `req.body`).\n"
                    "5. Output ONLY the complete updated Next.js TypeScript code inside a ```typescript ... ``` block."
                )
            }
        elif "express" in code_content.lower() or "router()" in code_content.lower():
            return {
                "language": "TypeScript (Express.js)",
                "framework": "Express.js",
                "code_block_lang": "typescript",
                **base_ts,
                "rules": (
                    "1. Target file is an Express.js router module.\n"
                    "2. Preserve existing Express router handlers (`req: Request, res: Response`).\n"
                    "3. Use safe optional chaining (`req.body?.user?.email`) and return `res.status(...).json(...)`.\n"
                    "4. Output ONLY the complete updated Express TypeScript code inside a ```typescript ... ``` block."
                )
            }
        else:
            return {
                "language": "TypeScript / JavaScript",
                "framework": "Generic TS/JS",
                "code_block_lang": "typescript",
                **base_ts,
                "rules": (
                    "1. Preserve existing module exports and function signatures.\n"
                    "2. Add optional chaining `?.` or default fallbacks `||` for property dereferences.\n"
                    "3. Output ONLY updated code inside a ```typescript ... ``` block."
                )
            }
    else:
        return {
            "language": ext.upper() if ext else "Generic",
            "framework": "Generic",
            "code_block_lang": ext if ext else "text",
            "setup_cmd": "",
            "build_cmd": "",
            "test_cmd": "make test || echo 'No tests'",
            "rules": (
                "1. Preserve existing code structure and signatures.\n"
                "2. Fix unsafe property dereferences safely."
            )
        }
