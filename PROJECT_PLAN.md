# PROJECT PLAN — AI Travel Planning Agent (Multi-Agent + MCP)

> **All 17 phases are complete.** This is the plan as written before any code
> existed, kept unedited so the plan and the result can be compared. Where the
> build diverged from it, [README.md](README.md) describes what was actually
> done and [docs/interview-guide.md](docs/interview-guide.md) says why.
>
> The main divergences: the MCP servers gained real providers (trivago, Open-Meteo,
> OSRM, AviationStack, Tavily) rather than staying mock-only; there are 13 graph
> nodes rather than 11, because feedback needed its own node and the
> "needs user decision" path had to remain answerable; and the LLM answers are
> cached, which the plan did not anticipate but a twenty-request daily free tier
> made necessary.

> Guiding rule: **simplicity > abstraction**. Every component below exists because
> I can answer "why is this here?" in one sentence. Anything I couldn't justify
> was cut.

---

## 1. Project Goal

Let a user say, in plain English:

> "Plan a 5 day trip from Kochi to Delhi for 2 people. Budget ₹50,000.
> Prefer trains if the journey is under 12 hours, otherwise flights.
> I like historical places and food."

…and get back a **validated, budget-correct, day-by-day itinerary** that the
human reviews, modifies, and approves before it is finalized.

The system must run **fully offline with mock data** (`USE_MOCK_DATA=true`) so no
paid API is ever required.

**What it does NOT do:** no real booking, no payments, no autonomous actions.
Booking tools exist only as a *design* to demonstrate HITL authorization.

---

## 2. Architecture (high level)

```
USER
 |
 v
FastAPI  ──►  Input Guardrail  ──►  LangGraph workflow
                                        |
                              Travel Manager (coordinator node)
                                        |
                 +----------------------+----------------------+
                 |                      |                      |
           Transport Agent      Accommodation Agent     Experience Agent
                 |                      |                      |
                 +----------------------+----------------------+
                                        |
                                   MCP Gateway   (auth + authz + rate limit + log)
                                        |
             +-------------+------------+-------------+
             |             |            |             |
       transport-mcp   hotel-mcp    places-mcp    weather-mcp
                                        |
                              Itinerary Planner (LLM node)
                                        |
                          Deterministic Validator (pure Python)
                                        |
                            FAIL ─► Re-plan ─► Validator
                                        |
                                    PASS ─► Human Review (HITL interrupt)
                                        |
                          MODIFY ─► Re-plan        APPROVE ─► Finalize
                                                        |
                                                 Output Guardrail
                                                        |
                                                       USER
```

Supporting infra: **PostgreSQL** (trip records + LangGraph checkpoints),
**Redis** (tool-result cache + rate limiter), **structlog** (observability).

---

## 3. Multi-Agent Design

**Four logical agents. Not one per tool.**

| Agent | Role | Tools it may call |
|---|---|---|
| Travel Manager | Coordination only. Decides *what research is needed*, merges results, drives itinerary/validation/HITL. | **none** (delegates) |
| Transport Agent | Compare flight vs train vs bus against user preference rules. | `search_flights`, `search_trains`, `search_buses` |
| Accommodation Agent | Pick hotels within budget/rating/location. | `search_hotels` |
| Experience Agent | Places, weather, distances; suggests weather-aware day ordering. | `search_places`, `get_weather`, `calculate_route` |

**Why multi-agent (the interview answer):** a single agent facing 7+ tools has a
large tool-selection space and picks wrong tools more often. Splitting by domain
means each agent sees ≤3 tools, with a prompt scoped to one job. The MCP Gateway
*enforces* the split — an agent that hallucinates a tool outside its scope gets a
hard `403`, not a wrong answer.

An "agent" here is deliberately plain: **a Python function that builds a scoped
prompt, calls the LLM with a restricted tool list, executes approved tool calls
through the Gateway, and returns a Pydantic model.** No agent framework, no
supervisor abstraction.

**Itinerary Planner is a LangGraph node, not a 5th agent** — it calls no tools,
it only turns collected data into a schedule. Making it an agent would add a
class for nothing.

---

## 4. Agent Responsibilities & Contracts

