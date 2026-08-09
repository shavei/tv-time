"""A local web UI for discovery, so you see the actual posters.

The terminal can only approximate an image. This serves a page on 127.0.0.1
and opens it in your browser, where the poster ``<img>`` tags point straight at
Simkl's CDN - so the *browser* fetches them, at full quality, and this script
never downloads or stores a single image. The browser's own HTTP cache covers
Simkl's "cache images by URL" rule the ordinary way.

The flow is a wall of posters you click through, then one screen to say how
much of each show you watched. Everything lands in the same queue the terminal
mode fills, so ``--send`` is unchanged.

Only the standard library is used here - no web framework, no new dependency.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .images import poster_url
from .models import TV, WatchItem
from .progress import ProgressError, apply_progress, parse_progress
from .taste import candidate_genres, candidate_rating

BIND_HOST = "127.0.0.1"
# 0 lets the OS pick a free port; overridden by --web-port
DEFAULT_PORT = 0
MAX_BODY = 4 * 1024 * 1024


def candidate_payload(entry: Dict[str, Any], match: float, item: WatchItem, show_match: bool) -> Dict[str, Any]:
    candidate = entry["candidate"]
    rating = candidate_rating(candidate)
    return {
        "id": str(item.ids.get("simkl")),
        "title": item.title,
        "year": item.year,
        "type": item.media_type,
        "is_movie": item.is_movie,
        "genres": [g.title() for g in candidate_genres(candidate)],
        "rating": rating,
        "match": round(match) if show_match else None,
        "bucket": entry.get("genre", ""),
        # full-quality poster straight from Simkl's CDN, fetched by the browser
        "poster": poster_url(item.poster, size="_ca", width=300) if item.poster else "",
        "url": f"https://simkl.com/{'movies' if item.is_movie else 'tv'}/{item.ids.get('simkl')}",
    }


class DiscoverySession:
    """Holds the candidate list and collects the browser's answers."""

    def __init__(self, ranked: List[Tuple[Dict[str, Any], float]], profile, queued_keys):
        self.token = secrets.token_urlsafe(24)
        self.profile = profile
        self.queued_keys = queued_keys
        self.finished = threading.Event()
        self.cancelled = False
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

    # ------------------------------------------------------------------ state

    def state(self) -> Dict[str, Any]:
        return {
            "candidates": self.payload,
            "profile": "" if self.profile.empty else self.profile.summary().strip(),
            "has_match": not self.profile.empty,
        }

    def commit(self, answers: Dict[str, Any]) -> Dict[str, Any]:
        """Apply the browser's answers. Returns an error dict or {'ok': True}."""
        accepted: List[WatchItem] = []
        problems: List[str] = []

        for record in answers.get("accepted") or []:
            key = str(record.get("id"))
            item = self.items.get(key)
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
        self.finished.set()
        return {"ok": True, "queued": len(accepted), "rejected": len(self.rejected_ids)}

    def cancel(self) -> None:
        self.cancelled = True
        self.finished.set()


class Handler(BaseHTTPRequestHandler):
    session: DiscoverySession = None  # set by make_server
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the terminal clean
        pass

    # ------------------------------------------------------------- guardrails

    def _host_ok(self) -> bool:
        """Only accept loopback Host headers (blocks DNS-rebinding from a page)."""
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _token_ok(self, query: Dict[str, List[str]]) -> bool:
        return secrets.compare_digest(query.get("token", [""])[0], self.session.token)

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

        if parsed.path == "/api/state":
            if not self._token_ok(query):
                return self._json(403, {"error": "bad token"})
            return self._json(200, self.session.state())

        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._token_ok(query):
            return self._json(403, {"error": "bad token"})

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})

        if parsed.path == "/api/commit":
            return self._json(200, self.session.commit(body))
        if parsed.path == "/api/cancel":
            self.session.cancel()
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})


