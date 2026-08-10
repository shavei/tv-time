"""The whole discovery flow, in your browser.

Running ``--discover`` starts a server on 127.0.0.1 and opens a page. Everything
happens there: picking what to browse, watching the candidate list build, and
clicking through a wall of real posters. The terminal only prints the URL and
the final summary.

Poster ``<img>`` tags point straight at Simkl's CDN, so the *browser* fetches
them at full quality and this script never downloads or stores an image. The
browser's own HTTP cache covers Simkl's "cache images by URL" rule.

Standard library only - no web framework, no new dependency.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .images import poster_url
from .models import TV, WatchItem
from .progress import ProgressError, apply_progress, parse_progress
from .taste import candidate_genres, candidate_rating


def open_in_browser(url: str) -> bool:
    """Open a URL, trying every mechanism this platform offers.

    webbrowser.open() returns False rather than raising when it cannot find a
    browser, and on Windows it can fail quietly, so we check the result and
    fall through to the OS handlers instead of assuming it worked.
    """
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except Exception:
            pass
        for command in (["cmd", "/c", "start", "", url], ["explorer", url]):
            try:
                subprocess.Popen(command, shell=False)
                return True
            except Exception:
                continue
        return False

    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 0  # 0 lets the OS pick a free port; overridden by --web-port
MAX_BODY = 4 * 1024 * 1024
SECTION_LABELS = {"tv": "TV shows", "movies": "Movies", "anime": "Anime"}


def candidate_payload(entry: Dict[str, Any], match: float, item: WatchItem, show_match: bool) -> Dict[str, Any]:
    candidate = entry["candidate"]
    return {
        "id": str(item.ids.get("simkl")),
        "title": item.title,
        "year": item.year,
        "type": item.media_type,
        "is_movie": item.is_movie,
        "genres": [g.title() for g in candidate_genres(candidate)],
        "rating": candidate_rating(candidate),
        "match": round(match) if show_match else None,
        "bucket": entry.get("genre", ""),
        # full-quality poster straight from Simkl's CDN, fetched by the browser
        "poster": poster_url(item.poster, size="_ca", width=300) if item.poster else "",
        "url": f"https://simkl.com/{'movies' if item.is_movie else 'tv'}/{item.ids.get('simkl')}",
    }


class DiscoverySession:
    """The candidate list, and the answers the browser sends back."""

    def __init__(self, ranked: List[Tuple[Dict[str, Any], float]], profile, queued_keys):
        self.profile = profile
        self.queued_keys = queued_keys
        self.accepted: List[WatchItem] = []
        self.rejected_ids: List[str] = []

        from .discovery import SECTION_TO_TYPE, candidate_to_item

        show_match = not profile.empty
        self.items: Dict[str, WatchItem] = {}
        self.payload: List[Dict[str, Any]] = []
        for entry, match in ranked:
            item = candidate_to_item(entry["candidate"], SECTION_TO_TYPE.get(entry["section"], TV))
            if item is None:
                continue
            key = str(item.ids.get("simkl"))
            if key in self.items:
                continue
            self.items[key] = item
            self.payload.append(candidate_payload(entry, match, item, show_match))

    def commit(self, answers: Dict[str, Any]) -> Dict[str, Any]:
        """Apply the browser's answers. Returns an error dict or {'ok': True}."""
        accepted: List[WatchItem] = []
        problems: List[str] = []

        for record in answers.get("accepted") or []:
            item = self.items.get(str(record.get("id")))
            if item is None:
                continue
            spec = (record.get("progress") or "all").strip()
            if item.is_movie:
                accepted.append(item)
                continue
            try:
                segments = parse_progress(spec)
            except ProgressError as exc:
                problems.append(f"{item.label()}: {exc}")
                continue
            apply_progress(item, segments)
            accepted.append(item)

        if problems:
            return {"ok": False, "errors": problems}

        self.accepted = accepted
        self.rejected_ids = [str(i) for i in (answers.get("rejected") or []) if str(i) in self.items]
        return {"ok": True, "queued": len(accepted), "rejected": len(self.rejected_ids)}