Every agent returns a Pydantic model, so the Travel Manager never parses free text.

```python
TransportResult:      recommended: Option | None, alternatives: list[Option], reason: str, failures: list[str]
AccommodationResult:  recommended: Hotel | None,  alternatives: list[Hotel],  reason: str, failures: list[str]
ExperienceResult:     places: list[Place], weather: list[DayWeather], routes: list[Route], notes: list[str]
```

`failures` carries degraded-mode info ("train MCP unavailable") so the final
response can honestly say what was skipped.

**Hallucination control:** agents may only put into their result objects data
that came back from a tool call. Enforced by re-validating returned IDs against
the raw tool payload in Python before accepting the agent's choice — if the LLM
invents a train number, we drop it and fall back to the cheapest real option.

---

## 5. MCP Architecture

Four custom MCP servers, built with the official `mcp` Python SDK (FastMCP),
served over **streamable HTTP** so they can be separate processes or in-process.

```
mcp_servers/
  transport/server.py   search_flights, search_trains, search_buses   (+ book_* stubs = ACTION)
  hotels/server.py      search_hotels                                 (+ book_hotel stub = ACTION)
  places/server.py      search_places, calculate_route
  weather/server.py     get_weather
```

`calculate_route` lives in **places-mcp** (it's geography, same data files) —
no separate maps server, that would be a folder for appearance only.

Every tool has an explicit typed signature validated by Pydantic *before*
execution. No `search(**anything)`.

**Providers:** each tool body is
```python
if settings.use_mock_data:  return mock_search_trains(...)
else:                       return real_search_trains(...)
```
A module-level `if`, not a provider abstraction layer. Real providers: Open-Meteo
(weather, free, no key) and OSRM (routes, free) are implemented; transport/hotels
stay mock-only since no free Indian rail/flight API exists.

---

## 6. MCP Gateway Responsibilities

A small FastAPI service (`gateway/server.py`) sitting between agents and MCP servers.

1. **Authentication** — `X-API-Key` must match `MCP_GATEWAY_API_KEY`.
2. **Identity** — caller sends `X-Agent-Name: transport_agent`.
3. **Authorization** — static `AGENT_TOOL_ACCESS` dict; unknown/forbidden tool → `403`.
4. **Tool discovery** — `GET /tools?agent=transport_agent` returns only that agent's tools (so the LLM's tool list is filtered at the source, not just at enforcement time).
5. **Routing** — `TOOL_REGISTRY` maps tool name → MCP server URL; forwards the call.
6. **Rate limiting** — Redis `INCR` + `EXPIRE` on a per-agent 60s window (default 60 calls/min).
7. **HITL enforcement** — `ACTION_TOOLS` require an `approval_token`; without one → `403 human_approval_required`.
8. **Logging** — one structured line per call: request_id, trip_id, agent, tool, duration_ms, status, cache hit.
9. **Caching** — read-tool results cached in Redis, key = `sha256(tool + sorted args)`, TTL by tool type (weather 1h, transport/hotels 15m, places 24h).
10. **Failure isolation** — MCP timeout/down → `503` with a structured error; the agent continues with whatever else succeeded.

Endpoints: `POST /call`, `GET /tools`, `GET /health`.

---

## 7. LangGraph Workflow

```
START → parse_request → validate_request →┬(invalid)→ END (clarification message)
                                          │
                          research_transport
                                  ↓
                          research_accommodation
                                  ↓
                          research_experience
                                  ↓
                           create_itinerary
                                  ↓
                            validate_plan ──FAIL(attempt<3)──► replan ──┐
                                  │                                     │
                                  │◄────────────────────────────────────┘
                                  │
                                  ├─FAIL(attempt=3)──► needs_user_decision → END
                                  │
                                  └─PASS──► human_review  (interrupt)
                                                │
                                MODIFY ─► replan ┘
                                APPROVE ─► finalize → output_guard → END
                                REJECT  ─► END
```

11 nodes. `research_*` nodes are sequential (transport dates feed hotel dates);
keeps state merging trivial and is fast enough with caching.

---

## 8. State Design

One `TypedDict`, flat, no nesting beyond Pydantic-model values:

```python
class TravelState(TypedDict, total=False):
    trip_id: str
    user_request: str
    origin: str; destination: str
    start_date: str; end_date: str          # ISO, computed in Python from duration
    travelers: int; budget: float
    preferences: dict; interests: list[str]

    transport_options: dict                 # raw tool payloads (audit trail)
    hotel_options: list; places: list; weather: list

    selected_transport: dict; selected_hotel: dict
    itinerary: dict                         # days[], per-day activities
    cost_breakdown: dict                    # transport/hotel/activity/local
    total_cost: float

    validation_errors: list[str]
    replan_attempts: int
    degraded: list[str]                     # MCP/LLM failures worth telling the user

    status: str                             # planning|awaiting_approval|approved|rejected|failed
    human_feedback: str; human_approved: bool
    final_response: str
```

---

## 9. Database Design (PostgreSQL)

Two tables. That's all this needs.

```sql
trips(
  trip_id UUID PK, thread_id TEXT, user_request TEXT, status TEXT,
  state JSONB, itinerary JSONB, total_cost NUMERIC,
  created_at, updated_at
)
trip_events(              -- lightweight audit/observability trail
  id BIGSERIAL PK, trip_id UUID FK, event_type TEXT, payload JSONB, created_at
)
```

Plus LangGraph's own checkpoint tables via `langgraph-checkpoint-postgres`
(`AsyncPostgresSaver`) — that gives interrupt/resume for HITL for free rather
than me hand-rolling state persistence. `trips` stays as the queryable
application view; the checkpointer is the resume mechanism.

---

## 10. Redis Usage

Cache and rate limiting **only**. No queues, no pub/sub.

- `tool:<sha256>` → JSON tool result, TTL per tool type.
- `ratelimit:<agent>:<minute>` → counter.
- Redis down = degraded, not fatal: cache helpers swallow errors and go to source.

---

## 11. LLM Fallback

Primary **Groq** (`llama-3.3-70b-versatile`), fallback **Gemini**
(`gemini-2.0-flash`). One function:

```python
def call_llm(messages, response_model=None) -> ...:
    try:    return _invoke(primary, ...)
    except Exception as e:
        log.warning("llm_primary_failed", provider=primary, error=str(e))
        state_degraded.append("llm_fallback_used")
        return _invoke(fallback, ...)
```

Retries once on the primary (transient), then falls back. Both fail → raise
`LLMUnavailable`, the node marks the trip `failed` with a clear message; existing
state is preserved so the user resumes rather than restarts.

No router, no scoring, no cost optimizer. Structured output via each SDK's JSON
mode + Pydantic parse; a parse failure is treated like a call failure.

---

## 12. Guardrails

**Input** (`guardrails/input_guard.py`, pure Python, runs before the LLM and again after parsing):
- on-topic check (travel keyword/LLM classification) → "I'm a travel planning assistant…"
- prompt-injection scan (ignore-previous-instructions patterns)
- length cap
- after parse: origin/destination present, dates parse, end ≥ start, duration 1–30 days, travelers 1–20, budget > 0

**Output** (`guardrails/output_guard.py`, runs before returning):
- itinerary day count == trip duration; dates contiguous and correct
- `total_cost` recomputed in Python and compared to the plan's number — LLM never owns the total
- required fields present; hotel nights == duration−1 (or as travel days dictate)
- no impossible schedule (activity before arrival, overlapping times)
- no leaked secrets/PII, no fabricated booking references

---

## 13. HITL Flow

Uses LangGraph `interrupt()` + Postgres checkpointer.

1. Plan validates → graph interrupts at `human_review`, trip status `awaiting_approval`, API returns the plan summary.
2. `POST /trips/{id}/modify {"feedback": "..."}` → resume with feedback → `replan` → `validate_plan` → back to `human_review`.
3. `POST /trips/{id}/approve {"approved": true}` → `finalize` → `output_guard` → END.
4. `approved: false` → status `rejected`, END.

Approval is asked **once per plan version**, never per tool call.

**Action tools (booking):** designed but not implemented as real bookings. The
Gateway rejects any `book_*` call lacking an `approval_token` issued by the
approval endpoint. This is the safety story: reads are free, writes need a human.

