# Interview guide

Twenty questions this project invites, and how I would answer them. The answers
are short on purpose - each one is the thing I would actually say, not a
recitation.

Where a decision was wrong at first, that is written down too. "We built it, it
broke, here is what it taught us" is a better answer than a clean story nobody
believes.

---

## 1. Why multi-agent, and not one agent?

To shrink the tool-selection problem.

One agent facing seven tools has to pick correctly every time from a large space,
with a prompt that has to describe all seven domains at once. Four agents split by
domain means each sees at most three tools and has a prompt about one job.

The important part is that it is **enforced, not suggested**. The MCP Gateway
holds a table of which agent may call which tool. An agent that asks for
something outside its domain gets a `403`, not a plausible wrong answer. So the
architecture is a real constraint rather than a hope about prompt behaviour.

## 2. Why four? Why not ten, or two?

Agents are **domains, not tools**. The question I asked for each candidate agent
was: does it have a decision to make that nothing else can make?

- Transport: yes. Flight against train against bus, weighed against a stated
  preference.
- Accommodation: yes. Rating against price against a budget.
- Experience: yes. What to see, and on which day, given the weather.
- Travel Manager: yes, but a different one - what research is needed at all, and
  how the answers combine.

A weather agent has no decision; it fetches a number. So weather is a *tool*
belonging to the agent that reasons about it. Same for maps.

The itinerary planner is a **workflow node, not a fifth agent**, for the same
reason: it calls no tools and chooses nothing between them - it turns collected
data into a schedule.

## 3. How does multi-agent actually reduce tool-selection complexity?

Three ways, in increasing order of strength:

1. **A smaller menu.** Three tools rather than seven.
2. **Filtered discovery.** `GET /tools?agent=transport_agent` returns only that
   agent's tools, so the model is never shown the rest.
3. **Refusal.** If it somehow asks anyway, the Gateway refuses.

The third is the one that matters. The first two make mistakes less likely; only
the third makes them harmless.

## 4. What is MCP?

A protocol for exposing tools to a model. A server declares tools with typed
input schemas; a client discovers them and calls them over a defined transport
(here, streamable HTTP). The value is that the tool contract lives with the tool,
not with the agent - so the same server can serve any MCP client, and a bad call
is rejected by schema before any code runs.

## 5. Why MCP rather than just calling the APIs?

Honestly: for a system this size, direct calls would work. What MCP buys is a
**boundary**.

- The tool schema is declared once and validated by the framework, so
  `search_flights(origin="Kochi")` fails on the missing arguments before touching
  a provider.
- Everything crossing that boundary is uniform, which is what makes one gateway
  able to authorize, rate limit, cache and log *every* tool without knowing what
  any of them do.
- The servers are independently runnable and independently replaceable.

The proof it was worth it: when a good public MCP server for hotels turned up,
swapping our mock for it changed one adapter and nothing else.

## 6. What does the MCP Gateway do?

It is the only door between the agents and the tools:

authentication (`X-API-Key`), identity (`X-Agent-Name`), authorization (the
per-agent tool table), filtered discovery, routing, rate limiting, caching, and
one structured log line per call.

It also maps failures onto codes the caller can act on: `503` for an unreachable
server (carry on with the other tools), `400` for bad arguments, `429` for rate
limits, `403` for anything unauthorized.

One thing I did not anticipate: it is also where **somebody else's output becomes
our data**. A public MCP server we call returns a tool result that begins
*"IMPORTANT: ... You MUST follow them exactly."* That is a third party issuing
instructions to our model. Almost certainly meant helpfully - but a tool result
is data, not a prompt, so it is stripped at the boundary. That is now my single
best argument for the Gateway existing.

## 7. Why restrict tools per agent?

Because "the model probably will not do that" is not a security property.

Restriction converts a prompt-engineering hope into an enforced invariant. It also
makes the system reviewable: I can answer "can the Accommodation Agent read the
weather?" by looking at one table, rather than reasoning about prompts.

It is the same instinct as least privilege anywhere else.

## 8. How does LangGraph manage state?

One flat `TypedDict` flows through the graph. Each node returns **only the keys it
changes**, and LangGraph merges them. The state is deliberately flat and made of
primitives, lists and dicts, because it has to serialise into a checkpoint.

The checkpointer writes it to Postgres after every node. That is what makes the
workflow resumable, which is what makes human-in-the-loop possible at all - the
process that resumes a trip need not be the process that started it. I tested
that by killing the container: the trip came back mid-workflow and was approved.

## 9. How does re-planning work?

The validator produces a list of errors. The re-planner tries four levers in a
fixed order, cheapest change to the traveller first:

1. a cheaper hotel
2. a cheaper transport option that still respects a stated time limit
3. swap the priciest paid activity for a free one, or drop it
4. move outdoor activities off wet days onto dry ones

