"""
core/anatomy.py — the Anatomy skill (github.com/wheresryan22/anatomy, MIT),
run by MARK itself.

Anatomy explains a technical idea by building it as a real, working object: an
interactive isometric figure whose moving parts perform the concept, with a
live readout computed from the same model. Upstream it is a skill for coding
agents, who spend one to two hours writing, building and checking a figure.

Here the assistant's own brain does that work:

  1. GPT (Gemini as fallback) gets the skill — SKILL.md, the kit API, the craft
     and motion guides, the smallest worked example — and writes ONE Node build
     script for the figure.
  2. The script is checked (only the kit, Node's fs/path/url — no network, no
     processes) and built with Node's permission model: it may read the skill
     folder and write only its own figure folder.
  3. Headless Chrome photographs the page; the picture and any build or console
     errors go back to the model, which critiques the figure against the skill's
     checklist and returns an improved script. Two rounds fast, four detailed.
  4. The finished page opens in an app window — WebGL on the GPU — and stays in
     tools/anatomy/mark/<figure>/ to reopen any time.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
ROOT = BASE / "tools" / "anatomy"
FIGS = ROOT / "mark"
CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_ALLOWED_IMPORT = re.compile(
    r"^(node:fs|node:path|node:url|fs|path|url|\.\./\.\./kit/[\w.-]+\.mjs|\.\./\.\./scripts/inline-kit\.mjs)$")
_FORBIDDEN = re.compile(
    r"child_process|worker_threads|node:net\b|node:http|node:https|node:dgram|node:os\b|"
    r"\bfetch\s*\(|XMLHttpRequest|WebSocket|\beval\s*\(|new\s+Function\b|process\.env|"
    r"\brequire\s*\(|import\s*\(|rmSync|unlinkSync|rmdirSync")


def installed() -> bool:
    return (ROOT / "SKILL.md").exists() and (ROOT / "kit" / "iso-kit.mjs").exists()


def chrome() -> Path | None:
    return next((c for c in CHROME_CANDIDATES if c.exists()), None)


def slug(concept: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", concept.lower())[:40].strip("-") or "figura"
    return f"{base}-{hashlib.sha1(concept.encode('utf-8')).hexdigest()[:6]}"


def figure_dir(concept: str) -> Path:
    return FIGS / slug(concept)


def existing(concept: str) -> Path | None:
    page = figure_dir(concept) / "page.html"
    return page if page.exists() else None


def list_figures() -> list[dict]:
    out = []
    for d in sorted(FIGS.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = d / "meta.json"
        if (d / "page.html").exists() and meta.exists():
            try:
                out.append({**json.loads(meta.read_text(encoding="utf-8")), "dir": str(d)})
            except Exception:
                pass
    return out


# ── what the model is given ──────────────────────────────────────────────────

def _read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


def _skill_context() -> str:
    skill = re.sub(r"^---.*?---\s*", "", _read("SKILL.md"), flags=re.S)
    parts = [("SKILL.md", skill), ("references/kit.md", _read("references/kit.md")),
             ("references/craft.md", _read("references/craft.md")),
             ("references/motion.md", _read("references/motion.md")),
             ("references/walkthrough.md", _read("references/walkthrough.md")),
             ("examples/dial-indicator/build.mjs (the smallest complete example — your template)",
              _read("examples/dial-indicator/build.mjs"))]
    return "\n\n".join(f"===== {name} =====\n{text}" for name, text in parts if text)


_RULES = """You are running the Anatomy skill (below) inside MARK, a voice assistant, with no tools of
your own: MARK builds, photographs and shows what you write. Produce exactly ONE complete Node ESM
build script, `build.mjs`, for a framework-free standalone HTML figure, following the skill.

Hard constraints (the script is rejected otherwise):
- Imports allowed: "node:fs" (writeFileSync only), "node:path", "node:url", "../../kit/<file>.mjs",
  "../../scripts/inline-kit.mjs". No other modules, no network, no processes, no environment.
- It lives in tools/anatomy/mark/<figure>/build.mjs — so the kit is at ../../kit/ exactly as in the
  example — and writes `page.html` next to itself (dark theme) with
  writeFileSync(join(HERE, theme === "light" ? "page-light.html" : "page.html"), k.pageHtml({...})).
