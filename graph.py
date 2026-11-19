#!/usr/bin/env python3
"""gap-miner as a LangGraph state graph.

One product per run: pick -> fetch -> extract -> append -> update -> END.
State and findings persist in DATA_DIR (a k8s PVC in prod, ./data locally).

LLM: any OpenAI-compatible endpoint. Default target is the rile.cc Qwen
pool behind the Caddy gateway (bearer in BEARER_TOKEN). Thinking tokens
are disabled server-side (chat_template_kwargs.enable_thinking=false):
reasoner models otherwise spend their max_tokens budget on reasoning and
return empty content — the same defect found and fixed in the mem-wrapper
extraction loop (4/4 substantive ingests after the fix vs 2/4 before).
"""
import json
import os
import pathlib
import re
import sys
import datetime
import html as html_mod

import httpx
from typing import TypedDict
from langgraph.graph import StateGraph, END

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "data"))
LLM_CHAT_URL = os.environ.get("LLM_CHAT_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen38")
BEARER_TOKEN = os.environ.get("BEARER_TOKEN", "")
STATE_PATH = DATA_DIR / "state.json"
FINDINGS_PATH = DATA_DIR / "findings.md"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"}
MAX_SRC_CHARS = 15000
MAX_ATTEMPTS = 2


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- state I/O
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception as exc:  # corrupt -> recreate, log the reset
            print(f"WARN state corrupt ({exc}); recreating", flush=True)
    backlog = json.loads((pathlib.Path(__file__).resolve().parent / "backlog.json").read_text())
    state = {
        "phase": "running",
        "niche": backlog.get("niche", ""),
        "backlog": [
            {"id": it["id"], "name": it["name"], "itunes_term": it["itunes_term"],
             "queries": it["queries"], "status": "pending", "notes": []}
            for it in backlog["items"]
        ],
        "findings_file": str(FINDINGS_PATH),
        "last_error": None,
        "heartbeat_at": None,
        "history": [],
    }
    STATE_PATH.write_text(json.dumps(state, indent=2))
    return state


def save_state(state: dict) -> None:
    state["heartbeat_at"] = now_iso()
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------- fetchers
def itunes_app_id(term: str) -> tuple[str, str] | None:
    """Resolve a search term to (app_id, app_name) via iTunes Search API."""
    try:
        r = httpx.get("https://itunes.apple.com/search", params={
            "term": term, "entity": "software", "limit": 3}, timeout=20, headers=UA)
        rows = r.json().get("results", [])
        if not rows:
            return None
        first = rows[0]
        return str(first["trackId"]), first["trackName"]
    except Exception:
        return None


def fetch_reviews(app_id: str, pages: int = 2) -> list[dict]:
    """Most-recent customer reviews from the iTunes RSS feed."""
    out = []
    for page in range(1, pages + 1):
        try:
            url = f"https://itunes.apple.com/us/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
            data = httpx.get(url, timeout=20, headers=UA).json()
            entries = data.get("feed", {}).get("entry", [])
            if isinstance(entries, dict):  # single-entry feed quirk
                entries = [entries]
            for e in entries:
                body = e.get("content", {}).get("label", "")
                out.append({
                    "rating": e.get("im:rating", {}).get("label", "?"),
                    "title": e.get("title", {}).get("label", ""),
                    "body": body,
                })
        except Exception:
            continue
    return out


def ddg_snippets(query: str) -> list[str]:
    """DuckDuckGo lite text snippets (the only search path that reliably
    works from this network; Reddit/Trustpilot are bot-walled)."""
    try:
        r = httpx.get("https://lite.duckduckgo.com/lite/", params={"q": query},
                      timeout=20, headers=UA, follow_redirects=True)
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = html_mod.unescape(re.sub(r"\s+", " ", text)).strip()
        return [text]
    except Exception:
        return []


# ---------------------------------------------------------------- graph state
class TState(TypedDict, total=False):
    product: dict          # backlog item being mined
    sources_text: str
    blocked: int
    themes: list | None
    wedge_notes: list | None
    attempts: int
    error: str | None
    skipped: bool


# ---------------------------------------------------------------- nodes
def pick_product(state: TState) -> TState:
    st = load_state()
    for item in st["backlog"]:
        if item["status"] == "pending":
            print(f"pick: {item['id']}", flush=True)
            return {"product": item, "blocked": 0, "attempts": 0,
                    "error": None, "skipped": False}
    print("pick: nothing pending — LOOP COMPLETE", flush=True)
    return {"skipped": True}


def fetch_reviews_node(state: TState) -> TState:
    item = state["product"]
    blocks, parts = 0, []
    resolved = itunes_app_id(item["itunes_term"])
    if resolved:
        app_id, app_name = resolved
        reviews = fetch_reviews(app_id)
        if reviews:
            parts.append(f"## iTunes customer reviews for {app_name} (id {app_id}):")
            parts.append("\n".join(f"[{r['rating']}] {r['title']}: {r['body']}" for r in reviews[:60]))
        else:
            blocks += 1
    else:
        blocks += 1
    for q in item["queries"]:
        snips = ddg_snippets(q)
        if snips:
            parts.append(f"## Web search results for '{q}':\n{snips[0][:4000]}")
        else:
            blocks += 1
    text = "\n\n".join(parts)[:MAX_SRC_CHARS]
    print(f"fetch: {len(parts)} sources, {blocks} blocked, {len(text)} chars", flush=True)
    return {"sources_text": text, "blocked": blocks}


