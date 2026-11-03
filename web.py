"""HTTP API + web UI for tunester (served at tunester.example.com)."""
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pipeline import Options, tune

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


class _Cancelled(Exception):
    """Raised from a job's log callback when the queue is cleared."""


def _clear_all() -> int:
    """Cancel every job and drop all job state. In-flight workers abort
    at their next log() call, which is bounded by the current LLM or
    download step (TUNESTER_LLM_TIMEOUT / TUNESTER_DOWNLOAD_TIMEOUT).
    Downloaded files are left in place."""
    with _LOCK:
        jobs = list(_JOBS.values())
        for job in jobs:
            job["cancelled"] = True
        _JOBS.clear()
    return len(jobs)


def _new_job(name: str, outdir: Path) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "status": "running",
        "log": [],
        "file": None,
        "filename": None,
        "artist": None,
        "title": None,
        "year": None,
        "error": None,
    }
    with _LOCK:
        _JOBS[job["id"]] = job
    threading.Thread(target=_run, args=(job, name, outdir), daemon=True).start()
    return job


def _run(job: dict, name: str, outdir: Path) -> None:
    def log(msg: str) -> None:
        if job.get("cancelled"):
            raise _Cancelled()
        job["log"].append(msg)
        print(msg, flush=True)

    try:
        res = tune(name, Options(outdir=outdir), log=log)
        job["file"] = str(res.file)
        job["filename"] = res.file.name
        job["artist"] = res.artist
        job["title"] = res.title
        job["year"] = res.year
        job["status"] = "done"
    except _Cancelled:
        # The job entry is already gone from _JOBS; this just ends the
        # worker thread at the next stage boundary instead of letting it
        # burn through the source fallback chain.
        job["status"] = "cancelled"
        print(f"job {job['id']} cancelled (queue cleared)", flush=True)
    except Exception as e:
        job["error"] = str(e)
        # Discovery may have succeeded even when no verified file was
        # delivered: keep the fixup data (artist/title/year) on the job.
        t = getattr(e, "target", None)
        if t is not None:
            job["artist"] = t.artist
            job["title"] = t.title
            job["year"] = t.year
        job["status"] = "error"
    except SystemExit as e:
        # sys.exit() inside the pipeline is a BaseException: a plain
        # `except Exception` let such worker threads die silently,
        # leaving the job stuck at "running" forever.
        job["error"] = f"worker exited (SystemExit {e.code})"
        job["status"] = "error"


