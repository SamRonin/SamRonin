#!/usr/bin/env python3
"""
SamRonin — profile generator.

Reads public GitHub data and rebuilds every SVG asset plus the README.
Deterministic: identical data in => byte-identical files out (no timestamps),
so the scheduled workflow only commits when something actually changed.

Safety: only PUBLIC, non-fork, non-archived repositories are ever read into
the output. A guard at the end fails the build if any private repo name leaks.

Usage:
    GITHUB_TOKEN=... python3 scripts/profile.py [--login SamRonin]

Offline test-suite (no token, no network):
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")

# ─────────────────────────────────────────── design tokens
BG, PANEL = "#0B0B0D", "#101014"
INK, DIM, MUTED = "#F2EAD9", "#B9B3A5", "#8C8779"
RED, RED_HI, RED_DK = "#E63946", "#FF4D5A", "#8E1B22"
SANS = "Inter,-apple-system,'Segoe UI','Helvetica Neue',Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

LANG_COLOR = {
    "TypeScript": "#3178C6", "JavaScript": "#F1E05A", "Rust": "#DEA584",
    "Python": "#3572A5", "HTML": "#E34C26", "CSS": "#563D7C", "Shell": "#89E051",
    "C++": "#F34B7D", "C#": "#178600", "Go": "#00ADD8", "Java": "#B07219",
    "Swift": "#F05138", "Kotlin": "#A97BFF", "Dart": "#00B4AB", "Vue": "#41B883",
    "Svelte": "#FF3E00", "Lua": "#000080", "Ruby": "#701516", "PHP": "#4F5D95",
    "PowerShell": "#012456", "Jupyter Notebook": "#DA5B0B", "C": "#555555",
}

# topic -> pretty pill label, in priority order
TOPIC_LABEL = {
    "chrome-extension": "CHROME MV3", "manifest-v3": "MV3",
    "tauri": "TAURI", "tauri-v2": "TAURI V2", "rust": "RUST",
    "local-first": "LOCAL-FIRST", "privacy": "PRIVACY", "offline": "OFFLINE",
    "llm": "LLM", "ai-tools": "AI", "markdown": "MARKDOWN",
    "downloader": "DOWNLOADER", "converter": "CONVERTER", "ffmpeg": "FFMPEG",
    "image-compression": "COMPRESSION", "video-compression": "COMPRESSION",
    "qa": "QA", "testing": "TESTING", "synthetic-data": "SYNTHETIC DATA",
    "form-filler": "FORMS", "developer-tools": "DEVTOOLS",
    "rtl": "RTL", "persian": "PERSIAN", "farsi": "PERSIAN", "i18n": "I18N",
    "react": "REACT", "typescript": "TS", "open-source": "OPEN SOURCE",
    "productivity": "PRODUCTIVITY", "dark-mode": "DARK MODE", "cli": "CLI",
    "automation": "AUTOMATION", "bot": "BOT", "telegram": "TELEGRAM",
}
FRAMEWORK_TOPICS = {
    "react": "React", "vite": "Vite", "tailwindcss": "Tailwind", "tailwind": "Tailwind",
    "tauri": "Tauri v2", "nextjs": "Next.js", "next": "Next.js", "vue": "Vue",
    "svelte": "Svelte", "electron": "Electron", "express": "Express",
    "fastapi": "FastAPI", "flask": "Flask", "discordjs": "discord.js",
}
SURFACE_TOPICS = {
    "chrome-extension": "Chrome MV3", "manifest-v3": "Chrome MV3",
    "tauri": "Desktop", "desktop-app": "Desktop", "electron": "Desktop",
    "cli": "CLI", "telegram": "Telegram", "bot": "Telegram",
    "web": "Web", "api": "API", "android": "Android",
}
CRAFT_TOPICS = {
    "local-first": "Local-first", "privacy": "Privacy", "offline": "Offline",
    "rtl": "RTL / Persian UI", "persian": "RTL / Persian UI",
    "open-source": "Open source", "i18n": "i18n", "accessibility": "A11y",
}

# ─────────────────────────────────────────── GitHub API
API = "https://api.github.com"
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0


class TokenMissing(RuntimeError):
    """No GitHub token — the contribution calendar (GraphQL) cannot be read."""


def _headers(token: str | None, extra: dict | None = None) -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "samronin-profile-generator",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:  # never send an empty Authorization header
        h["Authorization"] = f"Bearer {token}"
    if extra:
        h.update(extra)
    return h


def _rate_limited(e: urllib.error.HTTPError) -> bool:
    if (e.headers or {}).get("X-RateLimit-Remaining") == "0":
        return True
    try:
        return "rate limit" in e.read().decode("utf-8", "ignore").lower()
    except Exception:
        return False


def _retryable(e: Exception) -> bool:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 429 or e.code >= 500:
            return True
        if e.code == 403:  # 403 doubles as "rate limit exhausted" on GitHub
            return _rate_limited(e)
        return False
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))


def _request(url: str, *, data: bytes | None = None, headers: dict | None = None,
             timeout: int = 30) -> Any:
    """urlopen with bounded retry/backoff for transient failures (5xx/429/network)."""
    for attempt in range(RETRY_ATTEMPTS):
        req = urllib.request.Request(url, data=data, headers=headers or {},
                                     method="POST" if data is not None else "GET")
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except Exception as e:
            if not _retryable(e) or attempt == RETRY_ATTEMPTS - 1:
                raise
            retry_after = None
            if isinstance(e, urllib.error.HTTPError):
                retry_after = (e.headers or {}).get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else RETRY_BASE_DELAY * (2 ** attempt)
            except (TypeError, ValueError):
                delay = RETRY_BASE_DELAY * (2 ** attempt)
            time.sleep(min(delay, 30.0))
    raise RuntimeError("unreachable")


def _next_link(link: str | None) -> str | None:
    for part in (link or "").split(","):
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                return m.group(1)
    return None


def gh(path: str, token: str | None) -> Any:
    with _request(API + path, headers=_headers(token)) as r:
        return json.load(r)


def gh_all_pages(path: str, token: str | None, max_pages: int = 20) -> list:
    """Follow GitHub pagination so accounts with >100 repos aren't truncated."""
    items: list = []
    url = API + path
    for _ in range(max_pages):
        with _request(url, headers=_headers(token)) as r:
            data = json.load(r)
            nxt = _next_link(r.headers.get("Link"))
        if not isinstance(data, list):
            return data
        items.extend(data)
        if not nxt:
            return items
        url = nxt
    print(f"::warning::pagination stopped after {max_pages} pages — counts may be incomplete")
    return items


def gql(query: str, token: str | None, variables: dict | None = None) -> dict:
    if not token:
        raise TokenMissing("no GitHub token — the contribution calendar needs one")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    with _request(API + "/graphql", data=body, headers=_headers(token, {
            "Content-Type": "application/json"})) as r:
        out = json.load(r)
    if "errors" in out:
        raise RuntimeError(f"GraphQL error: {out['errors']}")
    return out["data"]


def readme_title(login: str, repo: str, token: str | None) -> str | None:
    """First usable heading of a repo README (one API call, default branch)."""
    try:
        with _request(f"{API}/repos/{login}/{repo}/readme", headers=_headers(token, {
                "Accept": "application/vnd.github.raw+json"})) as r:
            text = r.read(4000).decode("utf-8", "ignore")
    except Exception:
        return None
    heads = []
    # HTML <h1> titles first (common in RTL-first READMEs)
    for m in re.finditer(r"<h1[^>]*>(.*?)</h1>", text, re.S | re.I):
        t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+&!\-]*", t or ""):
            heads.append(t)
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#"):
            continue
        title = re.sub(r"<[^>]+>", "", line.lstrip("#")).strip()
        title = re.sub(r"[*_`\[\]]", "", title).strip()
        if 0 < len(title) <= 32 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+&!\-]*", title):
            heads.append(title)
    if not heads:
        return None
    for h in heads:
        if h.lower().replace(" ", "") == repo.lower():
            return h
    return heads[0]


