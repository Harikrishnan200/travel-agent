# AI Travel Planning Agent

Multi-agent travel planner built with **LangGraph** and **MCP**, with deterministic
validation, re-planning, and human-in-the-loop approval.

> **Status: Phase 2 of 17.** LangGraph workflow running end to end on stub nodes.
> See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the full architecture and phase plan.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # works as-is: USE_MOCK_DATA=true, no API keys needed
uvicorn app.main:app --reload
```

Then: <http://localhost:8000/health> · <http://localhost:8000/docs>

```bash
curl -X POST localhost:8000/api/v1/trips -H 'Content-Type: application/json' \
  -d '{"message":"Plan a 5 day trip from Kochi to Delhi for 2 people under Rs 50000"}'
```

## Workflow

```mermaid
flowchart TD
    START --> PARSE[parse_request] --> VREQ[validate_request]
    VREQ -->|invalid| END1[END]
    VREQ -->|valid| RT[research_transport] --> RA[research_accommodation]
    RA --> RE[research_experience] --> CI[create_itinerary] --> VP[validate_plan]
    VP -->|errors| RP[replan] --> VP
    VP -->|attempts exhausted| NUD[needs_user_decision] --> END2[END]
    VP -->|valid| HR[human_review]
    HR -->|modify| RP
    HR -->|approve| FIN[finalize] --> OG[output_guard] --> END3[END]
    HR -->|wait| END3
```

## Tests

```bash
pytest -q
ruff check . && ruff format --check .
```

## Documentation

- [PROJECT_PLAN.md](PROJECT_PLAN.md) — architecture, agents, MCP design, phases
- `docs/interview-guide.md` — coming in Phase 17
