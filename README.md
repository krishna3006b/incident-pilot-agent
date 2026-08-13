<div align="center">
  <img src="https://via.placeholder.com/150" alt="IncidentPilot Logo" />
  <h1>IncidentPilot 🚨</h1>
  <p><strong>AI-powered production incident investigation & remediation</strong></p>
  <p>
    <a href="#demo">Demo</a> •
    <a href="#architecture">Architecture</a> •
    <a href="#evaluation">Evaluation</a> •
    <a href="#getting-started">Getting Started</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/status-active_development-blue" alt="Status" />
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License" />
  </p>
</div>

IncidentPilot turns production incidents into evidence-backed, tested GitHub pull requests. It builds repository-specific knowledge before an incident occurs, investigates alerts using bounded retrieval, generates candidate fixes, verifies them in an isolated environment, and keeps a human in the loop before code is merged.

## 🎥 Demo

> [!NOTE]  
> *(Insert 30-60 second GIF/video here showing: Slack alert ↓ Dashboard incident ↓ Knowledge retrieval ↓ Diagnosis ↓ Patch ↓ Sandbox result ↓ GitHub PR)*

## 🧩 The Problem
Production incidents often require engineers to correlate alerts, logs, deployments, code changes, and historical incidents before writing and validating a fix. IncidentPilot automates this investigation loop while keeping the final code change under human review. 

## ⚡ How It Works

```mermaid
flowchart TD
    A[Production Alert] -->|Slack / Webhook| B(Evidence Collector)
    B --> C[Logs]
    B --> D[Git Deployments]
    
    K[Repository Knowledge] -->|Code RAG Graph| P
    K -->|Incident Memory| P
    
    C --> P(Context Packet)
    D --> P
    
    P -->|Bounded Tool Retrieval| G[Groq Agent]
    G -->|Candidate Fix| S(GitHub Actions Sandbox)
    S -->|Tests Pass/Fail| G
    
    S -->|Tests| PR[GitHub PR]
    PR -->|Human Review| H[Merge]
```

## 🧠 Knowledge-First Architecture
Don't give a massive monolithic repository directly to an agent. IncidentPilot builds repository knowledge *first*, then constructs a bounded evidence packet for each incident.

`Repository` ↓ `Tree-sitter symbol extraction` ↓ `Semantic code embeddings` ↓ `Dependency graph` ↓ `Incident / runbook memory` ↓ `Production evidence` ↓ `Ranked Context Packet` ↓ `Agent`

The agent is never blindly guessing file structures; it operates strictly on bounded context packets and can explicitly request additional evidence when necessary.

## 🔄 End-to-End Flow

**Agent Execution Trace:**
```text
14:03:02  Incident received
14:03:04  Context packet assembled
14:03:07  Relevant symbols retrieved
14:03:11  Root cause identified
14:03:16  Candidate patch generated
14:03:21  Sandbox started
14:03:47  Sandbox passed
14:03:51  PR created
```

## 🧪 Example Incident

**🚨 payment-service HTTP 500 rate > 20%**

**IncidentPilot retrieves:**
- ✓ Stack trace 
- ✓ Affected symbol (`PaymentService.ts`)
- ✓ Recent deployment
- ✓ Relevant Git changes
- ✓ Similar historical incident
- ✓ Relevant repository dependencies

**Diagnosis:** Nullable customer address access introduced during the latest payment-flow change.  
**Fix:** Add null-safe handling in `PaymentService`.  
**Verification:** ✓ Sandbox build ✓ Tests passed  
**Result:** GitHub PR created for human review.

## 🏗️ Core Engineering Concepts

### Knowledge-First Retrieval
Repository-specific knowledge is indexed before incidents occur.
### AST-Semantic Code Retrieval
Tree-sitter extracts symbols/classes/functions rather than blindly chunking files.
### Bounded Agent Execution
Context size, tool calls, repair attempts, and execution time are constrained.
### Evidence-Grounded Diagnosis
Agent conclusions strictly reference persisted evidence IDs.
### Sandbox Verification
Generated changes are tested asynchronously outside the agent backend via GitHub Actions before a PR is created.
### Human-in-the-Loop
The agent opens a PR; a human remains strictly responsible for merging.
### Incident Resolution Memory
Verified historical resolutions are persisted and reused during future investigations.

## 📊 Evaluation (Benchmarking)
IncidentPilot is actively evaluated against seeded production-like incidents using:

| Metric | Description |
|---|---|
| **Retrieval accuracy** | Did the relevant code appear in Top-K? |
| **Root-cause accuracy** | Did the agent identify the correct cause? |
| **Fix success rate** | Did the generated patch resolve the issue? |
| **Test pass rate** | Did the patch pass sandbox verification? |
| **Time to PR** | Time from incident to verified PR |
| **Tool calls** | Investigation complexity |
| **Context size** | Tokens/evidence supplied to the model |
| **LLM cost** | Estimated cost per incident |

*(Results pending benchmark execution phase).*

## 🔐 Security Guardrails

- **HMAC Webhook Validation**: Enforces cryptographic signatures for Slack and GitHub callbacks.
- **Isolated Sandbox**: Code execution runs securely in ephemeral GitHub Actions runners, never on the backend infrastructure.
- **Human Approval**: Absolutely no direct main-branch commits.
- **Restricted File Access**: Bounded retrieval reduces the risk of prompt injection attempting to access sensitive system files.
- **CORS Protection**: Hardened frontend/backend API boundaries.

## 🛠️ Tech Stack

**AI / Agent**
- Groq API
- LangGraph / LangChain

**Backend**
- Python / FastAPI / Uvicorn

**Knowledge / Retrieval**
- Tree-sitter / SentenceTransformers
- PostgreSQL / pgvector

**Frontend**
- Next.js / TypeScript / Tailwind CSS

**Integrations**
- Slack / GitHub API

**Execution**
- GitHub Actions isolated workflow

**Observability / State**
- PostgreSQL / Server-Sent Events (SSE)

## ⚙️ Architecture Decisions

**Why pgvector?**
Keeps operational state and vector retrieval in PostgreSQL without introducing another datastore.

**Why Tree-sitter?**
Provides structural source-code understanding and semantic chunking.

**Why Knowledge First?**
Avoids flooding the model with irrelevant repository context, significantly improving accuracy.

**Why bounded retrieval?**
Controls model cost, latency, and uncontrolled tool execution.

**Why GitHub Actions sandbox?**
Keeps generated code execution completely outside the API process.

**Why human approval?**
The system prepares and verifies changes; it does not autonomously merge production code.

## 🚀 Getting Started

### Setup
1. Create a Supabase project
2. Apply the database schema (`app/db/schema.sql`)
3. Configure environment variables (`.env`)
4. Configure the Slack webhook
5. Configure GitHub API credentials
6. Index your target repository
7. Start the backend (`uvicorn main:app --reload`)
8. Start the dashboard
9. Trigger a test incident

## ⚠️ Current Limitations

- IncidentPilot currently focuses on controlled production-like incidents and repositories configured for its supported runtime profiles.
- Generated changes require human review before merge.
- Incident resolution memory improves as verified incident outcomes accumulate.
- Sandboxed verification is currently implemented through GitHub Actions workflows only.

## 🤝 Contributing
Contributions are welcome. Please open an issue first to discuss what you would like to change.

## Author
Krishna Varshney
