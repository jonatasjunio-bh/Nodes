"""
Daily briefing — the opening greeting's data, on demand, plus a voice agenda.

    "give me the briefing"            → weather, agenda, national + city news
    "what do I have tomorrow?"        → agenda (linked calendars + noted events)
    "note dentist on Friday at 2pm"   → add_event
    "news about my city" / "news about elections"
    "I live in Campinas"              → set_city

The data itself comes from core/briefing.py, which the startup briefing in
main.py uses too, so asking for it later says exactly what the greeting said.
"""

from __future__ import annotations

from core import briefing


def _news_lines(items: list[dict]) -> str:
    return "\n".join(f"• {n['title']}" + (f" ({n['source']})" if n.get("source") else "")
                     for n in items) or "nothing found"


def _range(p: dict, value: str):
    """(first, last) dates for an agenda request, or None for a single day.
    Accepts date / end_date (YYYY-MM-DD) or a period word in value."""
    from datetime import date, timedelta
    today = date.today()
    v = value.lower().strip()
    try:
        if p.get("date"):
            first = date.fromisoformat(str(p["date"]))
            last = date.fromisoformat(str(p["end_date"])) if p.get("end_date") else first
            return first, last
    except ValueError:
        raise ValueError("dates must be YYYY-MM-DD")
    if v in ("week", "this week", "semana", "esta semana", "essa semana"):
        return today, today + timedelta(days=6 - today.weekday())
    if v in ("next week", "semana que vem", "próxima semana", "proxima semana"):
        start = today + timedelta(days=7 - today.weekday())
        return start, start + timedelta(days=6)
    if v in ("7 days", "next 7 days", "próximos 7 dias", "proximos 7 dias"):
        return today, today + timedelta(days=6)
    if v in ("month", "this month", "mês", "mes", "este mês", "este mes"):
        nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        return today, nxt - timedelta(days=1)
    if v in ("next month", "mês que vem", "mes que vem", "próximo mês", "proximo mes"):
        first = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        return first, nxt - timedelta(days=1)
    return None


