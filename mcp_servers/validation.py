"""Parameter checks shared by the MCP servers.

FastMCP validates types from the tool signature; these functions cover the rules
a type cannot express, like "a date must be a real calendar date". Every tool
validates before it touches a provider, so a bad call fails fast and loudly.
"""

from datetime import date as date_type


def validate_date(value: str, field: str = "date") -> str:
    """Accept only a real YYYY-MM-DD date."""
    try:
        date_type.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a YYYY-MM-DD date, got {value!r}") from exc
    return value


def validate_count(value: int, field: str, minimum: int = 1, maximum: int = 20) -> int:
    """Keep counts (passengers, guests, results) inside a sane range."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be a whole number, got {value!r}")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}, got {value}")
    return value