Each attempt applies the first lever that changes anything, and removes the option
it used from the pool, so the next attempt moves on. Three attempts, then the
traveller is asked to raise the budget or relax something.

No optimiser. It does not need one, because the validator has the final say
either way - the re-planner only has to produce a candidate.

**The rule that keeps it honest:** a problem is only reported when it can actually
be fixed. Rain is not a defect if there is nowhere drier to move to. The validator
and the re-planner test the *same* conditions, so the loop cannot spin on
something no lever can reach.

## 10. How does human-in-the-loop work?

When a plan validates, the workflow stops on a LangGraph `interrupt()`. The state
is checkpointed and the API returns the plan plus what it is waiting for. A later
call to `/approve`, `/modify` or `/reject` resumes that same node with the answer.

Approval is asked **once per version of the plan**, never per tool call.

Two details I would raise unprompted:

- A **modification is not automatic re-planning.** Automatic re-planning reacts to
  validation errors by squeezing numbers. A modification comes from a person, so
  it changes the *requirements* and re-runs research. That is why "reduce hotel
  cost and add more historical places" does both halves rather than only the
  cheap one.
- A plan we could not rescue is offered as **modify or reject only** - never
  approve, because the output guard would reject it anyway. Earlier it was a dead
  end: the system said "would you like to increase the budget?" and then returned
  `409` when you tried to answer.

## 11. How does LLM fallback work?

```
primary (Groq) --fails--> fallback (Gemini) --fails--> rule-based reading
```

One retry on the primary, one attempt on the fallback. A reply that fails Pydantic
validation counts as a failure, so a malformed answer falls back rather than being
half-trusted.

No router, no scoring, no cost optimiser. The only question this answers is "the
primary is down, can we keep going?".

The third step is what lets the whole system run with **no API keys at all**: a
keyword reader takes over and the response says so. A degraded extraction is
always labelled; it never passes for a normal one.

Two things I learned only by running it against real keys:

- **Retries should only wait when waiting can help.** A rate limit is measured per
  minute and needs the wait; a timeout does not; a daily quota will not reset
  today at all. Waiting five seconds after every failure was costing ten seconds a
  request, doing nothing.
- **Every provider has its own vocabulary.** Groq answers `"trains"` where we
  expect `"train"`; Gemini answers `"historical places"` where we expect
  `"historical"`. Both were being silently dropped, losing a stated preference.
  Normalising provider output is now a standard step.

## 12. How do you reduce hallucination?

Seven mechanisms, in roughly increasing order of strength:

1. Each agent sees only its own domain's tools.
2. The Gateway refuses anything else, whatever the model asks for.
3. Tool schemas are typed and validated before execution.
4. Agents may only return options the tools actually returned.
5. Financial and date arithmetic never touches a model.
6. The plan is validated deterministically, then validated again on the way out.
7. A person approves before anything is final.

I would then say the thing that matters: **these reduce hallucination, they do not
eliminate it.** The model still chooses among real options and writes prose that
could misdescribe them. Anyone claiming zero hallucination is describing a system
that does not use a language model.

## 13. Why deterministic validation?

Because a language model is the wrong tool for arithmetic and for rules, and
because "is this plan valid?" must have the same answer every time.

Concretely: `total = transport + hotel + activities + local transport`, computed in
Python, in one function, called by the validator, the re-planner **and** the output
guard - so those three can never disagree. The output guard re-derives the total
and compares it to the one in the plan, which catches a number that drifted during
re-planning.

The slogan I would use: **the model reasons, Python decides.**

## 14. Why PostgreSQL?

Two different needs, and it happens to serve both.

The **LangGraph checkpointer** writes workflow state after every node. That is
what makes a trip resumable across processes and restarts.

The **`trips` and `trip_events` tables** answer what the checkpointer cannot: list
recent trips, look one up by status, see what happened and when. Two tables, with
the plan itself in JSONB, because trips are queried by id and status, not by hotel
rating.

Postgres is where a trip is *remembered*, not where it is *planned*. Without it
the system still plans, and `/health` says which you are running.

## 15. Why Redis?

Caching and rate limiting. Not queues.

It caches two things: **tool results**, keyed on tool plus sorted arguments; and
**LLM answers**, keyed on prompt plus the shape requested. The second is worth
defending: every call asks for temperature 0, so the same question should give the
same answer, and free tiers are small enough - Gemini's is twenty requests a day -
that re-asking is the difference between a demo that works twice and one that does
not. Failures are never cached; that would turn a blip into an hour of blips.

The same request twice: 3.5 seconds cold, 122 milliseconds warm.

Redis is a speed-up, never a source of truth. With it down, the system plans
normally and the rate limiter falls back to an in-process counter rather than
quietly letting everything through.