def _blank_profile():
    from .taste import TasteProfile

    return TasteProfile()


class WebFlow:
    """Drives setup -> build -> pick, with the browser polling for progress."""

    def __init__(self, client, store, queue, refresh_library=False, use_taste=True):
        self.client = client
        self.store = store
        self.queue = queue
        self.refresh_library = refresh_library
        self.use_taste = use_taste

        self.token = secrets.token_urlsafe(24)
        self.finished = threading.Event()
        self.cancelled = False

        self._lock = threading.Lock()
        self._log: List[str] = []
        self.prepared: Optional[Dict[str, Any]] = None
        self.prep_error = ""
        self.building = False
        self.build_error = ""
        self.session: Optional[DiscoverySession] = None
        self.session_data: Optional[Dict[str, Any]] = None
        self.committed: Optional[Dict[str, Any]] = None

    # -------------------------------------------------------------- logging

    def log(self, line: str) -> None:
        text = str(line).strip()
        if text:
            with self._lock:
                self._log.append(text)

    def log_lines(self) -> List[str]:
        with self._lock:
            return list(self._log)

    # ---------------------------------------------------------------- phases

    def start_prep(self) -> None:
        """Learn the taste profile while the setup form is being filled in."""
        threading.Thread(target=self._prep, daemon=True).start()

    def _prep(self) -> None:
        from .discovery import prepare_taste

        try:
            self.prepared = prepare_taste(
                self.client,
                self.store,
                self.queue,
                refresh_library=self.refresh_library,
                use_taste=self.use_taste,
                log=self.log,
            )
        except Exception as exc:  # surfaced in the page rather than swallowed
            self.prep_error = str(exc)
            self.log(f"  Could not load your taste profile: {exc}")

    def prep_state(self) -> Dict[str, Any]:
        from .discovery import SUGGESTED_GENRES

        return {
            "ready": self.prepared is not None or bool(self.prep_error),
            "error": self.prep_error,
            "suggested": (self.prepared or {}).get("suggested", []),
            "summary": (self.prepared or {}).get("summary", ""),
            "all_genres": SUGGESTED_GENRES,
            "sections": SECTION_LABELS,
            "log": self.log_lines(),
        }

    def start_build(self, options: Dict[str, Any]) -> Dict[str, Any]:
        if self.building:
            return {"ok": False, "error": "already building"}
        if self.prepared is None and not self.prep_error:
            return {"ok": False, "error": "still reading your account - one moment"}
        self.building = True
        threading.Thread(target=self._build, args=(options,), daemon=True).start()
        return {"ok": True}

    def _build(self, options: Dict[str, Any]) -> None:
        from .discovery import ALL_SECTIONS, build_session

        try:
            sections = [s for s in (options.get("sections") or []) if s in ALL_SECTIONS]
            data = build_session(
                self.client,
                self.store,
                self.queue,
                self.prepared or {"library": {}, "profile": _blank_profile(), "suggested": []},
                sections=sections or list(ALL_SECTIONS),
                favourites=[t for t in (options.get("favourites") or []) if str(t).strip()],
                genres=[g for g in (options.get("genres") or []) if str(g).strip()],
                per_genre=int(options.get("per_genre") or 20),
                include_trending=bool(options.get("trending")),
                log=self.log,
            )
            self.session_data = data
            self.session = DiscoverySession(data["ranked"], data["profile"], data["queued_keys"])
        except Exception as exc:
            self.build_error = str(exc)
            self.log(f"  Build failed: {exc}")

    def build_state(self) -> Dict[str, Any]:
        data = self.session_data or {}
        return {
            "ready": self.session is not None or bool(self.build_error),
            "error": self.build_error,
            "log": self.log_lines(),
            "candidates": self.session.payload if self.session else [],
            "has_match": bool(self.session and not self.session.profile.empty),
            "skipped": data.get("skipped", 0),
            "skip_reasons": data.get("skip_reasons", []),
        }

    # ---------------------------------------------------------------- finish

    def commit(self, answers: Dict[str, Any]) -> Dict[str, Any]:
        if self.session is None:
            return {"ok": False, "errors": ["nothing to commit yet"]}
        result = self.session.commit(answers)
        if result.get("ok"):
            self.committed = result
            self.finished.set()
        return result

    def cancel(self) -> None:
        self.cancelled = True
        self.finished.set()