def list_private_names(token: str | None) -> list[str]:
    """Best-effort names of the token owner's private repos, for the leak guard.

    /users/{login}/repos only ever returns public repos, so without this call
    the privacy guard would have nothing to check. Fails soft: no permission,
    no names, no crash.
    """
    if not token:
        return []
    try:
        return [r.get("name", "") for r in gh_all_pages(
            "/user/repos?visibility=private&per_page=100&type=owner", token, max_pages=5)]
    except Exception:
        return []


def fetch(login: str, token: str | None) -> dict:
    if not token:
        raise TokenMissing("no GitHub token (set GITHUB_TOKEN / PROFILE_TOKEN)")
    user = gh(f"/users/{login}", token)
    raw_repos = gh_all_pages(f"/users/{login}/repos?per_page=100&sort=pushed&type=owner", token)

    private_names = list_private_names(token)
    repos = [r for r in raw_repos if not r.get("private") and not r.get("fork")
             and not r.get("archived") and r["name"].lower() != login.lower()]

    ranked = rank_repos(repos)
    # language bytes for the highest-ranked repos only (stays inside rate limits)
    for r in ranked[:12]:
        try:
            r["_langs"] = gh(f"/repos/{login}/{r['name']}/languages", token)
        except urllib.error.HTTPError:
            r["_langs"] = {}
    ranked = rank_repos(repos)          # re-rank now that language depth is known
    for r in ranked[:8]:
        r["_title"] = readme_title(login, r["name"], token)

    cal = gql("""query($login:String!){user(login:$login){contributionsCollection{
        contributionCalendar{totalContributions weeks{contributionDays{date contributionCount}}}
    }}}""", token, {"login": login})
    cal_user = (cal or {}).get("user") or {}
    calendar = ((cal_user.get("contributionsCollection") or {}).get("contributionCalendar")
                or {})

    return {"user": user, "repos": repos,
            "weeks": calendar.get("weeks") or [],
            "total_contribs": calendar.get("totalContributions") or 0,
            "private_names": private_names}


# ─────────────────────────────────────────── helpers
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2190-\u21FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
RTL_RUN = re.compile(r"[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]+[\s\u200c\u200f\u060c\u061f.!?،؛:\-–—]*")


def clean_text(text: str) -> str:
    """Make repo copy safe for a left-to-right SVG canvas."""
    if not text:
        return ""
    t = RTL_RUN.sub(" ", text)          # drop Persian/Arabic runs + their punctuation
    t = EMOJI.sub("", t)                # drop emoji & pictographs
    t = re.sub(r"\s{2,}", " ", t)       # collapse whitespace
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)  # no space before punctuation
    t = re.sub(r"\s*[|·—–]\s*(?=[|·—–]|$)", " ", t)   # dangling separators
    t = re.sub(r"\s*[|·—–]\s*$", "", t)
    t = re.sub(r"^[\s|·—–-]+", "", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" |·—–-\t")


def md_cell(text: str, limit: int = 96) -> str:
    """One safe markdown-table cell: pipes escaped so the table can't break."""
    return clean_text(text)[:limit].replace("|", "\\|") or "—"


def clip(text: str, n: int) -> str:
    """Truncate at a word boundary (used by the README console block)."""
    t = clean_text(text)
    if len(t) <= n:
        return t
    cut = t.rfind(" ", 0, n)
    return (t[:cut] if cut > 10 else t[:n]).rstrip() + "…"


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mw(s: str, size: float, ls: float = 0.0) -> float:
    return len(s) * (size * 0.60 + ls)


def wrap(text: str, maxw: float, size: float, max_lines: int = 3) -> list[str]:
    words, lines, cur = (text or "").split(), [], ""
    for wd in words:
        while mw(wd, size) > maxw:          # a single word wider than the line
            cut = max(1, int(maxw / (size * 0.60)) - 1)
            piece, wd = wd[:cut], wd[cut:]
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(piece)
        if not wd:
            continue
        t = (cur + " " + wd).strip()
        if mw(t, size) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(4, len(lines[-1]) - 1)].rstrip() + "…"
    return lines


def head(w: int, h: int, extra: str = "") -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" fill="none" role="img" aria-label="SamRonin profile asset">'
            f'<defs>{extra}</defs>')


STYLE = (f'<style>.m{{font-family:{MONO}}}.s{{font-family:{SANS}}}</style>')


def write(name: str, w: int, h: int, body: str, defs: str = "") -> None:
    os.makedirs(ASSETS, exist_ok=True)
    with open(os.path.join(ASSETS, name), "w", encoding="utf-8") as f:
        f.write(head(w, h, defs) + STYLE + body + "</svg>")