---

## 14. Validation & Re-planning

`validation/validators.py` — plain functions, deterministic, no LLM:
`validate_dates`, `validate_budget`, `validate_transport`, `validate_hotel`,
`validate_itinerary`, `validate_daily_schedule`. Each returns
`list[str]` of errors; `validate_plan` concatenates.

Budget: `total = transport + hotel + activities + local_transport`.
`over = total - budget` → error `"Budget exceeded by ₹7,000"`.

**Re-planner** (max 3 attempts), simple ordered heuristics — not an optimizer:
1. swap to a cheaper hotel from `hotel_options`
2. swap to a cheaper transport option that still respects hard preferences
3. drop/replace the most expensive paid activities with free ones
4. weather conflict → move outdoor activity to the best-weather remaining day, else swap in an indoor place

Steps 1–3 are deterministic Python; only the *re-narration* of the itinerary uses
the LLM. After 3 failures: stop and ask the user to raise the budget or relax a
preference (`status = needs_user_decision`).

---

## 15. Observability

`structlog` JSON logs. Every log line carries `request_id`, `trip_id`, plus
whichever of `agent`, `node`, `tool`, `duration_ms`, `status`, `llm_provider`,
`fallback_used`, `cache_hit` apply. Secrets are never logged (a redaction
processor drops `*_key`, `*_token`, `authorization`).

`prometheus-client` on `/metrics` with **four** counters/histograms only:
`llm_calls_total{provider,status}`, `tool_calls_total{tool,status}`,
`trip_validation_failures_total{reason}`, `node_duration_seconds{node}`.
No Grafana, no OTel collector — a Grafana config would be decoration.

---

## 16. Evaluation

`evaluation/dataset.json` — 12 cases matching the brief (basic, budget-constrained,
train/flight/bus preference, weather conflict, invalid dates, budget exceeded,
human modification, LLM fallback, MCP failure, multi-city).

`evaluation/evaluate.py` runs each case through the graph with mocks/fault
injection and scores deterministically:
extraction accuracy · correct tool selection · preference respected ·
budget arithmetic correct · itinerary valid · modification handled ·
response complete. Prints a table + pass rate, exits non-zero below threshold.
Plain Python + asyncio. No eval framework.

---

## 17. Testing (pytest)

```
tests/test_validation.py     dates, budget math, itinerary, daily schedule
tests/test_transport.py      preference rules (<12h → train), mock provider shape
tests/test_hotels.py         budget filtering, total price math
tests/test_places.py         category filter, route calc
tests/test_guardrails.py     off-topic, injection, output guard catches bad totals
tests/test_llm_fallback.py   primary fails → fallback used; both fail → clear error
tests/test_gateway.py        auth, authorization 403, rate limit, ACTION tool needs approval
tests/test_replan.py         budget exceeded → cheaper hotel; weather conflict → day move
tests/test_graph.py          HITL interrupt/resume, state transitions
tests/test_api.py            all endpoints, happy path + errors
```

All tests run with `USE_MOCK_DATA=true`, fake LLM, fakeredis, and no network.

---

## 18. Docker

`docker-compose.yml` — **4 services**: `app` (FastAPI + agents + gateway + MCP
servers in one image, started by a small supervisor script), `postgres`, `redis`.
Actually 3 containers; MCP servers run as child processes inside `app` because
separate containers per MCP server would be 4 more containers for zero learning.
A commented-out `mcp` service shows how to split them if asked in an interview.

`docker compose up` → working system on `localhost:8000`.

---

## 19. GitHub Actions

`.github/workflows/ci.yml` on push + PR:
checkout → setup-python 3.12 → pip install → `ruff check` + `ruff format --check`
→ `pytest -q` (postgres + redis service containers) → `docker build`.
No deploy step.

---

## 20. Development Phases

Each phase ends with: run tests → run the app → verify → review for needless
complexity → explain → commit → **stop and report before continuing**.