def run_web_discovery(
    session_data: Dict[str, Any],
    store,
    queue: List[WatchItem],
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> List[WatchItem]:
    """Serve the picker, block until the browser submits, return the new queue."""
    from .discovery import _remember_accepted

    ranked = session_data["ranked"]
    if not ranked:
        return queue

    session = DiscoverySession(ranked, session_data["profile"], session_data["queued_keys"])
    if not session.payload:
        print("  Nothing to show.")
        return queue

    handler = type("BoundHandler", (Handler,), {"session": session})
    server = ThreadingHTTPServer((BIND_HOST, port), handler)
    actual_port = server.server_address[1]
    url = f"http://{BIND_HOST}:{actual_port}/?token={session.token}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("\n" + "=" * 60)
    print("  Poster picker is running in your browser.")
    print(f"  {url}")
    print("=" * 60)
    print("  Click every title you have watched, then press Continue.")
    print("  Nothing is written to disk - your browser loads the posters directly.")
    print("  Ctrl-C here to stop without saving.\n")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        while not session.finished.wait(timeout=0.5):
            pass
    except KeyboardInterrupt:
        session.cancel()
        print("\n  Cancelled.")
    finally:
        server.shutdown()
        server.server_close()

    if session.cancelled:
        return queue

    queued_keys = session_data["queued_keys"]
    added = 0
    for item in session.accepted:
        if item.dedupe_key() in queued_keys:
            continue
        queue.append(item)
        queued_keys.add(item.dedupe_key())
        _remember_accepted(store, item)
        added += 1
    store.save_queue([item.to_dict() for item in queue])

    if session.rejected_ids:
        rejected = session_data["rejected"]
        for simkl_id in session.rejected_ids:
            item = session.items.get(simkl_id)
            if item is not None:
                rejected[simkl_id] = {"title": item.title, "genres": item.genres}
        store.save_rejected(rejected)

    print(f"  Added {added} title(s); {len(session.rejected_ids)} marked as not watched.")
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
    --bg: #f6f6f7; --panel: #ffffff; --ink: #16161a; --muted: #6b6b76;
    --line: #e2e2e8; --accent: #0b7285; --accent-ink: #ffffff; --shadow: rgba(0,0,0,.10);
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
    display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
    box-shadow: 0 1px 6px var(--shadow);
  }
  h1 { font-size: 17px; margin: 0; font-weight: 650; }
  .grow { flex: 1 1 auto; }
  .muted { color: var(--muted); font-size: 13px; }
  input[type=search], input[type=text] {
    background: var(--bg); color: var(--ink); border: 1px solid var(--line);
    border-radius: 8px; padding: 8px 11px; font: inherit; min-width: 0;
  }
  button {
    background: var(--accent); color: var(--accent-ink); border: 0; border-radius: 8px;
    padding: 9px 16px; font: inherit; font-weight: 600; cursor: pointer;
  }
  button.ghost { background: transparent; color: var(--ink); border: 1px solid var(--line); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  main { padding: 20px; max-width: 1500px; margin: 0 auto; }
  .grid {
    display: grid; gap: 16px;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  }
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
  .chip {
    background: transparent; color: var(--muted); border: 1px solid var(--line);
    border-radius: 20px; padding: 4px 10px; font-size: 12px; margin-right: 5px; cursor: pointer;
  }
  .err { color: #e03131; font-size: 13px; margin: 10px 0; }
  .done { text-align: center; padding: 70px 20px; }
  .done h2 { font-size: 24px; margin: 0 0 10px; }
  .hide { display: none !important; }
</style>
</head>
<body>
<header>
  <h1>What have you watched?</h1>
  <span id="count" class="muted"></span>
  <span class="grow"></span>
  <input type="search" id="filter" placeholder="Filter by title or genre">
  <button class="ghost" id="back">Back</button>
  <button id="next">Continue</button>
</header>

<main>
  <p id="profile" class="muted"></p>
  <div id="errors"></div>

  <section id="pick">
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
let candidates = [], picked = new Set(), step = 'pick';

const $ = (id) => document.getElementById(id);

function card(c) {
  const el = document.createElement('button');
  el.className = 'card' + (picked.has(c.id) ? ' on' : '');
  el.type = 'button';
  el.dataset.id = c.id;
  const shot = document.createElement('div');
  shot.className = 'shot';
  if (c.poster) {
    const img = document.createElement('img');
    img.src = c.poster;
    img.alt = '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
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

function placeholder(c) {
  const d = document.createElement('div');
  d.className = 'noimg';
  d.textContent = c.title;
  return d;
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
  $('count').textContent = picked.size + ' of ' + candidates.length + ' selected';
  $('next').disabled = step === 'pick' && picked.size === 0;
}

function showDetail() {
  step = 'detail';
  $('pick').classList.add('hide');
  $('detail').classList.remove('hide');
  $('filter').classList.add('hide');
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
      field.type = 'text';
      field.value = 'all';
      field.dataset.for = c.id;
      field.size = 18;
      ['all', 's1', 's1-s2', 's1-s3'].forEach(preset => {
        const chip = document.createElement('button');
        chip.type = 'button'; chip.className = 'chip'; chip.textContent = preset;
        chip.onclick = () => { field.value = preset; };
        input.appendChild(chip);
      });
      input.appendChild(field);
    }
    tr.append(thumb, title, input);
    table.appendChild(tr);
  });
  refreshCount();
}

function backToPick() {
  step = 'pick';
  $('detail').classList.add('hide');
  $('pick').classList.remove('hide');
  $('filter').classList.remove('hide');
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
  const res = await fetch(api('/api/commit'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ accepted, rejected }),
  });
  const data = await res.json();
  $('next').disabled = false;

  if (!data.ok) {
    $('errors').innerHTML = '';
    (data.errors || ['Something went wrong.']).forEach(text => {
      const p = document.createElement('p');
      p.className = 'err';
      p.textContent = text;
      $('errors').appendChild(p);
    });
    return;
  }
  $('errors').textContent = '';
  $('detail').classList.add('hide');
  $('done').classList.remove('hide');
  $('doneText').textContent =
    data.queued + ' title(s) queued, ' + data.rejected + ' marked as not watched.';
  $('next').classList.add('hide');
  $('back').classList.add('hide');
  $('filter').classList.add('hide');
  $('count').textContent = '';
}

$('next').onclick = () => (step === 'pick' ? showDetail() : commit());
$('back').onclick = () => (step === 'detail' ? backToPick() : null);
$('filter').oninput = render;

fetch(api('/api/state'))
  .then(r => r.json())
  .then(data => {
    candidates = data.candidates;
    if (data.profile) $('profile').textContent = data.profile;
    render();
  });
</script>
</body>
</html>
"""