# ─────────────────────────────────────────── derived data
def rank_repos(repos: list[dict]) -> list[dict]:
    """Rank by substance first (stars, topics, languages), then recency.

    A repo is only eligible for a featured card if it has a description and
    looks like a real project (>1 KB of code). Brand-new empty repos sort low
    instead of stealing a slot.
    """
    now = dt.datetime.now(dt.timezone.utc)

    def substance(r: dict) -> float:
        # kilobytes of code, log-scaled: 50 KB -> ~1.7, 2 MB -> ~3.3
        kb = r.get("size", 0) or sum((r.get("_langs") or {}).values()) // 1024
        depth = min(3.0, math.log10(max(1, kb) + 1))
        topic_depth = min(1.5, 0.15 * len(r.get("topics") or []))
        return (r.get("stargazers_count", 0) * 4
                + (2.0 if r.get("description") else 0)
                + topic_depth
                + (1.5 if r.get("_langs") else 0)
                + depth)

    def recency(r: dict) -> float:
        # present, but deliberately weak: this profile is curated by substance
        pushed = r.get("pushed_at")
        if not pushed:
            return 0.0
        try:
            pushed_dt = dt.datetime.fromisoformat(str(pushed).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        days = max(1.0, (now - pushed_dt).total_seconds() / 86400)
        return 0.35 / math.sqrt(days)

    for r in repos:
        r["_score"] = substance(r) + recency(r)

    return sorted(repos, key=lambda r: (-r["_score"], r["name"]))


def rank_key(r: dict) -> tuple:
    """Cards first (by score), then empty shells sorted last."""
    return (0 if is_featurable(r) else 1, -r.get("_score", 0), r["name"])


def is_featurable(r: dict) -> bool:
    """A card needs a real description and some actual code.

    `size` is unreliable for repos created in the last day (GitHub caches it),
    so language bytes -- which are live -- take priority.
    """
    if not (r.get("description") or "").strip():
        return False
    return bool(r.get("_langs")) or r.get("size", 0) >= 8


def display_name(r: dict) -> str:
    """Use the repo's own README heading when it has one (DropShrink, Page2LLM...)."""
    title = (r.get("_title") or "").strip()
    if not title or len(title) > 28 or title.lower() == "readme":
        return r["name"]
    # if the README heading is just the repo name in different casing, take the heading's
    # own camel-casing only when the repo name is otherwise all-lowercase
    if title.lower().replace(" ", "") == r["name"].lower():
        return title if r["name"][1:].islower() else r["name"]
    return title


def kicker_for(r: dict) -> str:
    t = set(r.get("topics") or [])
    lang = (r.get("language") or "CODE").upper()
    if t & {"chrome-extension", "manifest-v3"}:
        return "CHROME EXTENSION · MV3"
    if t & {"tauri", "tauri-v2"} or (r.get("language") == "Rust" and "desktop-app" in t):
        return "DESKTOP · TAURI V2 + RUST"
    if "telegram" in t or "bot" in t:
        return f"{lang} · AUTOMATION"
    if "cli" in t:
        return f"{lang} · CLI"
    if r.get("language"):
        return f"{lang} · OPEN SOURCE"
    return "OPEN SOURCE"


def tags_for(r: dict, n: int = 3) -> list[str]:
    picked, seen = [], set()
    for topic in (r.get("topics") or []):
        label = TOPIC_LABEL.get(topic)
        if not label or label in seen:
            continue
        seen.add(label)
        picked.append(label)
        if len(picked) == n:
            break
    if not picked and r.get("language"):
        picked = [r["language"].upper()]
    return picked or ["OPEN SOURCE"]


def top_lang(r: dict) -> tuple[str, int, str]:
    langs = r.get("_langs") or {}
    if langs:
        total = max(1, sum(langs.values()))
        name = r.get("language") or max(langs, key=lambda k: langs[k])
        pct = round(100 * langs.get(name, max(langs.values())) / total)
        return name, max(1, min(99, pct)), LANG_COLOR.get(name, RED)
    name = r.get("language") or "Code"
    return name, 50, LANG_COLOR.get(name, RED)


def build_stack(repos: list[dict]) -> list[tuple[str, list[str]]]:
    lang_bytes: dict[str, int] = {}
    topics: set[str] = set()
    for r in repos:
        for k, v in (r.get("_langs") or {}).items():
            lang_bytes[k] = lang_bytes.get(k, 0) + v
        topics |= set(r.get("topics") or [])

    languages = [k for k, _ in sorted(lang_bytes.items(), key=lambda kv: -kv[1])][:6]
    if not languages:
        languages = sorted({r["language"] for r in repos if r.get("language")})[:6]

    frameworks, surfaces, craft = [], [], []
    for t in topics:
        if t in FRAMEWORK_TOPICS and FRAMEWORK_TOPICS[t] not in frameworks:
            frameworks.append(FRAMEWORK_TOPICS[t])
        if t in SURFACE_TOPICS and SURFACE_TOPICS[t] not in surfaces:
            surfaces.append(SURFACE_TOPICS[t])
        if t in CRAFT_TOPICS and CRAFT_TOPICS[t] not in craft:
            craft.append(CRAFT_TOPICS[t])

    order = ["React", "Vite", "Tailwind", "Tauri v2", "Next.js", "Vue", "Svelte",
             "FastAPI", "Flask", "Express", "Electron"]
    frameworks = sorted(frameworks, key=lambda x: order.index(x) if x in order else 99)[:4]
    surface_order = ["Chrome MV3", "Desktop", "CLI", "Web", "API", "Telegram", "Android"]
    surfaces = sorted(surfaces, key=lambda x: surface_order.index(x) if x in surface_order else 99)[:4]
    craft_order = ["Local-first", "Privacy", "Offline", "RTL / Persian UI", "Open source", "i18n", "A11y"]
    craft = sorted(craft, key=lambda x: craft_order.index(x) if x in craft_order else 99)[:4]

    # no fabricated defaults: with no data we show an honest placeholder
    return [("LANGUAGES", languages or ["—"]),
            ("FRAMEWORKS", frameworks or ["—"]),
            ("SURFACES", surfaces or ["—"]),
            ("CRAFT", craft or ["—"])]


def build_stats(data: dict, repos: list[dict]) -> list[tuple[str, str, str]]:
    created = dt.datetime.fromisoformat(str(data["user"]["created_at"]).replace("Z", "+00:00"))
    years = max(1, (dt.datetime.now(dt.timezone.utc) - created).days // 365)
    # the headline number must match the dynamic README badge exactly
    # (data["user"]["public_repos"] is what the badge queries)
    total_public = int(data["user"].get("public_repos") or len(repos))
    return [(f"{total_public:02d}", "PUBLIC REPOS", "and counting"),
            (f"{years:02d}", "YEAR BUILDING", f"since {created:%b %Y}".lower()),
            (f"{data['total_contribs']}", "CONTRIBUTIONS", "last 52 weeks"),
            ("00", "TELEMETRY", "local-first by design")]


# ─────────────────────────────────────────── assets
def banner(est_year: int | None = None) -> None:
    est_year = est_year or dt.date.today().year
    defs = f'''
    <radialGradient id="sunG" cx="42%" cy="34%" r="72%">
      <stop offset="0" stop-color="#FF5A68"/><stop offset="0.55" stop-color="#D62828"/>
      <stop offset="1" stop-color="#7E1111"/>
    </radialGradient>
    <radialGradient id="haloG" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#44171A"/><stop offset="0.5" stop-color="#180C0D"/>
      <stop offset="1" stop-color="#0B0B0D"/>
    </radialGradient>
    <pattern id="gridP" width="40" height="40" patternUnits="userSpaceOnUse">
      <path d="M40 0H0V40" stroke="#F2EAD9" stroke-opacity="0.035"/>
    </pattern>
    <clipPath id="frameClip"><rect width="1280" height="420" rx="18"/></clipPath>
    <clipPath id="sunClip"><circle cx="0" cy="0" r="112"/></clipPath>
  '''
    write("banner.svg", 1280, 420, f'''
  <g clip-path="url(#frameClip)">
    <rect width="1280" height="420" fill="#0B0B0D"/>
    <rect width="1280" height="420" fill="url(#gridP)"/>
    <circle cx="1132" cy="210" r="212" fill="url(#haloG)">
      <animate attributeName="opacity" values="0.55;1;0.55" dur="7s" repeatCount="indefinite"/>
    </circle>
    <g transform="translate(1132 210) scale(0.8)">
      <g>
        <animateTransform attributeName="transform" type="scale" values="0.86;1" begin="0.15s" dur="1.1s"
          fill="freeze" calcMode="spline" keySplines="0.16 0.9 0.2 1" keyTimes="0;1"/>
        <g opacity="0">
          <animate attributeName="opacity" values="0;1" begin="0.15s" dur="0.9s" fill="freeze"/>
          <circle r="112" fill="url(#sunG)"/>
          <g clip-path="url(#sunClip)" opacity="0.13">
            <g fill="#0B0B0D">
              {''.join(f'<rect x="-120" y="{-112 + i * 8}" width="240" height="1.6"/>' for i in range(29))}
            </g>
          </g>
          <circle r="112" fill="none" stroke="#F2EAD9" stroke-opacity="0.35"/>
        </g>
      </g>
      <circle r="148" fill="none" stroke="#E63946" stroke-opacity="0.55" stroke-width="1.4" pathLength="1"
              stroke-dasharray="0.62 0.38" stroke-dashoffset="1">
        <animate attributeName="stroke-dashoffset" values="1;0" begin="0.45s" dur="1.6s" fill="freeze"/>
        <animateTransform attributeName="transform" type="rotate" values="0;360" dur="60s" repeatCount="indefinite"/>
      </circle>
      <circle r="166" fill="none" stroke="#F2EAD9" stroke-opacity="0.08"/>
    </g>
    <g clip-path="url(#sunClip)" transform="translate(1132 210)">
      <rect x="-148" y="-1.7" width="0" height="3.4" fill="#F2EAD9" opacity="0.9" transform="rotate(-16)">
        <animate attributeName="width" values="0;300;300;0" keyTimes="0;0.055;0.94;1"
                 begin="0.9s" dur="10s" repeatCount="indefinite"/>
      </rect>
      <rect x="-148" y="2.2" width="0" height="1.2" fill="#FF8A93" opacity="0.55" transform="rotate(-16)">
        <animate attributeName="width" values="0;300;300;0" keyTimes="0;0.06;0.94;1"
                 begin="1.05s" dur="10s" repeatCount="indefinite"/>
      </rect>
    </g>
    <g stroke="#E63946" stroke-width="1.4" fill="none" opacity="0.55">
      <path d="M1246 150h-26v-46" pathLength="1" stroke-dasharray="1" stroke-dashoffset="1">
        <animate attributeName="stroke-dashoffset" values="1;0" begin="1.2s" dur="0.9s" fill="freeze"/>
      </path>
      <path d="M1250 286h-24v54" pathLength="1" stroke-dasharray="1" stroke-dashoffset="1">
        <animate attributeName="stroke-dashoffset" values="1;0" begin="1.45s" dur="0.9s" fill="freeze"/>
      </path>
      <path d="M1132 330v52h64" pathLength="1" stroke-dasharray="1" stroke-dashoffset="1">
        <animate attributeName="stroke-dashoffset" values="1;0" begin="1.7s" dur="0.9s" fill="freeze"/>
      </path>
    </g>
    <g fill="#F2EAD9">
      <circle cx="1266" cy="104" r="3.4"><animate attributeName="opacity" values="0.25;1;0.25" dur="2.6s" repeatCount="indefinite"/></circle>
      <circle cx="1246" cy="328" r="3.4"><animate attributeName="opacity" values="0.25;1;0.25" dur="3.1s" begin="0.4s" repeatCount="indefinite"/></circle>
      <circle cx="1116" cy="396" r="3.4"><animate attributeName="opacity" values="0.25;1;0.25" dur="2.2s" begin="0.8s" repeatCount="indefinite"/></circle>
    </g>
    <g opacity="0">
      <animate attributeName="opacity" values="0;1" begin="0.2s" dur="0.6s" fill="freeze"/>
      <rect x="76" y="78" width="22" height="2" fill="#E63946"/>
      <text x="110" y="92" class="m" font-size="14" fill="#E63946" letter-spacing="6.5">// CODE RONIN</text>
      <text x="420" y="92" class="m" font-size="11" fill="#8C8779" letter-spacing="3">EST. {est_year}</text>
    </g>
    <g opacity="0">
      <animate attributeName="opacity" values="0;1" begin="0.5s" dur="0.7s" fill="freeze"/>
      <animateTransform attributeName="transform" type="translate" values="0 26;0 0" begin="0.5s" dur="0.8s"
        fill="freeze" calcMode="spline" keySplines="0.2 0.85 0.2 1" keyTimes="0;1"/>
      <text x="76" y="204" class="s" font-size="106" font-weight="800" fill="#F2EAD9" letter-spacing="8">SAM</text>
      <polygon points="348,204 365,204 398,96 381,96" fill="#E63946"/>
      <text x="408" y="204" class="s" font-size="106" font-weight="800" fill="#F2EAD9" fill-opacity="0.07"
            stroke="#F2EAD9" stroke-width="2" letter-spacing="8">RONIN</text>
    </g>
    <rect x="76" y="236" width="0" height="3" fill="#E63946">
      <animate attributeName="width" values="0;132" begin="0.95s" dur="0.6s" fill="freeze"/>
    </rect>
    <rect x="216" y="237.4" width="0" height="1" fill="#F2EAD9" opacity="0.12">
      <animate attributeName="width" values="0;540" begin="1.1s" dur="0.8s" fill="freeze"/>
    </rect>
    <g opacity="0">
      <animate attributeName="opacity" values="0;1" begin="1.9s" dur="0.4s" fill="freeze"/>
      <line x1="76" y1="237.9" x2="756" y2="237.9" stroke="#E63946" stroke-width="1.8" stroke-linecap="round"
            pathLength="100" stroke-dasharray="10 90" stroke-dashoffset="100">
        <animate attributeName="stroke-dashoffset" values="100;0" dur="8s" begin="1.9s" repeatCount="indefinite"/>
      </line>
      <line x1="76" y1="237.9" x2="756" y2="237.9" stroke="#FF8A93" stroke-width="1.1" stroke-linecap="round"
            opacity="0.7" pathLength="100" stroke-dasharray="3 97" stroke-dashoffset="100">
        <animate attributeName="stroke-dashoffset" values="100;0" dur="8s" begin="-2.4s" repeatCount="indefinite"/>
      </line>
    </g>
    <g opacity="0">
      <animate attributeName="opacity" values="0;1" begin="1.05s" dur="0.7s" fill="freeze"/>
      <text x="76" y="278" class="m" font-size="19" fill="#FF4D5A">❯</text>
      <text x="104" y="278" class="m" font-size="18" fill="#B9B3A5">local-first systems · zero telemetry · persian-first UI</text>
    </g>
    <g opacity="0">
      <animate attributeName="opacity" values="0;1" begin="1.3s" dur="0.7s" fill="freeze"/>
      <text x="76" y="356" class="m" font-size="12.5" fill="#8C8779" letter-spacing="2.4">TEHRAN · IRAN</text>
      <text x="220" y="356" class="m" font-size="12.5" fill="#E63946" letter-spacing="2.4">//</text>
      <text x="248" y="356" class="m" font-size="12.5" fill="#8C8779" letter-spacing="2.4">BUILDING SINCE {est_year}</text>
      <text x="478" y="356" class="m" font-size="12.5" fill="#E63946" letter-spacing="2.4">//</text>
      <text x="506" y="356" class="m" font-size="12.5" fill="#8C8779" letter-spacing="2.4">SHIPPING IN PUBLIC</text>
      <text x="1204" y="56" class="m" font-size="11" fill="#8C8779" letter-spacing="2.2" text-anchor="end">SYSTEM <tspan fill="#FF4D5A">ONLINE</tspan></text>
      <circle cx="1204" cy="72" r="2.6" fill="#FF4D5A">
        <animate attributeName="opacity" values="1;0.15;1" dur="1.6s" repeatCount="indefinite"/>
      </circle>
    </g>
    <line x1="0" y1="0" x2="1280" y2="0" stroke="#FF4D5A" stroke-width="1.6" opacity="0">
      <animate attributeName="y1" values="-4;422" dur="6.5s" repeatCount="indefinite"/>
      <animate attributeName="y2" values="-4;422" dur="6.5s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values="0;0.40;0.40;0" keyTimes="0;0.06;0.9;1" dur="6.5s" repeatCount="indefinite"/>
    </line>
    <g stroke="#F2EAD9" stroke-opacity="0.22" stroke-width="1.4">
      <path d="M24 46v-22h22"/><path d="M1256 374v22h-22"/>
    </g>
    <rect x="0.75" y="0.75" width="1278.5" height="418.5" rx="17.5" stroke="#F2EAD9" stroke-opacity="0.1" stroke-width="1.5"/>
  </g>''')


def divider() -> None:
    write("divider.svg", 1280, 46, f'''
  <line x1="0" y1="23" x2="560" y2="23" stroke="{INK}" stroke-opacity="0.12"/>
  <line x1="720" y1="23" x2="1280" y2="23" stroke="{INK}" stroke-opacity="0.12"/>
  <line x1="0" y1="23" x2="1280" y2="23" stroke="{RED}" stroke-opacity="0.13" stroke-width="1.4"/>
  <line x1="0" y1="23" x2="1280" y2="23" stroke="{RED}" stroke-width="2" stroke-linecap="round"
        pathLength="100" stroke-dasharray="11 89" stroke-dashoffset="100">
    <animate attributeName="stroke-dashoffset" values="100;0" dur="7s" repeatCount="indefinite"/>
  </line>
  <line x1="0" y1="23" x2="1280" y2="23" stroke="{RED_HI}" stroke-width="1.3" stroke-linecap="round" opacity="0.75"
        pathLength="100" stroke-dasharray="3 97" stroke-dashoffset="100">
    <animate attributeName="stroke-dashoffset" values="100;0" dur="7s" begin="-3.2s" repeatCount="indefinite"/>
  </line>
  <path d="M640 8 L655 23 L640 38 L625 23 Z" fill="{RED}">
    <animate attributeName="fill-opacity" values="1;0.45;1" dur="3.4s" repeatCount="indefinite"/>
  </path>
  <path d="M640 14 L649 23 L640 32 L631 23 Z" fill="{BG}"/>''')


def typing() -> None:
    size, out_x, out_w = 15.5, 0, 0
    cmd = "sam-ronin --about"
    out = "local-first tools · zero telemetry · persian-first UI"
    adv = size * 0.6
    x0 = 74
    cmd_w = len(cmd) * adv
    out_x = x0 + cmd_w + 26
    out_w = len(out) * adv
    t0, t1 = 0.35, 1.5
    write("typing.svg", 1280, 92, f'''
  <rect width="1280" height="92" rx="14" fill="{PANEL}"/>
  <rect x="0.75" y="0.75" width="1278.5" height="90.5" rx="13.5" stroke="{INK}" stroke-opacity="0.09" stroke-width="1.5"/>
  <circle cx="30" cy="30" r="4.2" fill="#3A3A40"/><circle cx="47" cy="30" r="4.2" fill="#3A3A40"/>
  <circle cx="64" cy="30" r="4.2" fill="{RED}"/>
  <text x="{x0 - 34}" y="64" class="m" font-size="{size + 2}" fill="{RED_HI}">❯</text>
  <clipPath id="tc"><rect x="{x0 - 2}" y="34" width="0" height="40">
    <animate attributeName="width" values="0;{cmd_w:.1f}" begin="{t0}s" dur="{t1}s" fill="freeze"/></rect></clipPath>
  <g clip-path="url(#tc)"><text x="{x0}" y="64" class="m" font-size="{size}" fill="{INK}">{esc(cmd)}</text></g>
  <clipPath id="to"><rect x="{out_x - 2}" y="34" width="0" height="40">
    <animate attributeName="width" values="0;{out_w:.1f}" begin="{t0 + t1 + 0.25:.2f}s" dur="1.9s" fill="freeze"/></rect></clipPath>
  <g clip-path="url(#to)" opacity="0">
    <animate attributeName="opacity" values="0;1" begin="{t0 + t1 + 0.25:.2f}s" dur="0.1s" fill="freeze"/>
    <text x="{out_x}" y="64" class="m" font-size="{size}" fill="{DIM}">{esc(out)}</text>
  </g>
  <rect x="{x0 - 2}" y="38" width="1.6" height="{size + 6}" fill="{RED_HI}" opacity="0">
    <animate attributeName="x" values="{x0 - 2};{out_x + out_w + 8:.1f}" begin="{t0}s" dur="{t1 + 2.15:.2f}s" fill="freeze"/>
    <animate attributeName="opacity" values="0;1" begin="{t0}s" dur="0.2s" fill="freeze"/>
    <animate attributeName="opacity" values="1;0" begin="{t0 + t1 + 2.3:.2f}s" dur="0.25s" fill="freeze"/>
  </rect>
  <rect x="{out_x + out_w + 8:.1f}" y="38" width="1.6" height="{size + 6}" fill="{RED_HI}" opacity="0">
    <animate attributeName="opacity" values="0;0;1;0;1;0;1;0;1;0;1;0;1"
      keyTimes="0;0.24;0.28;0.36;0.4;0.48;0.52;0.6;0.64;0.72;0.76;0.84;1" dur="9s" repeatCount="indefinite"/>
  </rect>
  <line x1="922" y1="26" x2="922" y2="66" stroke="{INK}" stroke-opacity="0.08"/>
  {''.join(f"""<text x="{952 + i * 108}" y="44" class="m" font-size="9.5" fill="{MUTED}" letter-spacing="1.6">{k}</text>
  <text x="{952 + i * 108}" y="62" class="m" font-size="12.5" fill="{c}" letter-spacing="1.2">{v}</text>"""
            for i, (k, v, c) in enumerate([("STATUS", "PUBLIC", RED_HI), ("REFRESH", "6H", DIM), ("TELEMETRY", "ZERO", DIM)]))}''')


def stats_asset(stats: list[tuple[str, str, str]]) -> None:
    w, h, cw = 1280, 210, 320
    cells = []
    for i, (num, label, sub) in enumerate(stats):
        cx = cw * i + cw / 2
        size = 62 if len(num) <= 3 else 46
        cells.append(f'''
  {f'<line x1="{cw * i}" y1="46" x2="{cw * i}" y2="{h - 46}" stroke="{INK}" stroke-opacity="0.08"/>' if i else ''}
  <g opacity="0"><animate attributeName="opacity" values="0;1" begin="{0.15 + i * 0.14:.2f}s" dur="0.5s" fill="freeze"/>
    <text x="{cx:.1f}" y="104" class="s" font-size="{size}" font-weight="800" fill="{INK}" text-anchor="middle"
          letter-spacing="-1">{esc(num)}</text>
    <text x="{cx:.1f}" y="140" class="m" font-size="12" fill="{RED_HI}" letter-spacing="3.4" text-anchor="middle">{esc(label)}</text>
    <text x="{cx:.1f}" y="166" class="m" font-size="11.5" fill="{MUTED}" text-anchor="middle">{esc(sub)}</text>
  </g>''')
    write("stats.svg", w, h, f'''
  <rect width="{w}" height="{h}" rx="16" fill="{PANEL}"/>
  <rect x="0.75" y="0.75" width="{w - 1.5}" height="{h - 1.5}" rx="15.5" stroke="{INK}" stroke-opacity="0.09" stroke-width="1.5"/>
  <line x1="0" y1="1" x2="{w}" y2="1" stroke="{RED}" stroke-opacity="0.16" stroke-width="2"/>
  <line x1="0" y1="1" x2="{w}" y2="1" stroke="{RED_HI}" stroke-width="2.4" stroke-linecap="round"
        pathLength="100" stroke-dasharray="13 87" stroke-dashoffset="100">
    <animate attributeName="stroke-dashoffset" values="100;0" dur="9s" repeatCount="indefinite"/>
  </line>
  {''.join(cells)}
  <circle cx="{w - 26}" cy="24" r="3" fill="{RED_HI}">
    <animate attributeName="opacity" values="1;0.15;1" dur="1.8s" repeatCount="indefinite"/>
  </circle>''')


def stack_asset(groups: list[tuple[str, list[str]]]) -> None:
    w, pill_h, gap, line_gap, pad = 1280, 32, 10, 48, 26
    rows, y = [], 0
    for label, items in groups:
        x, row = 0, []
        for it in items:
            pw = mw(it, 13) + 26
            row.append((x, x + pw, it))
            x += pw + gap
        rows.append((label, row))
        y += pill_h + line_gap
    h = y + 30
    body = []
    yy = 40
    for gi, (label, row) in enumerate(rows):
        body.append(f'<text x="{pad}" y="{yy}" class="m" font-size="11" fill="{RED}" letter-spacing="3">{esc(label)}</text>')
        body.append(f'<line x1="{pad + mw(label, 11, 3) + 14:.0f}" y1="{yy - 4}" x2="{w - pad}" y2="{yy - 4}" stroke="{INK}" stroke-opacity="0.07"/>')
        for pi, (x0, x1, it) in enumerate(row):
            px, py = pad + x0, yy + 16
            d = 0.25 + gi * 0.18 + pi * 0.05
            body.append(f'''<g opacity="0"><animate attributeName="opacity" values="0;1" begin="{d:.2f}s" dur="0.45s" fill="freeze"/>
    <rect x="{px}" y="{py}" width="{x1 - x0:.0f}" height="{pill_h}" rx="8" fill="{INK}" fill-opacity="0.035" stroke="{INK}" stroke-opacity="0.10"/>
    <rect x="{px}" y="{py + 8}" width="3" height="{pill_h - 16}" rx="1.5" fill="{RED}" fill-opacity="0.85"/>
    <text x="{px + 15}" y="{py + 22}" class="m" font-size="13" fill="{DIM}">{esc(it)}</text></g>''')
        yy += pill_h + line_gap
    write("stack.svg", w, h, f'''
  <rect width="{w}" height="{h}" rx="16" fill="{PANEL}"/>
  <rect x="0.75" y="0.75" width="{w - 1.5}" height="{h - 1.5}" rx="15.5" stroke="{INK}" stroke-opacity="0.09" stroke-width="1.5"/>
  {''.join(body)}''')


def activity_asset(weeks: list[dict], total: int) -> None:
    cell, gapc = 13.5, 4.0
    step = cell + gapc
    left, top, w = 64, 74, 1280
    h = top + 7 * step + 74

    def color(c: int) -> str:
        return "#16161A" if c == 0 else "#521216" if c <= 4 else "#8E1B22" if c <= 9 else "#C41E2A" if c <= 19 else "#FF4D5A"

    body = []
    last_month = None
    for wi, wk in enumerate(weeks):
        days = wk.get("contributionDays") or []
        if not days:
            continue
        d0 = dt.date.fromisoformat(str(days[0]["date"])[:10])
        if d0.month != last_month:
            last_month = d0.month
            if wi < len(weeks) - 1:
                body.append(f'<text x="{left + wi * step:.0f}" y="{top - 12}" class="m" font-size="9.5" fill="{MUTED}" letter-spacing="1.4">{d0.strftime("%b").upper()}</text>')
    for i, lab in enumerate(["MON", "", "WED", "", "FRI", "", "SUN"]):
        if lab:
            body.append(f'<text x="{left - 14}" y="{top + i * step + cell - 2.5:.0f}" class="m" font-size="9.5" fill="{MUTED}" text-anchor="end">{lab}</text>')
    for wi, wk in enumerate(weeks):
        for di, day in enumerate(wk.get("contributionDays") or []):
            c = day["contributionCount"]
            body.append(f'<rect x="{left + wi * step:.1f}" y="{top + di * step:.1f}" width="{cell}" height="{cell}" rx="2.6" '
                        f'fill="{color(c)}" opacity="0"><animate attributeName="opacity" values="0;1" '
                        f'begin="{0.12 + wi * 0.021:.2f}s" dur="0.35s" fill="freeze"/>'
                        f'<title>{day["date"]}: {c}</title></rect>')
    hw = len(weeks) * step
    quiet_cx = left + hw * 0.30
    cap = "EARLY DAYS · SHIPPING IN PUBLIC"
    capw = mw(cap, 10.5, 5.0) + 46
    capy = top + 2.55 * step
    body.append(f'<rect x="{quiet_cx - capw / 2:.0f}" y="{capy:.0f}" width="{capw:.0f}" height="30" rx="15" fill="{PANEL}" stroke="{INK}" stroke-opacity="0.07"/>')
    body.append(f'<text x="{quiet_cx:.0f}" y="{capy + 19.5:.0f}" class="m" font-size="10.5" fill="{INK}" fill-opacity="0.28" text-anchor="middle" letter-spacing="5">{cap}</text>')
    if weeks:
        nx = left + (len(weeks) - 1) * step + cell / 2
        body.append(f'<circle cx="{nx:.1f}" cy="{top + 7 * step + 12}" r="3" fill="{RED_HI}"><animate attributeName="opacity" values="1;0.2;1" dur="1.7s" repeatCount="indefinite"/></circle>')
        body.append(f'<text x="{nx:.1f}" y="{top + 7 * step + 32}" class="m" font-size="9.5" fill="{RED_HI}" text-anchor="middle" letter-spacing="2">NOW</text>')

    rx = left + hw + 62
    body.append(f'<line x1="{rx - 34:.0f}" y1="52" x2="{rx - 34:.0f}" y2="{h - 40}" stroke="{INK}" stroke-opacity="0.08"/>')
    body.append(f'<text x="{rx}" y="{top + 26}" class="s" font-size="54" font-weight="800" fill="{INK}" letter-spacing="-1.5">{total}</text>')
    body.append(f'<text x="{rx}" y="{top + 50}" class="m" font-size="10.5" fill="{MUTED}" letter-spacing="1.2">TOTAL CONTRIBUTIONS</text>')
    bars = [sum(d["contributionCount"] for d in w_["contributionDays"]) for w_ in weeks][-10:]
    by, bw, maxb = top + 78, 13, max(bars, default=0) or 1
    for i, v in enumerate(bars):
        bh = max(3, v / maxb * 54)
        body.append(f'<rect x="{rx + i * (bw + 6)}" y="{by + 54:.0f}" width="{bw}" height="0" rx="2" fill="{RED if i < len(bars) - 1 else RED_HI}" opacity="{0.35 + 0.06 * i:.2f}">'
                    f'<animate attributeName="height" values="0;{bh:.0f}" begin="{1.0 + i * 0.08:.2f}s" dur="0.5s" fill="freeze"/>'
                    f'<animate attributeName="y" values="{by + 54:.0f};{by + 54 - bh:.0f}" begin="{1.0 + i * 0.08:.2f}s" dur="0.5s" fill="freeze"/></rect>')
    body.append(f'<text x="{rx}" y="{by + 74}" class="m" font-size="9.5" fill="{MUTED}" letter-spacing="1.4">LAST 10 WEEKS</text>')

    write("activity.svg", w, h, f'''
  <rect width="{w}" height="{h}" rx="16" fill="{PANEL}"/>
  <rect x="0.75" y="0.75" width="{w - 1.5}" height="{h - 1.5}" rx="15.5" stroke="{INK}" stroke-opacity="0.09" stroke-width="1.5"/>
  <text x="{left}" y="42" class="m" font-size="11" fill="{RED}" letter-spacing="3">ACTIVITY</text>
  <text x="{left + 92}" y="42" class="m" font-size="11" fill="{MUTED}" letter-spacing="1">// last 52 weeks · {total} contributions</text>
  {''.join(body)}''')


def card_asset(idx: int, r: dict) -> None:
    w, h = 624, 238
    name = display_name(r)
    if len(name) > 32:                       # a 100-char repo name must not overflow
        name = name[:31].rstrip() + "…"
    nsize = 25 if len(name) <= 20 else 20
    kicker = kicker_for(r)
    desc = clean_text(r.get("description") or "") or "Open-source project — details in the repository."
    tags = tags_for(r)
    lang, pct, lcolor = top_lang(r)
    lines = wrap(desc, w - 56, 13.6, 3)
    pills, x = [], 28
    for t in tags:
        pw = mw(t, 11, 1.1) + 22
        pills.append(f'<rect x="{x:.0f}" y="{h - 62}" width="{pw:.0f}" height="24" rx="6" fill="{RED}" fill-opacity="0.10" stroke="{RED}" stroke-opacity="0.30"/>'
                     f'<text x="{x + pw / 2:.0f}" y="{h - 45.5}" class="m" font-size="11" fill="#FF7A84" text-anchor="middle" letter-spacing="1.1">{esc(t)}</text>')
        x += pw + 7
    defs = (f'<linearGradient id="g{idx}" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="#251114"/><stop offset="0.5" stop-color="#131217"/>'
            f'<stop offset="1" stop-color="#101014"/></linearGradient>'
            f'<pattern id="p{idx}" width="26" height="26" patternUnits="userSpaceOnUse">'
            f'<path d="M26 0H0V26" stroke="#F2EAD9" stroke-opacity="0.028"/></pattern>')
    write(f"card_{r['_slug']}.svg", w, h, f'''
  <rect width="{w}" height="{h}" rx="16" fill="{PANEL}"/>
  <rect width="{w}" height="{h}" rx="16" fill="url(#p{idx})"/>
  <rect width="{w}" height="{h}" rx="16" fill="url(#g{idx})"/>
  <rect x="0.75" y="0.75" width="{w - 1.5}" height="{h - 1.5}" rx="15.5" stroke="{INK}" stroke-opacity="0.10" stroke-width="1.5"/>
  <line x1="16" y1="0.9" x2="{w - 16}" y2="0.9" stroke="{RED}" stroke-opacity="0.18" stroke-width="2"/>
  <line x1="16" y1="0.9" x2="{w - 16}" y2="0.9" stroke="{RED_HI}" stroke-width="2.4" stroke-linecap="round"
        pathLength="100" stroke-dasharray="22 78" stroke-dashoffset="100">
    <animate attributeName="stroke-dashoffset" values="100;0" dur="5s" begin="-{idx * 1.1:.1f}s" repeatCount="indefinite"/>
  </line>
  <text x="{w - 24}" y="66" class="s" font-size="46" font-weight="800" fill="none" stroke="{INK}" stroke-opacity="0.13"
        text-anchor="end" letter-spacing="-1">{idx:02d}</text>
  <g opacity="0"><animate attributeName="opacity" values="0;1" begin="0.12s" dur="0.5s" fill="freeze"/>
    <text x="28" y="52" class="m" font-size="{nsize}" font-weight="700" fill="{INK}">{esc(name)}</text></g>
  <text x="28" y="96" class="m" font-size="12.5" fill="{RED_HI}" letter-spacing="2.6">❯ {esc(kicker)}</text>
  {''.join(f'<text x="28" y="{125 + i * 20}" class="m" font-size="13.6" fill="{DIM}">{esc(ln)}</text>' for i, ln in enumerate(lines))}
  {''.join(pills)}
  <circle cx="32" cy="{h - 20}" r="4" fill="{lcolor}"/>
  <text x="44" y="{h - 16}" class="m" font-size="11.5" fill="{MUTED}">{esc(lang)} {pct}%</text>
  <text x="{w - 28}" y="{h - 16}" class="m" font-size="11" fill="{RED_HI}" text-anchor="end" letter-spacing="1.6">OPEN SOURCE ↗</text>''', defs)


def footer_asset() -> None:
    year = dt.date.today().year   # never let the copyright year go stale
    write("footer.svg", 1280, 86, f'''
  <line x1="0" y1="0.8" x2="1280" y2="0.8" stroke="{INK}" stroke-opacity="0.10"/>
  <line x1="0" y1="0.8" x2="487" y2="0.8" stroke="{RED}" stroke-width="1.6" opacity="0.7"/>
  <line x1="0" y1="0.8" x2="1280" y2="0.8" stroke="{RED_HI}" stroke-width="1.6" stroke-linecap="round" opacity="0.85"
        pathLength="100" stroke-dasharray="9 91" stroke-dashoffset="100">
    <animate attributeName="stroke-dashoffset" values="100;0" dur="11s" repeatCount="indefinite"/>
  </line>
  <text x="0" y="46" class="m" font-size="11.5" fill="{MUTED}" letter-spacing="1.6">© {year} SAM RONIN</text>
  <text x="336" y="46" class="m" font-size="11.5" fill="{DIM}" letter-spacing="1.7">BUILT WITH INTENT</text>
  <text x="484" y="46" class="m" font-size="11.5" fill="{RED}" letter-spacing="1.7">·</text>
  <text x="500" y="46" class="m" font-size="11.5" fill="{DIM}" letter-spacing="1.7">SHIPPED IN PUBLIC</text>
  <text x="660" y="46" class="m" font-size="11.5" fill="{RED}" letter-spacing="1.7">·</text>
  <text x="676" y="46" class="m" font-size="11.5" fill="{DIM}" letter-spacing="1.7">KEPT LOCAL</text>
  <text x="1280" y="46" class="m" font-size="11.5" fill="{MUTED}" letter-spacing="1.6" text-anchor="end">TEHRAN, IRAN</text>
  <path d="M1160 38 h9 v9 z" fill="{RED}" opacity="0.9"/>
  <circle cx="640" cy="64" r="2.2" fill="{RED}">
    <animate attributeName="opacity" values="1;0.2;1" dur="2.6s" repeatCount="indefinite"/>
  </circle>''')


# ─────────────────────────────────────────── README
def build_readme(ctx: dict) -> str:
    u, cards, extra = ctx["user"], ctx["cards"], ctx["extra"]
    login = u["login"]

    card_cells = []
    for row in range(0, len(cards), 2):
        pair = cards[row:row + 2]
        tds = "".join(
            f'<td width="50%" valign="top"><a href="https://github.com/{login}/{c["name"]}">'
            f'<img src="assets/card_{c["_slug"]}.svg" alt="{esc(c["name"])}" width="100%" /></a></td>'
            for c in pair)
        if len(pair) == 1:
            tds += '<td width="50%" valign="top"></td>'
        card_cells.append(f"  <tr>\n    {tds}\n  </tr>")
    featured = ("<table>\n" + "\n".join(card_cells) + "\n</table>") if cards else "_Coming soon._"

    also_rows = "\n".join(
        f'| **[{esc(display_name(r))}](https://github.com/{login}/{r["name"]})** | '
        f'{md_cell(r.get("description") or "")} | `{esc(r.get("language") or "—")}` |'
        for r in extra[:12])
    also = (f"""
<details>
<summary><b>Everything else I'm shipping ({len(extra)})</b></summary>

| Repo | What it is | Lang |
|---|---|---|
{also_rows}

</details>
""") if extra else ""

    cur = []
    for r in cards[:3]:
        cur.append(f'  ▸ shipping   {r["name"]:<14} {clip(r.get("description") or "", 44)}')
    cur.append(f'  ▸ learning   Rust          ownership, memory model, zero-cost abstractions')
    cur.append(f'  ▸ open to    collabs       local-first tools, RTL/Persian UX, MV3 extensions')

    return f'''<div align="center">
  <img src="assets/banner.svg" alt="Sam Ronin — Code Ronin" width="100%" />
</div>

<div align="center">
  <img src="assets/typing.svg" alt="terminal" width="100%" />
</div>

<div align="center">
  <br />

  [![Local-first](https://img.shields.io/badge/LOCAL--FIRST-100%25-E63946?style=flat-square&labelColor=101014)](#)
  [![Telemetry](https://img.shields.io/badge/TELEMETRY-ZERO-E63946?style=flat-square&labelColor=101014)](#)
  [![Repos](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fapi.github.com%2Fusers%2F{login}&query=%24.public_repos&label=PUBLIC%20REPOS&style=flat-square&labelColor=101014&color=F2EAD9)](#)
  [![Location](https://img.shields.io/badge/TEHRAN-IRAN-F2EAD9?style=flat-square&labelColor=101014)](#)
  [![Followers](https://img.shields.io/github/followers/{login}?style=flat-square&labelColor=101014&color=F2EAD9&logo=github&logoColor=F2EAD9)](https://github.com/{login}?tab=followers)
  [![Last commit](https://img.shields.io/github/last-commit/{login}/{login}?style=flat-square&labelColor=101014&color=E63946)](https://github.com/{login}/{login}/commits/main)

</div>

<img src="assets/divider.svg" alt="" width="100%" />

## <img src="https://api.iconify.design/lucide/swords.svg?color=%23E63946" width="22" valign="middle" /> &nbsp;RONIN &nbsp;·&nbsp; The short version

I build **local-first, privacy-first tools** and ship them in public. No telemetry, no accounts, no cloud round-trips — the file stays on your machine, the data stays in your browser. If a feature needs the network, it earns that need first.

Most of my work lives where **the browser** meets **native performance**: Chrome extensions on Manifest V3, and desktop apps on **Tauri v2 + Rust** where FFmpeg-class work has to feel instant. Every UI I ship is **Persian-first and RTL-correct** — not translated afterwards, but designed that way from the first line of code.

<div dir="rtl">
  <p>
    <b>فارسی:</b> من ابزارهایی می‌سازم که <b>اول محلی</b> و <b>اول خصوصی</b> هستند — بدون تلمتری، بدون حساب کاربری، بدون آپلود فایل روی سرور. تمرکزم روی اکستنشن‌های کروم (Manifest V3) و اپلیکیشن‌های دسکتاپ با Tauri v2 و Rust است. رابط کاربری فارسی و راست‌به‌چپ برایم یک اصل است، نه یک قابلیت جانبی. هر چیزی که می‌سازم را علنی و قدم‌به‌قدم منتشر می‌کنم.
  </p>
</div>

<img src="assets/stats.svg" alt="stats" width="100%" />

<img src="assets/divider.svg" alt="" width="100%" />

## <img src="https://api.iconify.design/lucide/layers.svg?color=%23E63946" width="22" valign="middle" /> &nbsp;// Arsenal

<img src="assets/stack.svg" alt="tech stack" width="100%" />

<img src="assets/divider.svg" alt="" width="100%" />

## <img src="https://api.iconify.design/lucide/package.svg?color=%23E63946" width="22" valign="middle" /> &nbsp;// Featured work

<!-- AUTO:FEATURED — regenerated from the GitHub API by .github/workflows/update-profile.yml -->
{featured}
{also}
<img src="assets/divider.svg" alt="" width="100%" />

## <img src="https://api.iconify.design/lucide/activity.svg?color=%23E63946" width="22" valign="middle" /> &nbsp;// The record

<img src="assets/activity.svg" alt="contribution activity" width="100%" />

<img src="assets/divider.svg" alt="" width="100%" />

## <img src="https://api.iconify.design/lucide/compass.svg?color=%23E63946" width="22" valign="middle" /> &nbsp;// Code of the road

<table>
  <tr>
    <td width="33%" valign="top">
      <h3>🛡️ Privacy is architecture</h3>
      <p align="left">Not a checkbox, not a policy page. If the data never leaves the device, there is nothing to leak, sell, or subpoena. <code>0 bytes</code> of telemetry is a feature I can prove.</p>
    </td>
    <td width="33%" valign="top">
      <h3>⚡ Native or nothing</h3>
      <p align="left">Long jobs belong in Rust and FFmpeg, not in a spinning CSS loader. Fast is a design decision made before the first benchmark.</p>
    </td>
    <td width="33%" valign="top">
      <h3>🌐 Persian-first, not Persian-later</h3>
      <p align="left">RTL, Vazirmatn, correct number shaping, honest translations. 90 million people deserve software built for them from pixel one.</p>
    </td>
  </tr>
</table>

<img src="assets/divider.svg" alt="" width="100%" />

## <img src="https://api.iconify.design/lucide/terminal.svg?color=%23E63946" width="22" valign="middle" /> &nbsp;// Currently

```console
sam@ronin:~$ status

{chr(10).join(cur)}
```

<img src="assets/divider.svg" alt="" width="100%" />

## <img src="https://api.iconify.design/lucide/send.svg?color=%23E63946" width="22" valign="middle" /> &nbsp;// Reach out

<div align="center">
  <p>Got a local-first idea, an RTL interface that deserves better, or an extension that should exist? Open an issue on any repo — that's my inbox.</p>

  <a href="https://github.com/{login}?tab=repositories">
    <img src="https://img.shields.io/badge/REPOSITORIES-{login}-E63946?style=for-the-badge&labelColor=101014&logo=github&logoColor=F2EAD9" height="34" />
  </a>
  &nbsp;
  <a href="https://github.com/{login}?tab=stars">
    <img src="https://img.shields.io/badge/STARRED-REPOS-E63946?style=for-the-badge&labelColor=101014&logo=github&logoColor=F2EAD9" height="34" />
  </a>
  &nbsp;
  <a href="https://github.com/{login}?tab=followers">
    <img src="https://img.shields.io/badge/FOLLOW-THE%20RONIN-E63946?style=for-the-badge&labelColor=101014&logo=github&logoColor=F2EAD9" height="34" />
  </a>
</div>

<br />

<div align="center">
  <img src="assets/footer.svg" alt="" width="100%" />
  <br /><br />
  <samp>RONIN &nbsp;·&nbsp; build things &nbsp;·&nbsp; keep them local</samp>
</div>
'''


def build_summary(ctx: dict) -> str:
    u, cards = ctx["user"], ctx["cards"]
    langs = ctx["stack"][0][1]

    def one(s: Any) -> str:
        # single line, no stray markup — keeps the markdown valid
        return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", str(s or "—"))).strip() or "—"

    lines = [
        f"# {one(u.get('name') or u['login'])} — profile snapshot",
        "",
        f"> Auto-generated from public GitHub data on every scheduled build.",
        "",
        f"- **Handle:** [{u['login']}](https://github.com/{u['login']})",
        f"- **Bio:** {one(u.get('bio'))}",
        f"- **Location:** {one(u.get('location'))}",
        f"- **Public repos:** {int(u.get('public_repos') or len(ctx['repos']))}",
        f"- **Contributions (52w):** {ctx['total_contribs']}",
        f"- **Core languages:** {', '.join(langs)}",
        "",
        "## What he builds",
        "",
        "Local-first, privacy-first tools: browser extensions (Chrome MV3) and desktop apps "
        "(Tauri v2 + Rust), with Persian-first / RTL interfaces. No telemetry, no accounts, "
        "no server uploads.",
        "",
        "## Recent work",
        "",
    ]
    for r in cards:
        lines.append(f"- **[{r['name']}](https://github.com/{u['login']}/{r['name']})** — "
                     f"{one(r.get('description'))}")
    if ctx["extra"]:
        lines += ["", "## Other public repos", ""]
        for r in ctx["extra"][:12]:
            lines.append(f"- `{r['name']}` — {one(r.get('description'))[:90]}")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────── main
def assign_slugs(repos: list[dict]) -> None:
    """Filesystem-safe, UNIQUE card slugs (a collision would overwrite a card)."""
    used: set[str] = set()
    for r in repos:
        slug = re.sub(r"[^a-z0-9]+", "_", r["name"].lower()).strip("_")[:32] or "repo"
        base, n = slug, 2
        while slug in used:
            slug = f"{base[:29]}_{n}"
            n += 1
        used.add(slug)
        r["_slug"] = slug


def check_leaks(readme: str, summary: str, private_names: list[str]) -> list[str]:
    """Private repo names must never reach the public output (case-insensitive)."""
    haystack = (readme + "\n" + summary).casefold()
    return [n for n in private_names if n and len(n) > 2 and n.casefold() in haystack]


def validate_assets() -> None:
    """Every published SVG must be well-formed XML — fail loudly, ship nothing broken."""
    for f in sorted(os.listdir(ASSETS)):
        if f.endswith(".svg"):
            ET.parse(os.path.join(ASSETS, f))


def handle_fetch_error(e: Exception) -> tuple[int, str]:
    """Map a fetch failure to (exit_code, CI message).

    Auth and rate-limit problems leave the existing profile untouched (exit 0
    with a visible warning — a scheduled run must never wipe the profile),
    while config errors, unknown users and network failures fail the run
    loudly so CI never reports a false success.
    """
    if isinstance(e, TokenMissing):
        return 0, ("::warning::no GitHub token — profile left unchanged. "
                   "Add a read-only PROFILE_TOKEN secret to enable updates.")
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return 1, "::error::GitHub user not found — check PROFILE_LOGIN / --login."
        if e.code == 401:
            return 0, ("::warning::GitHub API rejected the token (401) — profile left unchanged. "
                       "Replace the PROFILE_TOKEN secret with a valid read-only token.")
        if e.code == 403 and not _rate_limited(e):
            return 0, ("::warning::GitHub API rejected the token (403) — profile left unchanged. "
                       "Replace the PROFILE_TOKEN secret with a valid read-only token.")
        if e.code in (403, 429):
            return 0, ("::warning::GitHub API rate limit exhausted — profile left unchanged. "
                       "The next scheduled run will retry.")
        return 1, f"::error::GitHub API HTTP {e.code} — failing the run."
    if isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return 1, f"::error::network failure talking to GitHub: {e}"
    return 1, f"::error::{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", default=os.environ.get("PROFILE_LOGIN", "SamRonin"))
    args = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    try:
        data = fetch(args.login, token)
    except (TokenMissing, urllib.error.HTTPError, urllib.error.URLError,
            TimeoutError, ConnectionError) as e:
        code, msg = handle_fetch_error(e)
        print(msg)
        return code

    try:
        est_year = dt.date.fromisoformat(str(data["user"]["created_at"])[:10]).year
    except Exception:
        est_year = dt.date.today().year

    repos = rank_repos(data["repos"])
    assign_slugs(repos)

    ordered = sorted(repos, key=rank_key)
    cards = [r for r in ordered if is_featurable(r)][:4] or ordered[:4]
    used = {r["name"] for r in cards}

    ctx = {
        "user": data["user"], "repos": repos, "cards": cards,
        "extra": [r for r in ordered if r["name"] not in used],
        "stats": build_stats(data, repos), "stack": build_stack(repos),
        "weeks": data["weeks"], "total_contribs": data["total_contribs"],
        "private_names": data["private_names"],
    }

    # assets
    banner(est_year); divider(); typing(); footer_asset()
    stats_asset(ctx["stats"]); stack_asset(ctx["stack"])
    activity_asset(data["weeks"], data["total_contribs"])
    for i, r in enumerate(cards, start=1):
        card_asset(i, r)
    # stale card assets from repos that dropped out of the top 4
    keep = {f"card_{r['_slug']}.svg" for r in cards}
    for f in os.listdir(ASSETS):
        if f.startswith("card_") and f.endswith(".svg") and f not in keep:
            os.remove(os.path.join(ASSETS, f))

    # README + machine-readable summary
    readme = build_readme(ctx)
    summary = build_summary(ctx)
    with open(os.path.join(ROOT, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    with open(os.path.join(ROOT, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary)

    # every published SVG must be well-formed XML — fail the run, not the profile
    validate_assets()

    # ── guard: nothing private may ever reach the output
    leaks = check_leaks(readme, summary, data["private_names"])
    if leaks:
        print(f"::error::private repository name reached the output: {leaks}", file=sys.stderr)
        return 1

    print(f"ok · {len(repos)} public repos · {len(cards)} cards · "
          f"{data['total_contribs']} contributions · {len(ctx['extra'])} in 'also shipping'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