def _agenda_changed() -> None:
    """An event was added or removed: the HUD's month agenda shows it now,
    not at its next 5-minute refresh."""
    try:
        from core import hud_data
        hud_data.refresh_agenda_soon()
    except Exception:
        pass


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action", "briefing")).strip().lower()
    value = str(p.get("value", "") or "").strip()
    day = 1 if str(p.get("day", "")).lower() in ("tomorrow", "amanhã", "amanha") else 0
    try:
        if action == "briefing":
            b = briefing.gather()
            text = briefing.render(b)
            if player:
                try:
                    player.show_content("DAILY BRIEFING", text)
                except Exception:
                    pass
            hint = ("" if b.get("city") else
                    " The user's city is unknown: ask where they live and call this tool "
                    "with action=set_city.")
            return text + "\n\nSummarise briefly; the full text is on screen." + hint
        if action == "agenda":
            rng = _range(p, value)
            if rng is None:
                return briefing.agenda_text(briefing.agenda(day), day)
            first, last = rng
            if first == last:
                from datetime import date
                return briefing.agenda_text(briefing.events_between(first, last),
                                            (first - date.today()).days)
            text = briefing.range_text(briefing.events_between(first, last), first, last)
            if player and text.count("\n") > 6:
                try:
                    player.show_content("AGENDA", text)
                except Exception:
                    pass
            return text
        if action == "find_event":
            return briefing.find_events(value)
        if action == "add_event":
            try:
                date_s, time_s = str(p.get("date", "")), str(p.get("time", "") or "")
                cal_req = str(p.get("calendar", "") or "").strip()
                low = cal_req.lower()
                from core import google_calendar as gcal
                g_on = gcal.connected()
                # the shared calendar's own names (the people it is shared with) come from
                # the settings, never from the code
                icloud_words = ("icloud", "apple", "compartilhad", "shared") + tuple(
                    a.strip().lower() for a in str(briefing._cfg().get("icloud_aliases") or "").split(",")
                    if a.strip())
                to_google = g_on and low != "local" and not any(w in low for w in icloud_words) and (
                    gcal.has_calendar(cal_req)
                    or (not low and str(briefing._cfg().get("default_calendar") or "google") == "google"))
                if to_google:
                    try:
                        return gcal.add_event(value, date_s, time_s, int(p.get("minutes") or 60),
                                              str(p.get("where", "") or ""), cal_req)
                    except Exception as e:
                        return (f"NOT ADDED — the Google calendar refused it ({type(e).__name__}: {e}). "
                                f"Nothing was saved; tell the user.")
                # Google asked for but not connected for writing: say so plainly —
                # before, the event went to iCloud and was reported as Google.
                if any(w in low for w in ("google", "gmail")) and not g_on:
                    return ("NOT ADDED — nothing was saved. The Google calendar is not connected for "
                            "writing yet (only read through its iCal address). It is set up once in "
                            "⚙ → Central de Módulos → Briefing Diário → TESTAR / CONECTAR GOOGLE. "
                            "Tell the user, and offer the iCloud calendar meanwhile.")
                # A linked iCloud is the user's real calendar: events go there
                # (and so to the iPhone) unless they ask for a local note.
                if all(briefing._icloud_creds()) and cal_req.lower() != "local":
                    try:
                        return briefing.icloud_add_event(
                            value, date_s, time_s, int(p.get("minutes") or 60),
                            str(p.get("where", "") or ""), cal_req)
                    except briefing.CalendarNotFound as e:
                        extra = ""
                        if g_on:
                            try:
                                extra = " Google calendars: " + ", ".join(
                                    gcal._name(c) for c in gcal.calendars()
                                    if c.get("accessRole") in ("owner", "writer")) + "."
                            except Exception:
                                pass
                        return f"NOT ADDED — nothing was saved: {e}.{extra} Ask which calendar to use."
                    except Exception as e:
                        return (f"Could not add it to iCloud ({type(e).__name__}: {e}). "
                                + briefing.add_event(value, date_s, time_s) + " (noted locally instead)")
                return briefing.add_event(value, date_s, time_s)
            finally:
                _agenda_changed()
        if action == "remove_event":
            date_s = str(p.get("date", "") or "")
            local = briefing.remove_event(value, date_s)
            _agenda_changed()
            from core import google_calendar as gcal
            g_found = []
            if gcal.connected():
                try:
                    g_found = gcal.matches(value, date_s)
                except Exception as e:
                    print(f"[Briefing] Google search for removal failed: {e}")
            icloud = all(briefing._icloud_creds())
            if not icloud and not g_found:
                return local
            # Deleting from the real calendars cannot be undone: the user
            # confirms on the HUD first.
            from core import confirm
            where = " + ".join(x for x, on in (("Google", g_found), ("iCloud", icloud)) if on)

            def _delete():
                out = []
                try:
                    if g_found:
                        out.append(gcal.remove_events(g_found))
                    if icloud:
                        out.append(briefing.icloud_remove_event(value, date_s))
                finally:
                    _agenda_changed()
                return " ".join(out)
            return confirm.request(
                "calendar_delete", f"Delete '{value}' from {where} calendar",
                f"{value}" + (f" on {date_s}" if date_s else " (next 6 months)")
                + (f" — {len(g_found)} on Google" if g_found else ""), _delete)
        if action == "news":
            scope = value.lower()
            if scope in ("", "national", "nacional", "brasil", "brazil"):
                return "National news:\n" + _news_lines(briefing.news_national(6))
            if scope in ("city", "cidade", "local"):
                c = briefing.city()
                if not c:
                    return "The user's city is unknown — ask, then use action=set_city."
                return f"News from {c}:\n" + _news_lines(briefing.news_city(c, 6))
            return f"News about {value}:\n" + _news_lines(briefing.news_topic(value, 6))
        if action == "set_city":
            if not value:
                return "Which city?"
            from memory.config_manager import save_plugin_config
            from memory.memory_manager import update_memory
            save_plugin_config(briefing.NAMESPACE, {"city": value})
            update_memory({"identity": {"city": {"value": value}}})
            w = briefing.weather(value)
            if w and w.get("error"):
                return f"Saved '{value}', but {w['error']} — ask the user to spell it or add the state."
            return f"Saved: the user lives in {w['place'] if w else value}."
        return f"Unknown briefing action '{action}'."
    except Exception as e:
        print(f"[Briefing] {action} failed: {e}")
        return f"Briefing: {action} failed — {e}"


PLUGIN = {
    "name": "daily_briefing",
    "description": (
        "The user's day: 'briefing' (weather, today's agenda, national and "
        "city news — use for 'give me the briefing', 'what's my day like'); "
        "'agenda' (any period: day=today|tomorrow, or date=YYYY-MM-DD with "
        "optional end_date, or value=week|next week|month|next month — all linked "
        "calendars, iCloud included); 'find_event' (value=text — 'when is my "
        "dentist?', searches 30 days back to 6 months ahead); 'add_event' "
        "(value=title, date=YYYY-MM-DD, time=HH:MM optional, minutes=duration, "
        "where=place, calendar='google' (the user's personal/main calendar), "
        "a Google or iCloud calendar name (the shared one is iCloud), or 'local'; without "
        "calendar it goes to the default calendar chosen in settings); 'remove_event' (value=title, date "
        "optional — iCloud deletions ask for on-screen confirmation); "
        "'news' (value='national', 'city', or a topic); 'set_city' (value=city, "
        "e.g. 'Campinas, SP' — when the user says where they live). For weather "
        "alone use weather_report; for timed alerts use reminder."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "briefing | agenda | find_event | add_event | remove_event | news | set_city"},
            "value": {"type": "STRING", "description": "Event title, news scope/topic, or city."},
            "day": {"type": "STRING", "description": "agenda only: today (default) or tomorrow"},
            "date": {"type": "STRING", "description": "agenda/add/remove_event: YYYY-MM-DD"},
            "end_date": {"type": "STRING", "description": "agenda: last day of a range, YYYY-MM-DD"},
            "time": {"type": "STRING", "description": "add_event: HH:MM (24h), optional"},
            "minutes": {"type": "STRING", "description": "add_event: duration in minutes (default 60)"},
            "where": {"type": "STRING", "description": "add_event: location, optional"},
            "calendar": {"type": "STRING",
                         "description": "add_event: iCloud calendar name, or 'local'"},
        },
        "required": ["action"],
    },
}


