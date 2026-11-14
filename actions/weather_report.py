"""
Weather — real numbers, spoken, instead of a browser tab.

The old version opened a Google search and said "showing the weather", which
left the assistant unable to answer "will it rain this afternoon?" because it
never saw the forecast itself. Data now comes from Open-Meteo through
core/briefing.py (free, no key), the same source the opening briefing uses.
"""

from core import briefing


def weather_action(parameters: dict, player=None) -> str:
    city = (parameters.get("city") or "").strip() or briefing.city()
    when = (parameters.get("day") or parameters.get("time") or "today").strip().lower()
    day = 1 if when in ("tomorrow", "amanhã", "amanha") else 0
    if not city:
        return ("I don't know which city — ask the user where they are, save it with "
                "save_memory (category identity, key city), then try again.")
    try:
        text = briefing.weather_text(briefing.weather(city, day), day)
    except Exception as e:
        text = f"The weather service did not answer ({type(e).__name__})."
    print(f"[Weather] {text}")
    if player:
        try:
            player.write_log(f"[weather] {text}")
        except Exception:
            pass
    return text


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "weather_report",
    "description": (
        "Current weather and today's or tomorrow's forecast: temperature now, "
        "min/max, sky, rain chance and the hours rain is likely. Use for any "
        "weather, temperature or 'will it rain' question. City is optional — "
        "the user's saved city is used when omitted."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {"type": "STRING", "description": "City name, optional (e.g. 'Campinas, SP')"},
            "day": {"type": "STRING", "description": "'today' (default) or 'tomorrow'"},
        },
        "required": [],
    },
    "handler": weather_action,
}
