# Function-by-function reference

You've read the source and the big picture makes sense, but individual
functions blur together. This doc is the fix: **every function in the
project, one to two lines each, in plain English.** Not committed — same as
`BEGINNER_GUIDE.md`, this is your personal map, not a deliverable.

**How to use this:** don't read it top to bottom. Open the file you're
confused about, find its section below, and read down the list while you
have the actual code open next to it. Ctrl-F for a function name if you
already know what you're looking for.

A few conventions used throughout:
- `_leading_underscore` names are private helpers — not called from outside
  that file, so you'll never need to import them yourself.
- Most functions take `state` (the big trip dictionary) and return a
  smaller dict of *just the fields they changed* — that's the LangGraph
  pattern explained in `BEGINNER_GUIDE.md` section 8.
- Pydantic model classes (`class Foo(BaseModel)`) aren't really
  "functions" — they're just descriptions of a data shape. I've listed
  their fields instead of pretending they have behaviour.

---

## Table of contents

- [app/ — the main application](#app--the-main-application)
- [gateway/ — the MCP Gateway](#gateway--the-mcp-gateway)
- [mcp_servers/ — the four tool servers](#mcp_servers--the-four-tool-servers)
- [evaluation/ and scripts/](#evaluation-and-scripts)

---

## app/ — the main application

### app/config.py — all the settings

- **`Settings`** (class) — every configuration value the app reads from
  `.env`: API keys, database URL, feature flags like `use_mock_data`. One
  object, read once.
- **`get_settings()`** — hands back the one shared `Settings` instance
  (cached, so reading `.env` only happens once per process).

### app/main.py — the FastAPI entry point

- **`lifespan(app)`** — runs once when the app starts (creates the database
  tables) and once when it stops (closes the connection pool cleanly).
- **`add_request_context(request, call_next)`** — middleware that runs on
  *every* request: gives it a request ID, times it, and puts both on every
  log line written during that request.
- **`prometheus_metrics()`** — the `/metrics` endpoint, in the format
  Prometheus expects.
- **`health()`** — the `/health` endpoint: are we in mock mode, is the
  database reachable, which LLM providers are configured.
- **`root()`** — `/`, just points you at `/docs` and `/health`.

### app/tools.py — hands each agent its allowed tools

- **`get_tools(agent, trip_id)`** — builds the `{tool_name: function}`
  dictionary one agent gets to work with. This is *the* function that makes
  "agents only see their own tools" real — it only includes tools that
  `gateway/policy.py` says this agent may call.
- **`_gateway_call(agent, tool, trip_id)`** — wraps one tool name into an
  actual callable function that, when called, sends the request through
  the Gateway. This is what turns `"search_flights"` (a string) into
  something an agent can actually call like a normal Python function.

### app/mcp_client.py — how the app talks to the Gateway

- **`ToolCallFailed`** (exception) — raised when a tool call didn't work
  out, for any reason. Agents catch this and degrade gracefully instead of
  crashing.
- **`call_tool(agent, tool, arguments, trip_id)`** — the single entry
  point every agent tool call goes through. Decides whether to call the
  Gateway as a plain function (fast, same process) or over real HTTP
  (when `SPLIT_PROCESSES=true`, like in Docker).
- **`_call_in_process(...)`** — the fast path: calls the Gateway's own
  Python function directly, no network involved.
- **`_call_over_http(...)`** — the Docker path: sends a real HTTP POST to
  the Gateway container, with the API key and agent name as headers.
- **`_detail(response)`** — pulls a readable error message out of a failed
  HTTP response, even if it's not valid JSON.

### app/cache.py — Redis helpers

- **`get_client()`** — the shared Redis connection, or `None` if Redis
  can't be reached (checked once, remembered, never retried every call).
- **`set_client(client)`** — swaps in a specific Redis client. Tests use
  this to plug in a fake Redis instead of a real one.
- **`make_key(tool, arguments)`** — turns a tool name + its arguments into
  one stable cache key (sorts the arguments first, so `{a:1,b:2}` and
  `{b:2,a:1}` hash to the same key).
- **`get(key)`** — reads a cached value. Returns `None` on a miss *or* if
  Redis itself is having a bad day — the caller can't tell the difference,
  and doesn't need to.
- **`set(key, value, ttl_seconds)`** — writes a value with an expiry. Fails
  silently if Redis is down (logs a warning, doesn't crash anything).
- **`ttl_for(tool)`** — how long to cache one tool's results (weather gets
  an hour, places get a day, prices get 15 minutes — see the `TOOL_TTL_SECONDS`
  table at the top of the file).
- **`increment_window(key, window_seconds)`** — the building block for
  rate limiting: "add one to this counter, and make sure it expires."
  Returns `None` if Redis is unreachable, which tells the caller to fall
  back to counting in memory instead.
- **`clear()`** — wipes the whole cache. Tests only, never called in
  normal operation.

### app/logging_setup.py — consistent, secret-safe logs

- **`RedactingFilter`** (class) — a logging filter that runs on *every* log
  line from *every* library (not just our own code), and its `.filter()`
  method is what actually does the scrubbing.
- **`_redact_arg(arg)`** — cleans one log argument. Only touches it if it
  actually contains something secret-shaped; leaves numbers and normal
  objects alone so things like `%d` formatting still work.
- **`redact_text(text)`** — the actual find-and-replace: removes API keys
  from query strings (`?api_key=xyz`) and anything matching a known key
  pattern (`sk-...`, `gsk_...`, etc).
- **`redact_secrets(logger, method_name, event_dict)`** — the structlog
  version of the same scrubbing, for our own log calls.
- **`drop_empty(logger, method_name, event_dict)`** — removes any log field
  whose value is `None`, so an unset `trip_id=None` doesn't overwrite a
  real one that was already bound to the request.
- **`configure_logging()`** — sets the whole logging system up: JSON in
  production, pretty colours in development, secrets scrubbed either way.
  Called once, automatically, the first time it's needed.
- **`get_logger(name)`** — hands back a logger for one module to use.
- **`log_context(**fields)`** — a `with` block: everything logged inside
  it automatically gets tagged with these fields (this is how every log
  line for one request ends up sharing the same `trip_id`).
- **`new_request_id()`** — makes up a short random ID for a new request.

### app/metrics.py — Prometheus counters

- **`render()`** — turns the counters into the text format Prometheus
  scrapes from `/metrics`.
- **`classify_error(error)`** — takes a validation error message (free
  text) and sorts it into one of a handful of fixed categories
  (`budget_exceeded`, `no_transport`, etc), so the metric doesn't end up
  with one label per unique sentence ever written.

### app/itinerary_rules.py — three small rules shared everywhere

- **`day_capacity(day_number, total_days)`** — how many activities a given
  day can hold. Day 1 and the last day get 1 (they're travel days),
  everything else gets 3.
- **`is_travel_day(day_number, total_days)`** — is this the first or last
  day of the trip? Those are allowed to have nothing scheduled at all.
- **`round_trip_transport_cost(transport, travelers)`** — the ONE place
  that calculates "fare for everyone, there and back." Used by both
  `compute_costs` and the Travel Manager's budget planning, so they can
  never disagree about this number.

---

### app/graph/state.py — the shape of the trip dictionary

- **`TravelState`** (class) — not a function, just the list of every field
  a trip can have: origin, destination, budget, itinerary, cost breakdown,
  etc. This is the dictionary passed between every step of the workflow.
- **`new_state(trip_id, user_request)`** — builds the very first version of
  that dictionary, when a trip is first created.

### app/graph/graph.py — wires the workflow together

- **`instrumented(name, node)`** — wraps any node function so it's
  automatically timed and logged, without the node itself knowing. This is
  why every step shows up in the metrics without 13 nodes each writing
  their own timing code.
- **`route_after_request_validation(state)`** — after checking the
  traveller's request is sane, decides: continue planning, or stop here
  because something's wrong (missing city, off-topic message, etc).
- **`route_after_plan_validation(state)`** — after checking the finished
  plan, decides: it's good (go to human review), it's fixable (try
  re-planning again), or we've tried enough times (give up and ask the
  human what to do).
- **`route_after_feedback(state)`** — after a modify request is
  interpreted, decides whether the WHOLE research pipeline needs to run
  again (traveller count changed) or just the experience part (everything
  else).
- **`route_after_human_review(state)`** — after the human answers
  approve/modify/reject, decides which node runs next.
- **`build_graph(checkpointer)`** — the actual flowchart-building function.
  Registers every node, wires every arrow between them, and hands back a
  runnable LangGraph object. This is the one function that IS the
  workflow — read this alongside the diagram in `README.md`.

### app/graph/nodes.py — the actual code behind each workflow step

This is the biggest, most important file — each function here is one box
in the workflow diagram.

- **`_refresh_degraded(state, stale_prefixes, fresh)`** — a small helper
  used by the three research nodes below: replaces a domain's OLD "this
  failed" notices with today's, instead of piling them up forever (a
  recovered tool should stop looking broken).
- **`parse_request(state)`** — Step 1. Reads the traveller's raw message
  and turns it into origin/destination/dates/budget/etc, using the LLM
  (with the rule-based fallback if no LLM answers).
- **`validate_request(state)`** — Step 2. Plain Python checks: is there an
  origin and destination, are they different cities, is the trip length
  sane, is the traveller count sane.
- **`research_transport(state)`** — Step 3. Calls the Transport Agent,
  which picks a flight/train/bus.
- **`research_accommodation(state)`** — Step 4. Calls the Accommodation
  Agent for a hotel. Skips the search entirely for a same-day (0 night)
  trip, since there's nothing to book.
- **`research_experience(state)`** — Step 5. Calls the Experience Agent
  for places, weather, and a day-by-day activity split.
- **`create_itinerary(state)`** — Step 6. Turns the Experience Agent's
  rough day split into a proper morning/afternoon/evening schedule, and
  asks the LLM to write a nice one-sentence summary for each day. Every
  date and cost here is still plain Python — the LLM only writes the words.
- **`_merge_degraded(state, notes)`** — small helper: add new "something
  degraded" notes without repeating ones already in the list.
- **`DayDescription`, `ItineraryDescription`** (classes) — the shape the
  LLM's day-summary response has to match.
- **`_describe_itinerary(state, days)`** — the actual LLM call that asks
  for those day summaries, with the fallback-to-nothing behaviour if no
  LLM answers.
- **`_weather_note(weather, activities)`** — writes the little "Heavy Rain,
  73% chance — indoor plans" note you see on each day of the itinerary.
- **`compute_costs(state)`** — **the** money function. Adds up transport +
  hotel + activities + local transport into one total. Called by the
  validator, the re-planner, and the output guard, so all three always
  agree on the number.
- **`validate_plan(state)`** — Step 7. Runs every check in
  `app/validation/validators.py` and decides if the plan is good enough to
  show a human.
- **`replan(state)`** — Step 8 (only runs if validation failed). Delegates
  to the actual fix-it logic in `app/validation/replan.py`.
- **`human_review(state)`** — Step 9. This is the one that **pauses the
  whole workflow** and waits for you to approve/modify/reject. Read the
  `interrupt()` explanation in `BEGINNER_GUIDE.md` section 8 if this looks
  magic.
- **`apply_feedback(state)`** — runs when you say "modify". Reads what you
  actually asked for (cheaper hotel? more people? a new interest?) and
  applies it — either directly (money levers) or by re-running research
  (if the traveller count changed, since that affects prices).
- **`needs_user_decision(state)`** — runs when re-planning tried 3 times
  and still couldn't make the numbers work. Writes an honest "here's what's
  wrong, want to raise your budget?" message.
- **`finalize(state)`** — runs when you approve. Writes the final
  human-readable summary.
- **`output_guard(state)`** — the very last check before anything is sent
  back to you. Recalculates the total from scratch and makes sure it
  matches what's about to be shown — this is what would catch a bug before
  you ever saw a wrong number.

### app/graph/extraction.py — reading your sentence

- **`normalize_interest(interest)`** — maps whatever word was used
  ("historical places", "heritage", "monuments") onto one of the six
  categories the places tool actually understands.
- **`normalize_mode(mode)`** — same idea for transport: "trains", "by
  train", "rail" all become just `"train"`.
- **`ExtractedRequest`** (class) — the shape the LLM's answer has to match:
  origin, destination, dates, budget, interests, etc, all optional (since
  you might not have mentioned all of them).
- **`extract_with_llm(message)`** — asks the LLM to fill in that shape from
  your message.
- **`extract_with_rules(message)`** — the fallback: reads the same
  information using plain regex/keyword matching, no AI needed at all.
  This is what runs if you have zero API keys configured.
- **`to_state(extracted)`** — takes whatever was extracted and turns it
  into real trip-state fields: computes actual dates from a duration,
  fills in sensible defaults for anything missing.
- **`_clean_interests(interests)`** — drops any interest that isn't one of
  the six real categories, and removes duplicates.
- **`_looks_like_travel(text)`** — a quick check: does this message even
  sound like it's about a trip? (looks for words like "trip", "visit",
  "plan").
- **`_find_cities(message)`** — the regex logic for figuring out which
  city is the origin and which is the destination, handling "from X to Y"
  and "to Y, leaving from X" the same way.
- **`_match_marker(message, pattern, names)`** — small helper used by
  `_find_cities` to pull one city name out of a regex match.
- **`_find_duration(text)`**, **`_find_travelers(text)`**,
  **`_find_mode(text)`**, **`_find_hour_limit(text)`** — each one is a
  small regex looking for one specific fact ("5 day", "2 people", "prefer
  trains", "under 12 hours").
- **`_find_budget(text)`** — the trickiest regex here: handles "50000",
  "50,000", "Rs 50k", and "1.5 lakh" all meaning the same thing.

### app/graph/feedback.py — reading your modify request

Same idea as `extraction.py`, but for interpreting feedback like "make the
hotel cheaper and add more historical places" instead of the original
request.

- **`FeedbackIntent`** (class) — the shape: cheaper_hotel (yes/no),
  cheaper_transport (yes/no), fewer_paid_activities (yes/no), a list of
  new interests, a new budget, a new traveller count — all optional.
- **`interpret_with_llm(feedback)`** — asks the LLM to fill in that shape.
- **`interpret_with_rules(feedback)`** — the no-AI fallback: keyword
  matching for the same things.
- **`_find_travelers(text)`** — the regex for a traveller-count change,
  handling both "for 4 people" and "number of peoples to 2" (the number
  can come before or after the word).
- **`_mentions(text, words)`** — tiny helper: does the text contain any of
  these words?
- **`_find_budget(text)`** — similar to extraction.py's version, but only
  matches when the word "budget" is nearby, so it doesn't accidentally
  read a duration or hour limit as money.

### app/graph/prompts.py

No functions — just the actual text sent to the LLM for each of the three
jobs (extract a request, interpret feedback, describe an itinerary). Worth
reading directly if you want to see exactly what instructions the AI is
given.

---

### app/agents/travel_manager.py — the coordinator

- **`build_transport_brief(state)`**, **`build_accommodation_brief(state)`**,
  **`build_experience_brief(state)`** — each one builds the small
  "brief" dictionary handed to one specialist agent, containing only the
  fields that agent needs (never the whole trip state).
- **`_money(amount)`** — formats a number for the human-readable summary,
  handling the case where there's no number at all.
- **`_share(budget, fraction)`** — "a slice of the budget", or `None` if
  there's no budget to slice.
- **`_transport_cost(state)`** — the round-trip transport cost, used when
  deciding how much money is left for the hotel. Calls the one shared
  `round_trip_transport_cost` function so this can never disagree with
  `compute_costs`.
- **`summarize_for_human(state)`** — writes the actual "TRAVEL PLAN..."
  text block you see when a plan is ready for your review.

### app/agents/transport_agent.py — picks flight/train/bus

- **`research_transport(brief, tools)`** — searches all three modes, then
  picks the best one according to the traveller's stated preference (and
  falls back sensibly if the preference can't be met).
- **`_choose(options, brief)`** — the actual decision logic: if they said
  "prefer trains under 12 hours" and no train qualifies, explain why a
  flight was picked instead.
- **`_alternatives(options, recommended)`** — picks which other options to
  show as runners-up, making sure every mode (flight/train/bus) is
  represented rather than just "the next 5 cheapest", which used to
  silently drop buses entirely.

### app/agents/accommodation_agent.py — picks a hotel

- **`research_accommodation(brief, tools)`** — searches hotels, and picks
  the best-rated one that fits the allocated budget (or the cheapest
  available if nothing fits, saying so honestly).

### app/agents/experience_agent.py — places, weather, day planning

- **`research_experience(brief, tools)`** — the top-level function: fetches
  places and weather, then works out which activity goes on which day.
- **`_fetch_places(brief, tools, failures)`** — searches places, once per
  interest the traveller mentioned.
- **`_fetch_weather(brief, tools, failures)`** — gets a forecast for every
  day of the trip.
- **`_fetch_route(brief, tools, failures)`** — one distance/route check,
  from the origin to the destination.
- **`_trip_dates(brief)`** — turns "starts on X, lasts N days" into the
  actual list of calendar dates.
- **`_suggest_schedule(places, weather, brief, notes)`** — the actual
  weather-aware scheduling: puts outdoor activities on the driest days,
  indoor ones on wet days, and makes sure emphasised interests (from "add
  more of X" feedback) get first pick of the slots.

### app/agents/schemas.py — shared data shapes

No real functions — just Pydantic classes describing what a
`TransportOption`, `Hotel`, `Place`, `DayWeather`, `Route` and the three
agents' result objects look like. Worth a skim so you know what fields to
expect when reading the node code above.

---

### app/guardrails/input_guard.py — screens messages before anything runs

- **`InputRejected`** (exception) — raised when a message shouldn't be
  planned at all; carries the polite reply to show the traveller.
- **`check_message(message)`** — the very first check: is it empty, way
  too long, or does it look like someone trying to manipulate the AI
  ("ignore all previous instructions...")?
- **`check_requirements(state)`** — the second check, after extraction:
  is there an origin and destination, are they different cities, is the
  trip length/traveller count/budget sane?

### app/guardrails/output_guard.py — the last check before responding

- **`check_plan(state, recomputed_total)`** — re-derives the total cost
  from scratch and compares it to what's about to be sent, catching any
  number that drifted during re-planning.
- **`check_response_text(text)`** — scans the final text for anything that
  looks like a leaked API key or password.

### app/validation/validators.py — is this plan actually valid?

Seven small, independent checks, each returning a list of problems (empty
list = all good):

- **`validate_dates(state)`** — do the dates parse, run the right way
  round, and match the stated trip length?
- **`validate_transport(state)`** — is there a transport option at all,
  does it have a price, and if it broke the traveller's time limit, could
  a faster option actually have been chosen?
- **`validate_hotel(state)`** — is there a hotel booked for the right
  number of nights? (Skipped entirely for a same-day trip — no hotel is
  correct there.)
- **`validate_itinerary(state)`** — is there one day per trip day, numbered
  correctly, on the right dates?
- **`validate_daily_schedule(state)`** — does every day actually have
  something planned, with no negative costs anywhere?
- **`validate_weather(state)`** — is there an outdoor activity sitting on
  a rainy day when a drier day had room for it? (Only flagged if it's
  actually fixable — no point complaining about rain if every day is wet.)
- **`validate_budget(state, total_cost)`** — does the total fit the
  budget? (Skipped if the traveller never gave one.)
- **`validate_plan(state, total_cost)`** — runs all seven of the above and
  combines the results. This is the one `nodes.py` actually calls.

### app/validation/replan.py — fixing a plan that doesn't fit

- **`replan(state, compute_costs)`** — the entry point: looks at what's
  wrong and tries ONE fix per call (see the four levers below), in a
  fixed cheapest-first order.
- **`_has(errors, fragment)`** — tiny helper: does any error message
  mention this?
- **`cheaper_hotel(state)`** — lever 1: switch to the best-rated hotel that
  costs less than the current one.
- **`cheaper_transport(state)`** — lever 2: switch to a cheaper
  flight/train/bus, still trying to respect the traveller's stated time
  limit if at all possible.
- **`cheaper_activities(state)`** — lever 3: find the single most
  expensive paid activity and either swap it for a free one or drop it.
- **`_reschedule_around_weather(state)`** — lever 4: move outdoor
  activities off rainy days onto sunny ones.
- **`_swap_one(days, forecast, outdoor)`** — does one single swap for the
  weather lever above (moves or trades one activity between two days).
- **`_refill_slots(day)`** — after activities move around, updates the
  morning/afternoon/evening labels to match.

---

### app/llm/client.py — talking to Groq/Gemini, with fallback

- **`LLMUnavailable`** (exception) — raised when NO provider answered at
  all (both Groq and Gemini failed, or neither is configured).
- **`LLMResult`** (class) — what came back: the answer, which provider
  gave it, whether it had to fall back.
- **`track_usage()`** — a `with` block: collects every LLM call made
  inside it, so the API can report "this response used groq, 2 calls,
  800ms" back to you.
- **`_cache_key(system_prompt, user_prompt, response_model)`** — one
  unique key per distinct question (includes the expected answer shape,
  so the same words asked for a different format count as a different
  question).
- **`_record_wait(waited_ms)`**, **`_record(...)`** — small helpers that
  log one call's outcome into whatever `track_usage()` block is currently
  open.
- **`summarize_usage(calls)`** — turns the raw list of calls into the
  neat `{"provider": "groq", "calls": 2, ...}` summary you see in API
  responses.
- **`call_llm(system_prompt, user_prompt, response_model)`** — **the**
  function. Checks the cache first, then tries the primary provider
  (with one retry), then the fallback provider, and raises
  `LLMUnavailable` if nothing worked. Every other file that needs the AI
  calls this one function.
- **`_backoff_for(error)`** — decides how long to wait before retrying,
  based on WHY the call failed. Returns `None` (meaning "don't bother
  retrying at all") for a daily quota that won't reset in time.
- **`_is_hopeless(error)`** — is this error the kind where retrying could
  never possibly work?
- **`_schema_for(model)`**, **`_describe_object(...)`**,
  **`_describe_type(...)`** — build a short, compact description of what
  shape the LLM's answer needs to be, instead of sending the full verbose
  JSON schema (saves a lot of tokens).
- **`_provider_chain()`** — the order to try providers in (primary, then
  fallback), skipping any that has no API key configured.
- **`_api_key(provider)`** — looks up the actual key for "groq" or
  "gemini".
- **`_invoke(provider, system_prompt, user_prompt, model)`** — dispatches
  to whichever provider's actual API-calling function is needed.
- **`_invoke_groq(...)`**, **`_invoke_gemini(...)`** — the actual SDK
  calls to each provider, asking for a JSON answer at temperature 0.

---

### app/db/database.py — Postgres for trip records

- **`get_engine()`** — the shared database connection, or `None` if
  Postgres can't be reached.
- **`create_tables()`** — creates the `trips` and `trip_events` tables if
  they don't exist yet.
- **`set_engine(engine)`** — swaps in a specific engine. Tests use this.
- **`session_scope()`** — a `with` block for one database transaction.
  Never lets a database problem crash the caller — yields `None` instead
  if anything goes wrong.
- **`save_trip(trip_id, state, plan)`** — writes (or updates) one trip's
  row.
- **`record_event(trip_id, event_type, payload)`** — appends one line to
  a trip's audit trail (requested, planned, approved, etc).
- **`get_trip(trip_id)`**, **`list_trips(limit)`**, **`get_events(trip_id)`**
  — the read side: fetch one trip, list recent trips, get one trip's
  history.

### app/db/checkpointer.py — LangGraph's own state saving

- **`get_checkpointer()`** — the thing that makes `interrupt()` and
  resuming a trip actually work. Returns a real Postgres-backed one if the
  database is reachable, otherwise an in-memory one (works fine, just
  forgets everything on restart).
- **`set_checkpointer(checkpointer)`** — swaps it out. Tests use this.
- **`_psycopg_url(url)`** — a tiny URL-format fix (SQLAlchemy and psycopg
  want slightly different URL prefixes).
- **`_dict_row()`** — a small config detail for how psycopg returns rows.
- **`close_checkpointer()`** — closes the connection pool cleanly when the
  app shuts down.

### app/db/models.py — the table definitions

No functions — `Trip` and `TripEvent` are just SQLAlchemy classes
describing what columns those two tables have.

---

### app/api/trips.py — the actual HTTP endpoints

- **`get_graph()`** — builds (once) and hands back the compiled LangGraph
  workflow, wired up to the real checkpointer.
- **`reset_graph()`** — forces it to be rebuilt. Tests use this after
  swapping the checkpointer.
- **`CreateTripRequest`, `TripResponse`, `ApproveRequest`, `ModifyRequest`,
  `MessageRequest`, `TripSummary`** (classes) — the request/response
  shapes for each endpoint. Read these if you want to know exactly what
  JSON to send or expect back.
- **`_plan_of(state)`** — picks out just the traveller-facing fields from
  the full internal state (hides raw tool payloads, internal flags, etc).
- **`_to_response(...)`** — assembles the final `TripResponse` object.
- **`_elapsed_ms(started)`** — how long has it been since `started`, in
  milliseconds.
- **`_pending_decision(trip_id)`** — checks whether a trip is currently
  paused at `human_review`, and if so, what it's waiting for.
- **`_resume(trip_id, decision, event)`** — the shared logic behind
  approve/modify/reject: answers the paused workflow with your decision
  and lets it continue.
- **`create_trip(request)`** — `POST /api/v1/trips`. Starts a brand new
  trip.
- **`approve_trip(trip_id, request)`** — `POST /trips/{id}/approve`.
- **`modify_trip(trip_id, request)`** — `POST /trips/{id}/modify`.
- **`send_message(trip_id, request)`** — `POST /trips/{id}/message`. Same
  as modify, just a more conversational-sounding endpoint.
- **`list_trips(limit)`** — `GET /api/v1/trips`. Recent trips from
  Postgres.
- **`get_trip(trip_id)`** — `GET /trips/{id}`. Fetches a trip, resuming it
  from wherever it's paused.
- **`get_trip_events(trip_id)`** — `GET /trips/{id}/events`. The audit
  trail.

---

## gateway/ — the MCP Gateway

### gateway/policy.py — who can call what

- **`ToolNotAllowedError`** (exception) — raised when an agent tries a
  tool it's not allowed to use.
- **`allowed_tools(agent)`** — looks up the small set of tools one agent
  may call.
- **`check_access(agent, tool)`** — the actual enforcement: raises unless
  this specific agent/tool pairing is allowed. Called before literally
  every tool execution.
- **`requires_human_approval(tool)`** — is this a "booking" action tool
  (needs a human's OK) or a read-only search tool (fine to run freely)?

### gateway/mcp_router.py — routes a tool call to the right MCP server

- **`MCPUnavailableError`**, **`MCPToolError`** (exceptions) — the two
  ways a tool call can fail: the server couldn't be reached at all, versus
  the server ran the tool and it rejected the input.
- **`server_url(server)`** — which URL a given MCP server lives at.
- **`call_tool(tool, arguments)`** — the entry point: figures out which
  server owns this tool and calls it, either in-process or over real HTTP
  depending on `SPLIT_PROCESSES`.
- **`_run(coro)`** — a small trick to run an async MCP call from
  synchronous code (which is what everything else in this project is).
- **`_call_in_process(server, tool, arguments)`** — calls the MCP server's
  Python function directly.
- **`_call_over_http(server, tool, arguments)`** — opens a real MCP
  session over HTTP and calls the tool that way (this is genuine MCP
  protocol, not a shortcut).
- **`_unwrap(outcome)`** — MCP results come back in a few different
  shapes depending on the tool; this normalises all of them into plain
  Python data.

### gateway/server.py — the Gateway's HTTP endpoints

- **`authenticate(x_api_key)`** — checks the `X-API-Key` header matches
  the configured secret.
- **`identify_agent(x_agent_name)`** — reads which agent is calling from
  the `X-Agent-Name` header.
- **`ToolCallRequest`** (class) — the shape of a `POST /call` request
  body.
- **`health()`** — `/health` for the Gateway itself.
- **`prometheus_metrics()`** — `/metrics` for the Gateway.
- **`list_tools(agent)`** — `GET /tools`. Tool discovery, already filtered
  to whichever agent is asking.
- **`call_tool(request, agent)`** — `POST /call`. The main endpoint —
  everything an agent wants to do goes through here.

### gateway/service.py — the actual bouncer logic

- **`RateLimitExceeded`**, **`HumanApprovalRequired`** (exceptions) — the
  two "you can't do that right now" outcomes.
- **`track_tool_usage()`** — same pattern as the LLM one: a `with` block
  that collects every tool call made inside it.
- **`record_tool_call(tool, cache_hit, status)`** — logs one call into
  whatever `track_tool_usage()` block is open.
- **`summarize_tool_usage(calls)`** — turns that raw list into the
  `{"calls": 11, "cache_hits": 9, ...}` summary you see in API responses.
- **`check_rate_limit(agent)`** — the actual rate-limit check: has this
  agent made too many calls in the last minute?
- **`_count_locally(agent)`** — the fallback counter used only if Redis is
  unreachable (so losing Redis doesn't also mean losing the rate limit).
- **`reset_rate_limits()`** — clears all counters. Tests use this.
- **`was_cache_hit(tool, arguments)`** — checks (without fetching) whether
  this exact call is already cached — used so the HTTP response can
  honestly report whether the cache answered it.
- **`execute_tool(agent, tool, arguments, approval_token, trip_id)`** —
  **the** function. Runs the five ordered checks (allowed? needs
  approval? cached? rate limit? actually call it) described in
  `BEGINNER_GUIDE.md` section 7.
- **`_log(...)`** — writes one structured log line (and updates the
  metric) for a tool call.
- **`describe_tools(agent)`** — the tool-discovery logic behind
  `GET /tools` — hands back only what this agent is allowed to see.

---

## mcp_servers/ — the four tool servers

### mcp_servers/cities.py — shared geography data

- **`iata_code(city)`** — the airport code for a city (used to talk to
  AviationStack).
- **`normalize_city(name)`** — matches a user-typed city name
  case-insensitively against the known list.
- **`distance_km(origin, destination)`** — straight-line distance between
  two cities, used as the basis for realistic mock prices and journey
  times.
- **`has_airport(city)`**, **`has_railway(city)`** — does this city have
  one? (Used to correctly return "no flights" for a city with no airport,
  rather than inventing one.)
- **`UnknownCityError`** (exception) — raised when a city name isn't in
  the reference table at all.

### mcp_servers/validation.py — parameter checks shared by all four servers

- **`validate_date(value, field)`** — is this a real `YYYY-MM-DD` date?
- **`validate_count(value, field, minimum, maximum)`** — is this a sane
  whole number (used for passenger counts, guest counts, result limits)?

---

### mcp_servers/transport/ — flights, trains, buses

**`server.py`** (the actual MCP tool definitions):
- **`search_flights(origin, destination, date, passengers)`** — the real
  MCP tool. Uses AviationStack if a key is configured and real data was
  asked for, otherwise mock data.
- **`search_trains(...)`**, **`search_buses(...)`** — same idea, but
  these are always mock (no free API exists for either).

**`mock_data.py`** (the fake-but-realistic data generator):
- **`price_for_flight(km, seed)`** — a plausible fare based on distance,
  shared with the real AviationStack path so a fare is estimated
  consistently either way.
- **`_rng(*parts)`** — makes a random generator seeded by the query, so
  the same search always returns the same "random" answer.
- **`_resolve(origin, destination)`** — checks both cities are known
  before searching.
- **`search_flights(...)`**, **`search_trains(...)`**, **`search_buses(...)`**
  — generate the actual fake options (3 flights, a few train classes, 2
  bus operators), each priced and timed based on real distance.

**`provider.py`** (the real AviationStack integration):
- **`FlightProviderError`** (exception) — AviationStack couldn't answer.
- **`search_flights(...)`** — calls the real API for real flight
  schedules, then estimates a price (since the free tier gives no fares).
- **`parse_flights(payload, ...)`** — turns AviationStack's raw JSON into
  our normal flight-option shape.
- **`_to_option(record, ...)`** — converts one single AviationStack flight
  record.
- **`_hours_between(departure, arrival)`** — journey length from two
  timestamps.

---

### mcp_servers/hotels/ — hotel search

**`server.py`**:
- **`search_hotels(city, check_in, check_out, guests, budget)`** — the MCP
  tool. Real (trivago) if real data was asked for, mock otherwise.

**`mock_data.py`**:
- **`nights_between(check_in, check_out)`** — counts nights, raising an
  error for an invalid (or same-day) range.
- **`search_hotels(...)`** — generates 8 fake hotels with prices that
  correlate with their rating (a 4.8-star room isn't priced like a
  3-star one).

**`provider.py`** (the real trivago integration):
- **`HotelProviderError`** (exception) — trivago couldn't answer.
- **`search_hotels(...)`** — calls the real trivago MCP server.
- **`_search(...)`** — the actual MCP client connection to trivago.
- **`parse_response(text)`** — pulls the hotel list out of trivago's
  response.
- **`strip_instructions(text)`** — removes anything in that response that
  looks like it's trying to give instructions to our AI model (trivago's
  reply literally opens with "IMPORTANT: you MUST follow these
  instructions" — this is why it gets stripped).
- **`to_hotel(entry, ...)`** — maps one trivago result (their format,
  price as a string, rating out of 10) onto our format (numbers, rating
  out of 5).
- **`parse_price(value)`** — turns `"₹12,599"` into `12599.0`.
- **`_review_out_of_five(entry)`** — converts trivago's rating scale to
  ours.

---

### mcp_servers/places/ — attractions, weather-routing distance

**`server.py`**:
- **`search_places(city, category, max_results)`** — the MCP tool. Always
  mock catalogue (no free structured API exists), optionally enriched with
  real Tavily descriptions.
- **`calculate_route(origin, destination)`** — real (OSRM) if real data
  was asked for, mock otherwise.

**`mock_data.py`**:
- **`search_places(...)`** — the fake catalogue: real landmark names for
  the main cities, plus generated filler ("Heritage Walk in X") so smaller
  cities still have enough variety.
- **`calculate_route(...)`** — a straight-line-distance-times-a-road-factor
  estimate. Not a real routing engine.
- **`city_coordinates(city)`** — looks up a city's lat/lon.

**`provider.py`** (real OSRM routes + real Tavily descriptions):
- **`calculate_route(...)`** — calls the real OSRM routing API.
- **`parse_route(origin, destination, payload)`** — turns OSRM's response
  into our normal route shape.
- **`describe_place(name, city, api_key, timeout)`** — asks Tavily for a
  real sentence about one place, with the result cached for a day.
- **`parse_description(payload)`** — pulls a sentence + source link out
  of Tavily's response.
- **`describe_places(places, city, api_key, timeout)`** — adds real
  descriptions to the first 3 places only (protects the free monthly
  quota), leaving the rest untouched.

---

### mcp_servers/weather/ — forecasts

**`server.py`**:
- **`get_weather(city, date)`** — the MCP tool. Real (Open-Meteo) if real
  data was asked for, mock otherwise.

**`mock_data.py`**:
- **`get_weather(...)`** — generates a plausible fake forecast: more rain
  in monsoon months, colder in hill stations, etc.

**`provider.py`** (real Open-Meteo integration):
- **`OutsideForecastWindow`** (exception) — raised when the date is
  further out than Open-Meteo can actually forecast (16 days), so we
  refuse rather than guess.
- **`get_weather(...)`** — calls the real API.
- **`parse_forecast(city, date, payload)`** — turns Open-Meteo's response
  into our normal weather shape.
- **`describe_code(code)`** — turns a numeric weather code into a plain
  word ("heavy rain", "clear", etc).

---

## evaluation/ and scripts/

### evaluation/evaluate.py

- **`run_case(case, use_llm)`** — plans one of the 18 test scenarios,
  optionally breaking specific tools first (to test failure handling).
- **`_pending(graph, config)`** — checks if the trip paused at
  `human_review`, and grabs what it's waiting for.
- **`check(case, outcome)`** — compares what actually happened against
  what the scenario said should happen, returning a list of mismatches
  (empty = passed).
- **`_authorization_failures()`** — a special check used by one scenario:
  makes sure every agent is still refused every tool outside its domain.
- **`main()`** — runs all 18 scenarios (or just one, if `--case` was
  given) and prints a pass/fail report.

### scripts/run_services.py

- **`main()`** — starts the 4 MCP servers and the Gateway as separate
  processes (used when `SPLIT_PROCESSES=true` outside of Docker), and
  shuts all of them down together if any one crashes or you hit Ctrl-C.

---

That's every function. If you're staring at one that's still confusing
after reading its entry here, the fastest next step is: find the tests for
that file (`tests/test_<something>.py`) and read the test names — they're
written as full sentences describing exactly what the function is supposed
to do in plain English, which usually clears things up faster than the
code itself.