class Handler(BaseHTTPRequestHandler):
    flow: WebFlow = None  # bound in run_web_discovery
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the terminal clean
        pass

    # ------------------------------------------------------------- guardrails

    def _host_ok(self) -> bool:
        """Only accept loopback Host headers (blocks DNS-rebinding from a page)."""
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _token_ok(self, query: Dict[str, List[str]]) -> bool:
        return secrets.compare_digest(query.get("token", [""])[0], self.flow.token)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    # ------------------------------------------------------------------ verbs

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/":
            if not self._token_ok(query):
                return self._send(403, b"Bad or missing token. Use the URL printed in the terminal.", "text/plain")
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")

        if not self._token_ok(query):
            return self._json(403, {"error": "bad token"})
        if parsed.path == "/api/prep":
            return self._json(200, self.flow.prep_state())
        if parsed.path == "/api/build":
            return self._json(200, self.flow.build_state())
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        parsed = urlparse(self.path)
        if not self._token_ok(parse_qs(parsed.query)):
            return self._json(403, {"error": "bad token"})

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})

        if parsed.path == "/api/start":
            return self._json(200, self.flow.start_build(body))
        if parsed.path == "/api/commit":
            return self._json(200, self.flow.commit(body))
        if parsed.path == "/api/cancel":
            self.flow.cancel()
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})