| Phase | Deliverable |
|---|---|
| 1 | Repo skeleton, `config.py`, FastAPI + `/health`, `.env.example`, requirements, `.gitignore`, git init + first commit |
| 2 | `TravelState`, LangGraph skeleton with stub nodes, in-memory checkpointer, `POST /trips` runs end-to-end with fakes |
| 3 | The three agents + Travel Manager wiring (still calling local service functions) |
| 4 | Four MCP servers with mock data + per-tool tests |
| 5 | MCP Gateway: auth, discovery, authz, routing, rate limit, logging; agents switch to gateway calls |
| 6 | Real/mock provider switch (Open-Meteo, OSRM), Redis cache, MCP failure degradation |
| 7 | LLM fallback (Groq → Gemini) + structured output |
| 8 | Input/output guardrails, deterministic validators |
| 9 | Re-planning (budget + weather) |
| 10 | Postgres persistence + LangGraph checkpointer, resume a trip |
| 11 | HITL interrupt/resume, approve/modify/reject endpoints |
| 12 | structlog + Prometheus metrics |
| 13 | Evaluation dataset + runner |
| 14 | Full pytest suite green |
| 15 | Dockerfile + docker-compose |
| 16 | GitHub Actions CI |
| 17 | README (Mermaid diagrams), `docs/interview-guide.md`, troubleshooting |

---

## 21. Directory Structure

```
travel-agent/
├── app/
│   ├── main.py                  FastAPI app, lifespan, /health, /metrics
│   ├── config.py                pydantic-settings, one Settings object
│   ├── logging_setup.py         structlog config + redaction
│   ├── api/
│   │   └── trips.py             5 endpoints
│   ├── agents/
│   │   ├── travel_manager.py
│   │   ├── transport_agent.py
│   │   ├── accommodation_agent.py
│   │   ├── experience_agent.py
│   │   └── schemas.py           Pydantic contracts between agents
│   ├── graph/
│   │   ├── state.py             TravelState
│   │   ├── nodes.py             the 11 nodes
│   │   ├── graph.py             build_graph()
│   │   └── prompts.py
│   ├── llm/
│   │   └── client.py            call_llm + fallback
│   ├── guardrails/
│   │   ├── input_guard.py
│   │   └── output_guard.py
│   ├── validation/
│   │   ├── validators.py
│   │   └── replan.py
│   ├── mcp_client.py            thin HTTP client for the Gateway
│   ├── cache.py                 Redis helpers
│   └── db/
│       ├── database.py          engine/session
│       └── models.py            trips, trip_events
├── gateway/
│   ├── server.py                FastAPI gateway
│   └── policy.py                AGENT_TOOL_ACCESS, READ_TOOLS, ACTION_TOOLS, TOOL_REGISTRY
├── mcp_servers/
│   ├── transport/{server.py,mock_data.py}
│   ├── hotels/{server.py,mock_data.py}
│   ├── places/{server.py,mock_data.py}
│   └── weather/{server.py,provider.py}
├── evaluation/{dataset.json,evaluate.py}
├── tests/…
├── docs/interview-guide.md
├── scripts/run_all.py           starts gateway + MCP servers + app (Docker entrypoint)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## Deliberate Simplifications (and how I'd defend them)

| Choice | Why |
|---|---|
| 4 agents, not 10 | Domain boundaries, not tool boundaries. |
| Itinerary planner = node | It calls no tools; an agent class would add nothing. |
| Sequential research nodes | Hotel dates depend on transport; parallel merge isn't worth the complexity. |
| MCP servers in the app container | Demonstrates MCP fully; 4 extra containers teach nothing. |
| Module-level mock/real `if` | A provider registry would be abstraction for one branch. |
| Heuristic re-planner | Real optimization is out of scope; 3 ordered fixes cover the demo cases. |
| 2 DB tables | Everything else is JSONB inside the trip. |
| Multi-city | **Phase 2 / future work.** Sequential legs would ripple through validation and re-planning; single-origin/destination first. |

## Known Limitations (stated up front, also in README)

- Mock transport/hotel data — not real availability or pricing.
- Guardrails + validation **reduce** hallucination; they don't eliminate it.
- Re-planner is heuristic, not optimal.
- Single-region assumptions (INR, Indian cities).
- No auth on the public API (Gateway is authenticated; the app API is demo-open).
