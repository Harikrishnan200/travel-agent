"""Prompts. Kept in one file so the wording is easy to find and change."""

EXTRACT_REQUEST_SYSTEM = """You extract travel requirements from a traveller's message.

Rules:
- Return only what the traveller actually said. Never invent a budget, a date or
  a destination that is not there; leave the field null instead.
- Cities are Indian cities, spelled in full (Kochi, Delhi, Bangalore, Jaipur...).
- Budgets are in Indian rupees. "50k" and "50,000" both mean 50000.
- If the traveller gives a duration ("5 days") rather than dates, set
  duration_days and leave start_date null.
- transport_mode is only set when a preference is stated ("prefer trains").
- max_journey_hours is only set when a time limit is stated ("under 12 hours").
- interests must come from: historical, nature, food, culture, adventure, shopping.
- is_travel_request is false for anything that is not about planning a trip.
"""

EXTRACT_REQUEST_USER = "Traveller's message:\n{message}"