- Size it as a **Figure** (one mechanism, 40–80 parts, SVG; at most one WebGL layer, only if the
  idea truly needs light, fire or water). Keep it to one file of roughly 250–700 lines.
- The page's own script (live motion, readout, interaction) is plain JS passed to pageHtml as
  `script: kitScript() + live`, like the example.
- Text in the figure (title, hint, readout, legend, caption) in the user's language: Brazilian
  Portuguese.
- Every number shown is true and comes from a small model in the script.

Answer with the complete file in one ```js code block and nothing else."""

_REVIEW = """Here is your figure as MARK built and photographed it (1400×950, after 4 s of motion), with any
build or console errors below. Review it as the skill's critic would (SKILL.md "Verify like a critic"
and the craft rules): Is it instantly a real, recognisable machine whose working IS the concept? Rich
in small precise parts with volume, four-tone shading, correct painter's order, lines that end on
something, no bare faces or empty corners? Card framed (Fig n, title, hint, live readout, legend,
caption), the figure filling 55–65% of the card, calm motion? Fix every error and every fault you see,
and push the detail further. Return the COMPLETE improved build.mjs in one ```js block, nothing else."""


# ── one model call ───────────────────────────────────────────────────────────

def _ask(messages_text: str, image: bytes | None, detailed: bool, log) -> str:
    """The script from GPT (or Gemini). Returns the code."""
    try:
        from core import openai_brain
        if openai_brain.enabled():
            deep, _fast = openai_brain.pick_models()
            out = openai_brain.respond(_RULES + "\n\n" + _skill_context(), messages_text, deep,
                                       effort="high" if detailed else "medium", timeout=900,
                                       images=[(image, "image/png")] if image else None)
            return _code_of(out)
    except Exception as e:
        log(f"GPT indisponível ({str(e)[:80]}) — usando o Gemini")
    from core import gemini
    from google.genai import types
    cl = gemini.client(timeout_ms=600_000)
    contents = ([types.Part.from_bytes(data=image, mime_type="image/png"), messages_text]
                if image else messages_text)
    for model in ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-flash-latest"):
        if gemini._cooling(model):
            continue
        try:
            resp = cl.models.generate_content(
                model=model, contents=contents,
                config=types.GenerateContentConfig(system_instruction=_RULES + "\n\n" + _skill_context()))
            return _code_of(resp.text or "")
        except Exception as e:
            if gemini.is_quota_error(str(e)):
                gemini._cool(model)
    raise RuntimeError("no model could write the figure")


def _code_of(text: str) -> str:
    m = re.search(r"```(?:js|javascript|mjs)?\s*\n(.*?)```", text, re.S)
    code = (m.group(1) if m else text).strip()
    if "import" not in code[:400]:
        raise RuntimeError("the answer had no build script")
    return code + "\n"


# ── check, build, photograph ─────────────────────────────────────────────────

def check_script(code: str) -> str | None:
    """None when the script only uses what it may; else why not."""
    for spec in re.findall(r"""^\s*import\s[^'"]*?from\s+['"]([^'"]+)['"]""", code, re.M):
        if not _ALLOWED_IMPORT.match(spec):
            return f"import not allowed: {spec}"
    for spec in re.findall(r"""^\s*import\s+['"]([^'"]+)['"]""", code, re.M):
        if not _ALLOWED_IMPORT.match(spec):
            return f"import not allowed: {spec}"
    m = _FORBIDDEN.search(code)
    if m:
        return f"forbidden construct: {m.group(0)}"
    return None


def build(fig: Path) -> tuple[bool, str]:
    """node build.mjs under the permission model. (ok, stderr+stdout tail)."""
    r = subprocess.run(
        ["node", "--permission", f"--allow-fs-read={ROOT}{chr(92)}*",
         f"--allow-fs-write={fig}{chr(92)}*", str(fig / "build.mjs")],
        cwd=str(fig), capture_output=True, timeout=180, creationflags=NO_WINDOW)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")[-4000:]
    return r.returncode == 0 and (fig / "page.html").exists(), out


def photograph(page: Path, out_png: Path) -> str:
    """Headless Chrome screenshot after 4 s of the page's own motion; returns
    any console errors it logged."""
    ch = chrome()
    if ch is None:
        return "no Chrome/Edge found for the screenshot"
    prof = page.parent / ".chrome-profile"
    r = subprocess.run(
        [str(ch), "--headless=new", "--hide-scrollbars", "--window-size=1400,950",
         "--virtual-time-budget=4000", "--enable-logging=stderr", "--v=0",
         f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check",
         f"--screenshot={out_png}", page.as_uri()],
        capture_output=True, timeout=90, creationflags=NO_WINDOW)
    log = r.stderr.decode("utf-8", "replace")
    errs = [ln for ln in log.splitlines()
            if "CONSOLE" in ln and ("rror" in ln or "Uncaught" in ln)]
    return "\n".join(errs[-20:])


# ── the whole job ────────────────────────────────────────────────────────────

def make(concept: str, detailed: bool = False, log=print) -> Path:
    """Build a figure for `concept`; returns the page path. Raises on failure."""
    if not installed():
        raise RuntimeError("the Anatomy skill is not installed in tools/anatomy")
    fig = figure_dir(concept)
    fig.mkdir(parents=True, exist_ok=True)
    rounds = 4 if detailed else 2
    t0 = time.time()
    log(f"🧩 Anatomy: projetando '{concept}' ({'detalhado' if detailed else 'rápido'}, "
        f"{rounds} rodadas de revisão)…")
    prompt = (f"Concept to explain: {concept}\n\nFirst decide (silently) the true numbers, the object "
              f"whose working is the concept and the parts list, then write the build script.")
    image = None
    last_good = None
    for i in range(rounds + 1):
        code = _ask(prompt, image, detailed, log)
        why = check_script(code)
        if why:
            prompt = (f"{_REVIEW}\n\nYour previous script was REJECTED before building: {why}. "
                      f"Remove it and keep within the allowed imports.\n\nPrevious script:\n```js\n{code}```")
            image = None
            log(f"🧩 Anatomy: script recusado na checagem ({why}) — pedindo correção")
            continue
        (fig / "build.mjs").write_text(code, encoding="utf-8")
        ok, out = build(fig)
        if not ok:
            prompt = (f"{_REVIEW}\n\nThe build FAILED:\n{out}\n\nPrevious script:\n```js\n{code}```")
            image = None
            log(f"🧩 Anatomy: a montagem falhou na rodada {i + 1} — corrigindo")
            continue
        last_good = code
        shot = fig / "shot.png"
        console = photograph(fig / "page.html", shot)
        log(f"🧩 Anatomy: rodada {i + 1}/{rounds + 1} pronta ({time.time() - t0:.0f}s)")
        if i == rounds:
            break
        image = shot.read_bytes() if shot.exists() else None
        prompt = (f"{_REVIEW}\n\nBuild output:\n{out[-1500:] or '(clean)'}\n\nConsole errors:\n"
                  f"{console or '(none)'}\n\nPrevious script:\n```js\n{code}```")
    if last_good is None:
        raise RuntimeError("no version of the figure built successfully")
    if (fig / "build.mjs").read_text(encoding="utf-8") != last_good:   # last attempt broke it
        (fig / "build.mjs").write_text(last_good, encoding="utf-8")
        build(fig)
        photograph(fig / "page.html", fig / "shot.png")
    (fig / "meta.json").write_text(json.dumps({"concept": concept, "detailed": detailed,
                                               "made": time.strftime("%Y-%m-%d %H:%M"),
                                               "seconds": round(time.time() - t0)},
                                              ensure_ascii=False), encoding="utf-8")
    return fig / "page.html"


def open_window(page: Path) -> None:
    """Show the figure in an app window — Chrome/Edge, WebGL on the GPU."""
    ch = chrome()
    if ch is None:
        import os
        os.startfile(str(page))
        return
    subprocess.Popen([str(ch), f"--app={page.as_uri()}", "--window-size=1440,1000",
                      "--new-window"], creationflags=NO_WINDOW)