def extract_themes(state: TState) -> TState:
    item, src = state["product"], state["sources_text"]
    system = (
        "You are a review miner. Respond with ONLY one single-line JSON object, "
        'no markdown, no preamble: {"themes": [{"theme": "3-6 words", "kind": '
        '"complaint|praise", "share": "approx % of reviews", "quote": "short '
        'verbatim quote from the sources"}], "wedge_notes": ["1-3 short lines: '
        'which complaints are structural vs shallow; what a self-hosted '
        'alternative could do differently"]}. Use 5-9 themes. Never invent '
        "quotes; use only the provided text; if the text has no usable reviews, "
        'return {"themes": [], "wedge_notes": ["sources-blocked"]}.'
    )
    payload = {
        "model": LLM_MODEL,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": f"Product: {item['name']}\n\n{src}"}],
        "max_tokens": 2000,
        "temperature": 0.2,
    }
    try:
        r = httpx.post(LLM_CHAT_URL, json=payload, timeout=240,
                       headers={"Authorization": f"Bearer {BEARER_TOKEN}",
                                "Content-Type": "application/json"})
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
        blob = raw[raw.index("{"): raw.rindex("}") + 1] if "{" in raw and "}" in raw else ""
        parsed = json.loads(blob)
        themes = parsed.get("themes") or []
        wedge = parsed.get("wedge_notes") or []
        if not themes:
            raise ValueError("empty themes")
        print(f"extract: {len(themes)} themes ok", flush=True)
        return {"themes": themes, "wedge_notes": wedge, "attempts": state.get("attempts", 0), "error": None}
    except Exception as exc:
        print(f"extract: attempt {state.get('attempts', 0) + 1} failed: {exc}", flush=True)
        return {"attempts": state.get("attempts", 0) + 1, "error": str(exc)[:200]}


def append_findings(state: TState) -> TState:
    item = state["product"]
    date = datetime.date.today().isoformat()
    lines = [f"\n## {item['name']} ({date})", "Complaints:"]
    for t in state["themes"]:
        if t.get("kind") == "complaint":
            lines.append(f"- {t['theme']} — {t.get('share', '?')} — \"{t.get('quote', '')}\"")
    lines.append("Praise:")
    for t in state["themes"]:
        if t.get("kind") != "complaint":
            lines.append(f"- {t['theme']} — {t.get('share', '?')}")
    lines.append("Wedge notes:")
    lines += [f"- {w}" for w in state.get("wedge_notes", [])]
    FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FINDINGS_PATH.open("a") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"append: findings for {item['id']} written", flush=True)
    return {}


def update_state(state: TState) -> TState:
    st = load_state()
    item = state["product"]
    themes = state.get("themes") or []
    for b in st["backlog"]:
        if b["id"] == item["id"]:
            if themes:
                b["status"] = "done"
                b["notes"] = [f"langgraph run: {len(state['themes'])} themes, "
                              f"{state['blocked']} sources blocked"]
                st["history"].append({"date": now_iso()[:10], "item": b["id"],
                                      "themes_found": len(state["themes"]),
                                      "by": "langgraph-cron"})
            else:
                b["status"] = "partial"
                b["notes"] = [f"extraction failed: {state.get('error', 'unknown')}"]
                st["last_error"] = state.get("error")
    save_state(st)
    top = themes[0]["theme"] if themes else "n/a"
    print(f"tick {item['id']}: {len(themes)} themes (top: {top}); "
          f"{state['blocked']} sources blocked", flush=True)
    return {}


# ---------------------------------------------------------------- wiring
def route_after_pick(state: TState) -> str:
    return END if state.get("skipped") else "fetch_reviews"


def route_after_extract(state: TState) -> str:
    if state.get("themes"):
        return "append_findings"
    if state.get("attempts", 0) < MAX_ATTEMPTS and state.get("sources_text"):
        return "retry_extract"
    return "update_state"


def main() -> int:
    dry = "--dry-run" in sys.argv
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    graph = StateGraph(TState)
    graph.add_node("pick_product", pick_product)
    graph.add_node("fetch_reviews", fetch_reviews_node)
    graph.add_node("extract_themes", extract_themes)
    graph.add_node("retry_extract", lambda s: {"attempts": s.get("attempts", 0) + 1})
    graph.add_node("append_findings", append_findings)
    graph.add_node("update_state", update_state)
    graph.set_entry_point("pick_product")
    graph.add_conditional_edges("pick_product", route_after_pick,
                                {"fetch_reviews": "fetch_reviews", END: END})
    graph.add_edge("fetch_reviews", "extract_themes")
    graph.add_conditional_edges("extract_themes", route_after_extract, {
        "append_findings": "append_findings",
        "retry_extract": "retry_extract",
        "update_state": "update_state"})
    graph.add_edge("retry_extract", "extract_themes")
    graph.add_edge("append_findings", "update_state")
    graph.add_edge("update_state", END)
    app = graph.compile()
    if dry:
        state = {}
        state.update(pick_product(state))
        if not state.get("skipped"):
            state.update(fetch_reviews_node(state))
        print(f"DRY RUN: sources_text chars = {len(state.get('sources_text', ''))}, "
              f"{state.get('blocked', '?')} blocked")
        return 0
    final = app.invoke({})
    if not LLM_CHAT_URL:
        sys.exit("LLM_CHAT_URL not set")
    return 0 if not final.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