def _test(values: dict):
    """Settings button: check the city resolves and each calendar URL reads."""
    import re
    msgs, ok = [], True
    c = (values.get("city") or "").strip()
    if c:
        try:
            w = briefing.weather(c)
            if w.get("error"):
                ok = False
                msgs.append(w["error"])
            else:
                msgs.append(f"{w['place']}: {w['now']}°C")
        except Exception as e:
            ok = False
            msgs.append(f"weather: {type(e).__name__}")
    from datetime import datetime, timedelta
    start = datetime.combine(datetime.now().date(), datetime.min.time())
    for u in [u for u in re.split(r"[\s,;]+", values.get("calendar_urls") or "") if u]:
        try:
            n = len(briefing._ics_events(u, start, start + timedelta(days=1)))
            msgs.append(f"calendar OK ({n} events today)")
        except Exception as e:
            ok = False
            msgs.append(f"calendar failed ({type(e).__name__})")
    # Google Calendar (read + write through the API)
    from core import google_calendar as gcal
    gpath = (values.get("google_client_file") or "").strip()
    try:
        if gpath:
            gcal.install_client_file(gpath)
        if gcal.CLIENT_FILE.exists() and not gcal.connected():
            msgs.append("Google: approve access in the browser window…")
            name = gcal.connect()
            msgs[-1] = f"Google connected ({name})"
        elif gcal.connected():
            n = len(gcal.events(start, start + timedelta(days=1)))
            msgs.append(f"Google OK — {gcal.primary_name()} ({n} events today)")
    except Exception as e:
        ok = False
        msgs.append(f"Google: {type(e).__name__}: {str(e)[:120]}")
    user, pwd = briefing._icloud_creds(values)
    if user or pwd:
        if not (user and pwd):
            ok = False
            msgs.append("iCloud: fill in both the Apple ID and the app-specific password")
        else:
            try:
                n = len(briefing.icloud_events(start, start + timedelta(days=1), values))
                msgs.append(f"iCloud OK ({n} events today)")
            except Exception as e:
                ok = False
                msgs.append("iCloud: Apple refused the login — use an app-specific password, "
                            "not the account password" if briefing._is_auth_error(e)
                            else f"iCloud failed ({type(e).__name__})")
    return ok, " | ".join(msgs) or "Fill in the city or a calendar first."


PLUGIN_SETTINGS = {
    "namespace": briefing.NAMESPACE,
    "title": "Daily briefing — city, calendar, news",
    "fields": [
        {"key": "city", "label": "Your city (e.g. Campinas, SP)", "type": "text"},
        {"key": "calendar_urls", "label": "Calendar iCal address(es) — separate with spaces",
         "type": "password",
         "placeholder": "Google Calendar → Settings → your calendar → Secret address in iCal format"},
        {"key": "google_client_file", "label": "Google Agenda — arquivo de credencial OAuth (.json, "
                                                "tipo 'App para computador')", "type": "text",
         "placeholder": r"C:\Users\voce\Downloads\client_secret_....json  → depois TESTAR / CONECTAR GOOGLE"},
        {"key": "google_aliases", "label": "Outros nomes da sua agenda do Google (separados por vírgula)",
         "type": "text", "placeholder": "Seu Nome, Apelido"},
        {"key": "icloud_aliases", "label": "Outros nomes da agenda compartilhada do iCloud (separados por vírgula)",
         "type": "text", "placeholder": "nome de quem compartilha com você"},
        {"key": "default_calendar", "label": "Agenda padrão para novos eventos", "type": "choice",
         "options": ["google", "icloud"]},
        {"key": "icloud_apple_id", "label": "iCloud — Apple ID (e-mail)", "type": "text"},
        {"key": "icloud_app_password", "label": "iCloud — app-specific password", "type": "password",
         "placeholder": "appleid.apple.com → Sign-In and Security → App-Specific Passwords"},
        {"key": "news_country", "label": "National news from", "type": "choice",
         "options": ["BR", "PT", "US"]},
    ],
    "action": {"label": "TESTAR / CONECTAR GOOGLE", "run": _test},
}