## 16. How is MCP failure handled?

It depends on how much failed, and the distinction is the whole point:

- **Part of a domain** - trains down, flights up: the plan continues on what is
  left, and the response says what was missed.
- **A whole leg** - no transport at all, or no hotel: the plan is **rejected**.

The second case is a bug I shipped and then found by testing outages. With every
transport search failing, the system produced a plausible five-day itinerary with
no way to get there, and a total that silently omitted the fare. The agents had
each degraded correctly; nothing was checking that the sum of correct parts was a
complete plan.

The message also has to tell the truth about which happened. Telling someone to
raise their budget when a provider is down would be misleading.

## 17. How is LLM failure handled?

Fall back, then degrade, then say so. See question 11.

The part I would emphasise: a workflow that has already done work does not restart.
The failure happens inside one node; the state from earlier nodes is intact, and
the trip continues. If nothing answers at all, the trip is marked failed with a
clear message rather than a stack trace.

## 18. How would you scale this?

In the order I would actually hit the limits:

1. **Run the research agents in parallel.** They do not depend on each other except
   that hotel dates need the chosen transport. That is the cheapest large win on
   the cold path, and it is sequential today only because merging state
   sequentially was simpler to get right first.
2. **Move the LLM calls off the request path** for anything that does not need to
   be synchronous. The itinerary prose does not.
3. **Scale the app and the gateway separately.** They are already separate
   processes with separate metrics endpoints; the gateway is stateless apart from
   Redis.
4. **Per-tenant rate limits and quotas**, since the current limit is per agent and
   would need to become per user.

What I would *not* do first is add machines. The dominant cost is LLM latency, and
the profile says so: `duration_ms` against `llm.total_duration_ms` against
`llm.waiting_ms`.

## 19. What are the current limitations?

- **Trains and buses are mock.** Every MCP server covering Indian rail or road
  sits behind a paid subscription, and the one free hosted option was unreachable.
  For an Indian travel planner that is the biggest gap - and it is a supply
  problem, not a design one.
- **Flight fares are estimated.** Real airlines, real timetables, invented prices,
  clearly labelled - but the budget arithmetic runs on estimates.
- The re-planner is heuristic, not optimal.
- Single origin and destination; multi-city would ripple through research,
  validation and re-planning.
- The public API has no authentication.
- INR and Indian cities are assumed throughout.

## 20. What would you improve in a real production system?

- **Authentication and per-user ownership** on the public API. It is open today,
  which is fine for a demo and for nothing else.
- **Real fares**, which means a paid aggregator, which changes the cost model.
- **A judged evaluation for itinerary quality.** The 18 cases check structure -
  the right day count, the budget respected, the preference honoured. None of them
  ask whether the itinerary is any *good*. That needs either human raters or an
  LLM judge, and both need care to avoid grading fluency instead of usefulness.
- **Idempotency keys** on the write endpoints.
- **Tracing**, not just metrics, once there is more than one hop worth following.
- **A real secret store.** Keys come from the environment today.

---

## Things that went wrong, and what they taught me

Interviewers ask "tell me about a bug". These are the ones I would use.

**A plan with no way to get there.** With every transport search failing, the
system returned a confident five-day itinerary and a total that omitted the fare.
Each agent had degraded correctly and reported its failure; nothing checked that
the *combination* was complete. Lesson: correct parts do not make a correct whole,
and that is exactly what a deterministic validator is for.

**Buses could never be recommended.** The Transport Agent kept its first five
alternatives in collection order - flights, then trains, then buses - so buses fell
off the end, and the re-planner reads that list. 325 unit tests missed it because
every piece was individually correct. The evaluation suite found it on its first
run. Lesson: unit tests check the parts; you need something that checks the
journey.

**The Docker image was not the tested one.** `requirements.txt` still pinned the
LLM SDKs from before an upgrade, so Docker built with versions two majors older
than anything I had run. Everything passed locally; Gemini failed only in the
container. Lesson: a version pin nobody checks is a comment. There is now a test
comparing every pin to what is installed.

**A key in the logs.** httpx logs the URL it fetches, and an API key sat in the
query string. My redaction covered my own log lines and nothing else. Fixing it
took three attempts: the first coerced every log argument to a string and broke
uvicorn's `%d` formatting, and the second only handled `str` - missing the actual
leak, because httpx passes an `httpx.URL` object. Lesson: instrument the boundary,
not your own good intentions.

**Ten seconds of nothing.** After adding response timing, a request showed ten
seconds that were neither the model nor us. It was retry backoff: waiting five
seconds after every failure, including failures that could never succeed on a
retry - an exhausted daily quota does not come back in five seconds. Lesson: you
cannot fix what you cannot see, and "the model was slow" and "we were slow" need
different fixes.
