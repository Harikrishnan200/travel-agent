"""Travel Manager: the coordinator.

It calls no travel tools of its own. Its job is to turn trip requirements into a
scoped *brief* for each specialised agent, and to merge their structured results
back into the workflow state.

The briefs matter architecturally: an agent receives only the fields its domain
needs, not the whole TravelState. That keeps agents independent of each other
and makes it obvious in review what each one is allowed to know.
"""

# How the budget is split before the agents go shopping. The manager decides
# this, not the agents, so nobody spends the same rupee twice.
TRANSPORT_SHARE = 0.35
HOTEL_SHARE = 0.40
# The remaining 25% covers activities and local transport.


def build_transport_brief(state: dict) -> dict:
    preferences = state.get("preferences") or {}
    return {
        "origin": state["origin"],
        "destination": state["destination"],
        "date": state["start_date"],
        "travelers": state["travelers"],
        "preferred_mode": preferences.get("transport_mode"),
        "max_journey_hours": preferences.get("max_journey_hours"),
        "max_total_price": _share(state.get("budget"), TRANSPORT_SHARE),
    }


def build_accommodation_brief(state: dict) -> dict:
    """Hotel budget is what is left after the transport actually chosen."""
    budget = state.get("budget")
    cap = None
    if budget is not None:
        remaining = max(budget - _transport_cost(state), 0)
        # Never allocate more than what is actually left, even if the share allows it.
        cap = min(budget * HOTEL_SHARE, remaining * 0.7)

    return {
        "city": state["destination"],
        "check_in": state["start_date"],
        "check_out": state["end_date"],
        "travelers": state["travelers"],
        "max_total_price": cap,
    }


def build_experience_brief(state: dict) -> dict:
    return {
        "origin": state["origin"],
        "city": state["destination"],
        "start_date": state["start_date"],
        "duration_days": state["duration_days"],
        "interests": state.get("interests") or [],
        "travelers": state["travelers"],
    }


def _share(budget: float | None, fraction: float) -> float | None:
    """A slice of the budget, or no cap at all when the traveller gave no budget."""
    return None if budget is None else budget * fraction


def _transport_cost(state: dict) -> float:
    """Return fare for everyone. Duplicated nowhere else: compute_costs uses the same rule."""
    transport = state.get("selected_transport") or {}
    return transport.get("price_per_person", 0) * state.get("travelers", 1) * 2


def summarize_for_human(state: dict) -> str:
    """The plan summary shown at the human review step."""
    transport = state.get("selected_transport") or {}
    hotel = state.get("selected_hotel") or {}
    costs = state.get("cost_breakdown") or {}

    lines = [
        "TRAVEL PLAN",
        f"{state.get('origin')} -> {state.get('destination')}",
        f"Travellers: {state.get('travelers')}   Duration: {state.get('duration_days')} days",
        f"Dates: {state.get('start_date')} to {state.get('end_date')}",
        "",
        f"Transport: {transport.get('mode', '-')} {transport.get('provider', '')} "
        f"({transport.get('duration_hours', '-')}h, "
        f"Rs {transport.get('price_per_person', 0):,.0f} per person)",
        f"Hotel: {hotel.get('name', '-')} in {hotel.get('location', '-')} "
        f"(rating {hotel.get('rating', '-')}, Rs {hotel.get('price_per_night', 0):,.0f}/night)",
        "",
        "Estimated cost:",
    ]
    for label in ("transport", "hotel", "activity", "local_transport"):
        lines.append(f"  {label.replace('_', ' ').title():<18} Rs {costs.get(label, 0):>10,.0f}")
    lines.append(f"  {'Total':<18} Rs {state.get('total_cost', 0):>10,.0f}")
    lines.append(f"  {'Budget':<18} Rs {state.get('budget', 0):>10,.0f}")

    if state.get("degraded"):
        lines += ["", "Note: " + "; ".join(state["degraded"])]

    return "\n".join(lines)