UI_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tunester</title>
<style>
  :root { color-scheme: dark; }
  body { font: 15px/1.5 system-ui, sans-serif; background: #0e1116; color: #d8dee9;
         max-width: 720px; margin: 4rem auto; padding: 0 1rem; }
  h1 { font-size: 1.6rem; } h1 span { color: #4fd6be; }
  p.sub { color: #8a93a5; }
  textarea { width: 100%; box-sizing: border-box; min-height: 90px; resize: vertical;
             background: #171c24; color: #d8dee9; border: 1px solid #2c3442;
             border-radius: 8px; padding: .7rem; font: inherit; }
  button { margin-top: .6rem; background: #4fd6be; color: #0e1116; border: 0; border-radius: 8px;
           padding: .6rem 1.4rem; font: inherit; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  pre { background: #12161d; border: 1px solid #2c3442; border-radius: 8px; padding: .8rem;
        font: 12.5px/1.6 ui-monospace, monospace; white-space: pre-wrap; word-break: break-word;
        min-height: 2rem; max-height: 400px; overflow: auto; }
  a.dl { display: inline-block; margin-top: .8rem; color: #4fd6be; font-weight: 600; }
  .err { color: #f28fad; }
  .q { margin-top: 2.5rem; border-top: 1px solid #2c3442; padding-top: 1rem; }
  .q .row { display: flex; justify-content: space-between; align-items: center; }
  .q h2 { font-size: 1.15rem; margin: 0; }
  .q button { margin-top: 0; padding: .45rem 1.1rem; font-size: 13.5px; }
  .q ul { list-style: none; padding: 0; margin: .6rem 0 0; }
  .q li { display: flex; gap: .6rem; align-items: baseline; padding: .3rem 0;
          color: #8a93a5; font-size: 13.5px; }
  .q .st { font-weight: 600; flex: none; min-width: 64px; }
  .st.running { color: #f2c94c; } .st.done { color: #4fd6be; }
  .st.error, .st.cancelled { color: #f28fad; }
  .q a { color: #4fd6be; text-decoration: none; }
  .q .err { font-size: 12.5px; }
</style></head>
<body>
<h1>tune<span>ster</span></h1>
<p class="sub">Dirty song name in, verified uncensored MP3 out
(160 kbps CBR / 44.1 kHz / stereo).<br>
Sources, best to worst: YouTube, SoundCloud, archive.org, YouTube (deep).</p>
<textarea id="name" placeholder="e.g. the loud rtm song about killing in the name"></textarea>
<button id="go" onclick="tuneIt()">Tune it</button>
<pre id="log"></pre>
<a id="dl" class="dl" hidden></a>
<div class="q">
  <div class="row"><h2>Queue</h2>
  <button id="clear" onclick="clearQueue()" disabled>Clear queue (0)</button></div>
  <ul id="jobs"></ul>
</div>
<script>
let job = null, timer = null;
async function tuneIt() {
  const name = document.getElementById('name').value.trim();
  if (!name) return;
  const btn = document.getElementById('go'), log = document.getElementById('log'),
        dl = document.getElementById('dl');
  btn.disabled = true; log.textContent = ''; dl.hidden = true;
  const r = await fetch('/api/tune', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
  const d = await r.json();
  if (!r.ok) { log.textContent = 'error: ' + (d.error || r.status); btn.disabled = false; return; }
  job = d.id;
  timer = setInterval(poll, 1500);
  poll();
  refreshJobs();
}
async function poll() {
  if (!job) return;
  const r = await fetch('/api/job/' + job);
  const j = await r.json();
  refreshJobs();
  if (r.status === 404) {
    // the job was cleared while we were polling
    clearInterval(timer);
    document.getElementById('log').textContent += '\\njob cleared';
    return;
  }
  document.getElementById('log').textContent =
    (j.log || []).join('\\n') + (j.status === 'running' ? '\\n...' : '');
  if (j.status !== 'running') {
    clearInterval(timer);
    const btn = document.getElementById('go'), dl = document.getElementById('dl');
    btn.disabled = false;
    if (j.status === 'done' && j.filename) {
      dl.href = '/api/dl/' + job; dl.textContent = 'Download: ' + j.filename;
      dl.hidden = false;
    } else if (j.error) {
      document.getElementById('log').textContent += '\\nerror: ' + j.error;
      document.getElementById('log').classList.add('err');
    }
  }
}
async function refreshJobs() {
  const r = await fetch('/api/jobs');
  const d = await r.json();
  const ul = document.getElementById('jobs');
  const btn = document.getElementById('clear');
  ul.innerHTML = '';
  btn.disabled = !d.jobs.length;
  btn.textContent = 'Clear queue (' + d.jobs.length + ')';
  for (const j of d.jobs) {
    const li = document.createElement('li');
    const st = document.createElement('span');
    st.className = 'st ' + j.status;
    st.textContent = j.status;
    const nm = document.createElement('span');
    nm.textContent = j.name;
    li.append(st, nm);
    if (j.status === 'done' && j.filename) {
      const a = document.createElement('a');
      a.href = '/api/dl/' + j.id;
      a.textContent = j.filename;
      li.append(a);
    } else if (j.error) {
      const er = document.createElement('span');
      er.className = 'err';
      er.textContent = j.error.slice(0, 140);
      li.append(er);
    }
    ul.appendChild(li);
  }
}
async function clearQueue() {
  const btn = document.getElementById('clear');
  btn.disabled = true;
  const r = await fetch('/api/clear', {method: 'POST'});
  const d = await r.json();
  document.getElementById('log').textContent +=
    '\\ncleared ' + d.cleared + ' job(s) from the queue';
  if (job) { job = null; if (timer) clearInterval(timer); }
  document.getElementById('go').disabled = false;
  refreshJobs();
}
refreshJobs();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "tunester/1.0"
    outdir: Path = Path("downloads")

    def log_message(self, fmt, *args):
        print("[http]", fmt % args, flush=True)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path == "/":
            self._send(200, UI_HTML.encode(), "text/html; charset=utf-8")
            return
        if self.path == "/api/health":
            self._json(200, {"ok": True})
            return
        if self.path == "/api/jobs":
            with _LOCK:
                jobs = [
                    {k: job[k] for k in ("id", "name", "status", "filename", "error")}
                    for job in _JOBS.values()
                ]
            jobs.reverse()
            self._json(200, {"jobs": jobs})
            return
        m = re.match(r"^/api/job/([\w]+)$", self.path)
        if m:
            with _LOCK:
                job = _JOBS.get(m.group(1))
            self._json(200 if job else 404, job or {"error": "unknown job"})
            return
        m = re.match(r"^/api/dl/([\w]+)$", self.path)
        if m:
            with _LOCK:
                job = _JOBS.get(m.group(1))
            if not job or job.get("status") != "done" or not job.get("file"):
                self._json(404, {"error": "not ready"})
                return
            path = Path(job["file"])
            if not path.exists():
                self._json(410, {"error": "file gone"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{job["filename"]}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with open(path, "rb") as f:
                self.wfile.write(f.read())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/clear":
            n = _clear_all()
            print(f"queue cleared: {n} job(s)", flush=True)
            self._json(200, {"cleared": n})
            return
        if self.path != "/api/tune":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            name = str(body.get("name") or "").strip()
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return
        if not name:
            self._json(400, {"error": "name required"})
            return
        job = _new_job(name, self.outdir)
        self._json(202, {"id": job["id"]})


def serve(host: str, port: int, outdir: Path) -> None:
    Handler.outdir = outdir
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise SystemExit(f"cannot create output dir {outdir}: {e} (use -o OUTDIR)")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"tunester listening on http://{host}:{port} (output: {outdir})", flush=True)
    httpd.serve_forever()