def run_web_discovery(
    client,
    store,
    queue: List[WatchItem],
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    refresh_library: bool = False,
    use_taste: bool = True,
) -> List[WatchItem]:
    """Serve the whole flow, block until the browser submits, return the queue."""
    from .discovery import _remember_accepted

    flow = WebFlow(client, store, queue, refresh_library, use_taste)
    handler = type("BoundHandler", (Handler,), {"flow": flow})
    server = ThreadingHTTPServer((BIND_HOST, port), handler)
    url = f"http://{BIND_HOST}:{server.server_address[1]}/?token={flow.token}"

    threading.Thread(target=server.serve_forever, daemon=True).start()
    flow.start_prep()

    opened = open_in_browser(url) if open_browser else False

    print("\n" + "=" * 68)
    if opened:
        print("  Opening in your browser. If nothing appeared, paste this in:")
    else:
        print("  Open this in your browser:")
    print(f"\n    {url}\n")
    print("=" * 68)
    print("  Everything happens there. Ctrl-C here to stop without saving.\n")

    try:
        while not flow.finished.wait(timeout=0.5):
            pass
    except KeyboardInterrupt:
        flow.cancel()
        print("\n  Cancelled.")
    finally:
        server.shutdown()
        server.server_close()

    if flow.cancelled or flow.session is None or flow.session_data is None:
        return queue

    queued_keys = flow.session_data["queued_keys"]
    added = 0
    for item in flow.session.accepted:
        if item.dedupe_key() in queued_keys:
            continue
        queue.append(item)
        queued_keys.add(item.dedupe_key())
        _remember_accepted(store, item)
        added += 1
    store.save_queue([item.to_dict() for item in queue])

    if flow.session.rejected_ids:
        rejected = flow.session_data["rejected"]
        for simkl_id in flow.session.rejected_ids:
            item = flow.session.items.get(simkl_id)
            if item is not None:
                rejected[simkl_id] = {"title": item.title, "genres": item.genres}
        store.save_rejected(rejected)

    print(f"  Added {added} title(s); {len(flow.session.rejected_ids)} marked as not watched.")
    print(f"  {len(queue)} in the queue overall.")
    return queue


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Simkl importer - what have you watched?</title>
<style>
  :root {
    --bg: #f6f6f7; --panel: #fff; --ink: #16161a; --muted: #6b6b76;
    --line: #e2e2e8; --accent: #0b7285; --accent-ink: #fff; --shadow: rgba(0,0,0,.10);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #131316; --panel: #1c1c21; --ink: #f0f0f3; --muted: #9a9aa6;
      --line: #2e2e36; --accent: #3bc9db; --accent-ink: #08282e; --shadow: rgba(0,0,0,.45);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  header {
    position: sticky; top: 0; z-index: 10; background: var(--panel);
    border-bottom: 1px solid var(--line); padding: 14px 20px;
    display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
    box-shadow: 0 1px 6px var(--shadow);
  }
  h1 { font-size: 17px; margin: 0; font-weight: 650; }
  .grow { flex: 1 1 auto; }
  .muted { color: var(--muted); font-size: 13px; }
  input[type=search], input[type=text], input[type=number] {
    background: var(--bg); color: var(--ink); border: 1px solid var(--line);
    border-radius: 8px; padding: 9px 11px; font: inherit; min-width: 0;
  }
  button {
    background: var(--accent); color: var(--accent-ink); border: 0; border-radius: 8px;
    padding: 9px 16px; font: inherit; font-weight: 600; cursor: pointer;
  }
  button.ghost { background: transparent; color: var(--ink); border: 1px solid var(--line); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  main { padding: 20px; max-width: 1500px; margin: 0 auto; }
  .setup { max-width: 700px; }
  .field { margin: 0 0 22px; }
  .field > label { display: block; font-weight: 600; margin-bottom: 7px; }
  .field input[type=text] { width: 100%; }
  .chips { display: flex; flex-wrap: wrap; gap: 7px; }
  .chip {
    background: transparent; color: var(--ink); border: 1px solid var(--line);
    border-radius: 20px; padding: 6px 13px; font-size: 13px; font-weight: 500; cursor: pointer;
  }
  .chip.on { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
  .logbox {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 14px 16px; font: 12.5px/1.7 ui-monospace, Menlo, Consolas, monospace;
    max-height: 380px; overflow-y: auto; white-space: pre-wrap; color: var(--muted);
  }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .card {
    background: var(--panel); border: 2px solid transparent; border-radius: 12px;
    overflow: hidden; cursor: pointer; position: relative; text-align: left;
    padding: 0; color: inherit; display: flex; flex-direction: column;
    box-shadow: 0 1px 4px var(--shadow); transition: transform .08s ease;
  }
  .card:hover { transform: translateY(-2px); }
  .card.on { border-color: var(--accent); }
  .card .shot { aspect-ratio: 2 / 3; background: var(--line); position: relative; }
  .card img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .card .noimg {
    position: absolute; inset: 0; display: grid; place-items: center;
    color: var(--muted); font-size: 12px; padding: 8px; text-align: center;
  }
  .card .meta { padding: 9px 10px 11px; }
  .card .name { font-weight: 600; font-size: 13.5px; line-height: 1.3; }
  .card .sub { color: var(--muted); font-size: 11.5px; margin-top: 3px; }
  .tick {
    position: absolute; top: 8px; right: 8px; width: 26px; height: 26px;
    border-radius: 50%; background: var(--accent); color: var(--accent-ink);
    display: none; place-items: center; font-weight: 700; font-size: 15px;
  }
  .card.on .tick { display: grid; }
  .match {
    position: absolute; bottom: 8px; left: 8px; background: rgba(0,0,0,.72);
    color: #fff; font-size: 11px; padding: 2px 7px; border-radius: 20px;
  }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  td.thumb { width: 54px; } td.thumb img { width: 46px; border-radius: 5px; display: block; }
  .err { color: #e03131; font-size: 13px; margin: 10px 0; }
  .done { text-align: center; padding: 70px 20px; }
  .done h2 { font-size: 24px; margin: 0 0 10px; }
  .spinner {
    width: 15px; height: 15px; border: 2px solid var(--line); border-top-color: var(--accent);
    border-radius: 50%; display: inline-block; animation: spin .8s linear infinite;
    vertical-align: -2px; margin-right: 7px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .hide { display: none !important; }
</style>
</head>
<body>
<header>
  <h1 id="heading">What have you watched?</h1>
  <span id="count" class="muted"></span>
  <span class="grow"></span>
  <input type="search" id="filter" class="hide" placeholder="Filter by title or genre">
  <button class="ghost hide" id="back">Back</button>
  <button id="next">Find titles</button>
</header>

<main>
  <div id="errors"></div>

  <section id="setup" class="setup">
    <p class="muted" id="profileLine"><span class="spinner"></span>Reading your Simkl account…</p>

    <div class="field">
      <label>What do you want to go through?</label>
      <div class="chips" id="sections"></div>
    </div>

    <div class="field">
      <label for="favourites">Favourite shows or movies <span class="muted">(optional)</span></label>
      <input type="text" id="favourites" placeholder="Breaking Bad, Severance, Arrival">
      <p class="muted">Used to work out which genres to show you. Comma separated.</p>
    </div>

    <div class="field">
      <label>Genres to browse</label>
      <div class="chips" id="genres"></div>
      <p class="muted">Leave none selected to use whatever your taste profile suggests.</p>
    </div>

    <div class="field">
      <label for="perGenre">Titles per genre</label>
      <input type="number" id="perGenre" value="20" min="1" max="50" style="width:90px">
      <label style="display:inline-block;margin-left:20px;font-weight:400">
        <input type="checkbox" id="trending"> also include Simkl Trending
      </label>
    </div>
  </section>

  <section id="building" class="hide">
    <p class="muted"><span class="spinner"></span>Building your list…</p>
    <div class="logbox" id="log"></div>
  </section>

  <section id="pick" class="hide">
    <p class="muted" id="skipLine"></p>
    <div class="grid" id="grid"></div>
  </section>

  <section id="detail" class="hide">
    <p class="muted">How much of each show did you watch? Blank or "all" means the whole thing.
       You can write <code>s1</code>, <code>1-3</code>, <code>s2e5</code>, <code>s2e1-10</code>,
       or combinations like <code>s1, s2e1-4</code>.</p>
    <table id="rows"></table>
  </section>

  <section id="done" class="done hide">
    <h2>Queued.</h2>
    <p class="muted" id="doneText"></p>
    <p class="muted">Go back to the terminal to send it to Simkl.</p>
  </section>
</main>

<script>
const token = new URLSearchParams(location.search).get('token');
const api = (path) => path + '?token=' + encodeURIComponent(token);
const $ = (id) => document.getElementById(id);

let step = 'setup';
let candidates = [], picked = new Set();
let chosenSections = new Set(['tv', 'movies', 'anime']);
let chosenGenres = new Set();

function show(name) {
  ['setup', 'building', 'pick', 'detail', 'done'].forEach(s =>
    $(s).classList.toggle('hide', s !== name));
  step = name;
}

function chip(label, on, onToggle) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = 'chip' + (on ? ' on' : '');
  el.textContent = label;
  el.onclick = () => { el.classList.toggle('on'); onToggle(el.classList.contains('on')); };
  return el;
}

function showErrors(list) {
  $('errors').textContent = '';
  (list || []).forEach(text => {
    const p = document.createElement('p');
    p.className = 'err';
    p.textContent = text;
    $('errors').appendChild(p);
  });
}

// ------------------------------------------------------------------- setup

function renderSetup(prep) {
  const sections = $('sections');
  sections.textContent = '';
  Object.entries(prep.sections).forEach(([key, label]) => {
    sections.appendChild(chip(label, chosenSections.has(key),
      on => on ? chosenSections.add(key) : chosenSections.delete(key)));
  });

  const genres = $('genres');
  genres.textContent = '';
  const all = Array.from(new Set([...(prep.suggested || []), ...prep.all_genres]));
  all.forEach(name => {
    const on = (prep.suggested || []).includes(name);
    if (on) chosenGenres.add(name);
    genres.appendChild(chip(name, on,
      active => active ? chosenGenres.add(name) : chosenGenres.delete(name)));
  });

  if (prep.error) {
    $('profileLine').textContent = 'Could not read your account: ' + prep.error;
  } else if (prep.summary) {
    $('profileLine').textContent = prep.summary.replace(/\s+/g, ' ');
  } else {
    $('profileLine').textContent =
      'No taste profile yet — this round will be ordered by popularity.';
  }
}

async function pollPrep() {
  const prep = await (await fetch(api('/api/prep'))).json();
  if (!prep.ready) { setTimeout(pollPrep, 600); return; }
  renderSetup(prep);
}

// ---------------------------------------------------------------- building

async function startBuild() {
  const body = {
    sections: Array.from(chosenSections),
    genres: Array.from(chosenGenres),
    favourites: $('favourites').value.split(',').map(s => s.trim()).filter(Boolean),
    per_genre: parseInt($('perGenre').value, 10) || 20,
    trending: $('trending').checked,
  };
  if (!body.sections.length) {
    return showErrors(['Pick at least one of TV shows, Movies or Anime.']);
  }
  showErrors([]);
  const res = await (await fetch(api('/api/start'), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })).json();
  if (!res.ok) return showErrors([res.error || 'Could not start.']);

  show('building');
  $('next').disabled = true;
  $('heading').textContent = 'Building your list';
  pollBuild();
}

async function pollBuild() {
  const state = await (await fetch(api('/api/build'))).json();
  $('log').textContent = state.log.join('\n');
  $('log').scrollTop = $('log').scrollHeight;
  if (!state.ready) { setTimeout(pollBuild, 700); return; }

  $('next').disabled = false;
  if (state.error) {
    show('setup');
    $('heading').textContent = 'What have you watched?';
    return showErrors([state.error]);
  }
  candidates = state.candidates;
  if (!candidates.length) {
    // nothing to submit, so release the terminal rather than leaving it waiting
    await fetch(api('/api/cancel'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    show('done');
    $('heading').textContent = 'Nothing new to ask about';
    document.querySelector('#done h2').textContent = 'Nothing new.';
    $('doneText').textContent = state.skipped
      ? 'All ' + state.skipped + ' title(s) found were ones you have already answered: '
        + state.skip_reasons.join(', ') + '.'
      : 'No titles came back for those genres. Try more genres, or a larger count per genre.';
    ['next', 'back', 'filter'].forEach(id => $(id).classList.add('hide'));
    return;
  }
  if (state.skipped) {
    $('skipLine').textContent =
      'Skipped ' + state.skipped + ' you have seen before: ' + state.skip_reasons.join(', ') + '.';
  }
  $('heading').textContent = 'Click everything you have watched';
  $('filter').classList.remove('hide');
  $('next').textContent = 'Continue';
  show('pick');
  render();
}

// -------------------------------------------------------------------- pick

function placeholder(c) {
  const d = document.createElement('div');
  d.className = 'noimg';
  d.textContent = c.title;
  return d;
}

function card(c) {
  const el = document.createElement('button');
  el.className = 'card' + (picked.has(c.id) ? ' on' : '');
  el.type = 'button';

  const shot = document.createElement('div');
  shot.className = 'shot';
  if (c.poster) {
    const img = document.createElement('img');
    img.src = c.poster; img.alt = ''; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
    img.onerror = () => { img.remove(); shot.appendChild(placeholder(c)); };
    shot.appendChild(img);
  } else {
    shot.appendChild(placeholder(c));
  }
  if (c.match !== null && c.match !== undefined) {
    const m = document.createElement('span');
    m.className = 'match';
    m.textContent = c.match + '% match';
    shot.appendChild(m);
  }
  const tick = document.createElement('span');
  tick.className = 'tick';
  tick.textContent = '✓';
  shot.appendChild(tick);

  const meta = document.createElement('div');
  meta.className = 'meta';
  const name = document.createElement('div');
  name.className = 'name';
  name.textContent = c.title;
  const sub = document.createElement('div');
  sub.className = 'sub';
  const bits = [];
  if (c.year) bits.push(c.year);
  if (c.genres.length) bits.push(c.genres.slice(0, 2).join(', '));
  if (c.rating) bits.push('★ ' + c.rating);
  sub.textContent = bits.join(' · ');
  meta.append(name, sub);

  el.append(shot, meta);
  el.onclick = () => {
    picked.has(c.id) ? picked.delete(c.id) : picked.add(c.id);
    el.classList.toggle('on');
    refreshCount();
  };
  return el;
}

function render() {
  const needle = $('filter').value.trim().toLowerCase();
  const grid = $('grid');
  grid.textContent = '';
  candidates
    .filter(c => !needle
      || c.title.toLowerCase().includes(needle)
      || c.genres.join(' ').toLowerCase().includes(needle))
    .forEach(c => grid.appendChild(card(c)));
  refreshCount();
}

function refreshCount() {
  if (step !== 'pick' && step !== 'detail') { $('count').textContent = ''; return; }
  $('count').textContent = picked.size + ' of ' + candidates.length + ' selected';
  $('next').disabled = step === 'pick' && picked.size === 0;
}

// ------------------------------------------------------------------ detail

function showDetail() {
  show('detail');
  $('heading').textContent = 'How much did you watch?';
  $('filter').classList.add('hide');
  $('back').classList.remove('hide');
  $('next').textContent = 'Add to queue';

  const table = $('rows');
  table.textContent = '';
  candidates.filter(c => picked.has(c.id)).forEach(c => {
    const tr = document.createElement('tr');

    const thumb = document.createElement('td');
    thumb.className = 'thumb';
    if (c.poster) {
      const img = document.createElement('img');
      img.src = c.poster; img.alt = ''; img.referrerPolicy = 'no-referrer';
      thumb.appendChild(img);
    }

    const title = document.createElement('td');
    title.textContent = c.title + (c.year ? ' (' + c.year + ')' : '');

    const input = document.createElement('td');
    if (c.is_movie) {
      const span = document.createElement('span');
      span.className = 'muted';
      span.textContent = 'movie';
      input.appendChild(span);
    } else {
      const field = document.createElement('input');
      field.type = 'text'; field.value = 'all'; field.dataset.for = c.id; field.size = 18;
      ['all', 's1', 's1-s2', 's1-s3'].forEach(preset => {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'chip'; b.textContent = preset;
        b.onclick = () => { field.value = preset; };
        input.appendChild(b);
      });
      input.appendChild(field);
    }
    tr.append(thumb, title, input);
    table.appendChild(tr);
  });
  refreshCount();
}

function backToPick() {
  show('pick');
  $('heading').textContent = 'Click everything you have watched';
  $('filter').classList.remove('hide');
  $('back').classList.add('hide');
  $('next').textContent = 'Continue';
  refreshCount();
}

async function commit() {
  const accepted = candidates.filter(c => picked.has(c.id)).map(c => {
    const field = document.querySelector('input[data-for="' + CSS.escape(c.id) + '"]');
    return { id: c.id, progress: field ? field.value : 'all' };
  });
  const rejected = candidates.filter(c => !picked.has(c.id)).map(c => c.id);

  $('next').disabled = true;
  const data = await (await fetch(api('/api/commit'), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ accepted, rejected }),
  })).json();
  $('next').disabled = false;

  if (!data.ok) return showErrors(data.errors || ['Something went wrong.']);
  showErrors([]);
  show('done');
  $('heading').textContent = 'Done';
  $('doneText').textContent =
    data.queued + ' title(s) queued, ' + data.rejected + ' marked as not watched.';
  ['next', 'back', 'filter'].forEach(id => $(id).classList.add('hide'));
  $('count').textContent = '';
}

$('next').onclick = () => {
  if (step === 'setup') return startBuild();
  if (step === 'pick') return showDetail();
  if (step === 'detail') return commit();
};
$('back').onclick = () => { if (step === 'detail') backToPick(); };
$('filter').oninput = render;

pollPrep();
</script>
</body>
</html>
"""
