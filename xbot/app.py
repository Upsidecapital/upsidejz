"""
FastAPI web dashboard for the X Geopolitical News Bot.
Provides real-time monitoring, manual controls, and growth analytics.
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from scheduler import DailyPostingScheduler
from researcher import stream_research_progress
from poster import verify_credentials
from database import Database

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# ── Global state ─────────────────────────────────────────────────────────────
scheduler = DailyPostingScheduler()
db = Database()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start scheduler on boot, stop on shutdown."""
    await scheduler.initialize()
    scheduler.start()
    logger.info("XBot started — scheduler running")
    yield
    scheduler.stop()
    logger.info("XBot stopped")


app = FastAPI(
    title="XBot — Geopolitical News Bot",
    description="Automated geopolitical & financial markets X poster",
    lifespan=lifespan,
)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    """Bot status, credentials, and upcoming jobs."""
    creds = verify_credentials()
    jobs = scheduler.get_next_jobs()
    today = datetime.now(ET).date().isoformat()
    today_posts = await db.get_today_posts(today)

    posted = sum(1 for p in today_posts if p.get("status") == "posted")
    pending = sum(1 for p in today_posts if p.get("status") == "pending")
    failed = sum(1 for p in today_posts if p.get("status") == "failed")

    return {
        "bot_running": scheduler.scheduler.running,
        "current_time_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        "account": creds,
        "today": {
            "date": today,
            "total_posts": len(today_posts),
            "posted": posted,
            "pending": pending,
            "failed": failed,
        },
        "next_jobs": jobs[:5],
    }


@app.get("/api/posts/today")
async def get_today_posts():
    """Get all posts planned/posted for today."""
    today = datetime.now(ET).date().isoformat()
    posts = await db.get_today_posts(today)
    return {"date": today, "posts": posts}


@app.get("/api/posts/recent")
async def get_recent_posts(limit: int = 20):
    """Get recently posted tweets."""
    posts = await db.get_recent_posts(limit=limit)
    return {"posts": posts}


@app.get("/api/stats")
async def get_stats():
    """Get posting stats and follower growth history."""
    posting_stats = await db.get_posting_stats(days=30)
    follower_history = await db.get_follower_history(days=30)
    return {
        "posting_stats": posting_stats,
        "follower_history": follower_history,
    }


@app.post("/api/research/run")
async def run_research_now(background_tasks: BackgroundTasks):
    """Manually trigger research and content generation."""
    background_tasks.add_task(scheduler.run_research_now)
    return {"message": "Research started in background. Check /api/posts/today in ~2 minutes."}


@app.get("/api/research/stream")
async def stream_research():
    """Stream research progress as Server-Sent Events."""
    async def event_generator():
        async for event in stream_research_progress():
            data = json.dumps(event)
            yield f"data: {data}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/posts/{post_index}/publish")
async def publish_post_now(post_index: int):
    """Manually publish a specific post immediately."""
    result = await scheduler.post_now(post_index)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to post"))
    return result


@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now(ET).isoformat()}


# ── Dashboard HTML (single-file, no external dependencies) ───────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XBot — Geopolitical News Bot</title>
<style>
  :root {
    --bg: #0a0e1a;
    --card: #111827;
    --border: #1f2937;
    --text: #e5e7eb;
    --muted: #6b7280;
    --accent: #3b82f6;
    --green: #10b981;
    --red: #ef4444;
    --yellow: #f59e0b;
    --purple: #8b5cf6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; }
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 1rem 2rem; border-bottom: 1px solid var(--border);
    background: var(--card);
  }
  header h1 { font-size: 1.25rem; font-weight: 700; color: var(--accent); }
  header .status { font-size: 0.8rem; color: var(--muted); }
  .live-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--green); animation: pulse 2s infinite; margin-right: 6px; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

  main { max-width: 1400px; margin: 0 auto; padding: 1.5rem 2rem; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem; }
  .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-bottom: 1rem; }
  .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 1rem; }

  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 1.25rem;
  }
  .card h2 { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.1em;
    color: var(--muted); margin-bottom: 0.75rem; }
  .stat-value { font-size: 2rem; font-weight: 700; }
  .stat-sub { font-size: 0.75rem; color: var(--muted); margin-top: 0.25rem; }

  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7rem;
    font-weight: 600; text-transform: uppercase;
  }
  .badge-posted { background: #064e3b; color: var(--green); }
  .badge-pending { background: #1e3a5f; color: #60a5fa; }
  .badge-failed  { background: #4c0519; color: var(--red); }
  .badge-thread  { background: #2d1b69; color: #a78bfa; }
  .badge-breaking { background: #451a03; color: var(--yellow); }

  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { text-align: left; padding: 0.5rem; color: var(--muted);
       font-size: 0.7rem; text-transform: uppercase; border-bottom: 1px solid var(--border); }
  td { padding: 0.6rem 0.5rem; border-bottom: 1px solid #1a2235; vertical-align: top; }
  tr:hover td { background: #161e2e; }

  .tweet-preview { max-width: 400px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; color: var(--text); }
  .tweet-time { color: var(--muted); font-size: 0.75rem; }

  .btn {
    padding: 0.4rem 0.9rem; border-radius: 6px; border: none; cursor: pointer;
    font-size: 0.8rem; font-weight: 600; transition: opacity 0.15s;
  }
  .btn:hover { opacity: 0.8; }
  .btn-primary { background: var(--accent); color: white; }
  .btn-green { background: var(--green); color: black; }
  .btn-sm { padding: 0.2rem 0.6rem; font-size: 0.72rem; }

  .controls { display: flex; gap: 0.75rem; margin-bottom: 1.5rem; align-items: center; }

  #research-log {
    background: #060a14; border: 1px solid var(--border); border-radius: 8px;
    padding: 1rem; height: 180px; overflow-y: auto; font-family: monospace;
    font-size: 0.8rem; color: #94a3b8;
  }
  .log-tool { color: var(--yellow); }
  .log-done { color: var(--green); }
  .log-err  { color: var(--red); }

  .section-title {
    font-size: 0.85rem; font-weight: 600; color: var(--muted);
    margin-bottom: 0.75rem; display: flex; align-items: center; gap: 0.5rem;
  }

  @media (max-width: 900px) {
    .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<header>
  <h1>⚡ XBot — Geopolitical News Bot</h1>
  <div class="status">
    <span class="live-dot"></span>
    <span id="live-time">Loading...</span>
  </div>
</header>

<main>

  <!-- Top stats row -->
  <div class="grid-4" id="stats-row">
    <div class="card">
      <h2>Followers</h2>
      <div class="stat-value" id="stat-followers">—</div>
      <div class="stat-sub">@<span id="stat-username">—</span></div>
    </div>
    <div class="card">
      <h2>Today Posted</h2>
      <div class="stat-value" id="stat-posted" style="color: var(--green)">—</div>
      <div class="stat-sub">of <span id="stat-total">10</span> planned</div>
    </div>
    <div class="card">
      <h2>Pending</h2>
      <div class="stat-value" id="stat-pending" style="color: #60a5fa">—</div>
      <div class="stat-sub">queued today</div>
    </div>
    <div class="card">
      <h2>Failed</h2>
      <div class="stat-value" id="stat-failed" style="color: var(--red)">—</div>
      <div class="stat-sub">today</div>
    </div>
  </div>

  <!-- Controls -->
  <div class="controls">
    <button class="btn btn-primary" onclick="runResearch()">Run Research Now</button>
    <button class="btn btn-green" onclick="refreshStatus()">Refresh Status</button>
    <span id="last-refresh" style="font-size:0.75rem; color: var(--muted)"></span>
  </div>

  <!-- Research log -->
  <div class="card" style="margin-bottom: 1rem;">
    <h2>Research Log</h2>
    <div id="research-log"><span style="color: var(--muted)">Click "Run Research Now" to start...</span></div>
  </div>

  <!-- Today's posts table -->
  <div class="grid-2">
    <div class="card">
      <div class="section-title">Today's Posts Queue</div>
      <table>
        <thead>
          <tr>
            <th>Time ET</th>
            <th>Type</th>
            <th>Topic</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody id="today-posts-body">
          <tr><td colspan="5" style="color: var(--muted); text-align:center; padding: 1rem;">
            Loading...
          </td></tr>
        </tbody>
      </table>
    </div>

    <!-- Next jobs -->
    <div class="card">
      <div class="section-title">Next Scheduled Jobs</div>
      <table>
        <thead>
          <tr><th>Job</th><th>Next Run (ET)</th></tr>
        </thead>
        <tbody id="jobs-body">
          <tr><td colspan="2" style="color: var(--muted)">Loading...</td></tr>
        </tbody>
      </table>

      <div class="section-title" style="margin-top: 1.5rem;">Account Info</div>
      <div id="account-info" style="font-size: 0.82rem; color: var(--muted);">Loading...</div>
    </div>
  </div>

</main>

<script>
let statusInterval;

async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    document.getElementById('live-time').textContent = d.current_time_et;
    document.getElementById('stat-followers').textContent = (d.account?.followers ?? '—').toLocaleString();
    document.getElementById('stat-username').textContent = d.account?.username ?? '—';
    document.getElementById('stat-posted').textContent = d.today.posted;
    document.getElementById('stat-pending').textContent = d.today.pending;
    document.getElementById('stat-failed').textContent = d.today.failed;
    document.getElementById('stat-total').textContent = d.today.total_posts;

    // Jobs table
    const jobsBody = document.getElementById('jobs-body');
    if (d.next_jobs?.length) {
      jobsBody.innerHTML = d.next_jobs.map(j => `
        <tr>
          <td>${j.name}</td>
          <td class="tweet-time">${new Date(j.next_run).toLocaleTimeString('en-US', {timeZone:'America/New_York', hour:'2-digit', minute:'2-digit'})}</td>
        </tr>
      `).join('');
    }

    // Account info
    const acc = d.account;
    if (acc?.success) {
      document.getElementById('account-info').innerHTML = `
        <div><b>@${acc.username}</b> — ${acc.name}</div>
        <div style="margin-top:4px;">
          <span style="color: var(--green)">${acc.followers.toLocaleString()} followers</span> ·
          ${acc.following.toLocaleString()} following ·
          ${acc.tweet_count.toLocaleString()} tweets
        </div>
      `;
    } else {
      document.getElementById('account-info').innerHTML = `<span style="color:var(--red)">X API: ${acc?.error || 'Not connected'}</span>`;
    }

    document.getElementById('last-refresh').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
  } catch(e) {
    console.error('Status refresh failed:', e);
  }
}

async function loadTodayPosts() {
  try {
    const r = await fetch('/api/posts/today');
    const d = await r.json();
    const body = document.getElementById('today-posts-body');

    if (!d.posts?.length) {
      body.innerHTML = '<tr><td colspan="5" style="color:var(--muted); text-align:center; padding:1rem;">No posts generated yet. Click "Run Research Now".</td></tr>';
      return;
    }

    body.innerHTML = d.posts.map((p, i) => `
      <tr>
        <td class="tweet-time">${p.optimal_time_slot || p.optimal_slot || '—'}</td>
        <td>
          <span class="badge ${p.post_type === 'thread' ? 'badge-thread' : p.post_type === 'breaking' ? 'badge-breaking' : 'badge-pending'}">
            ${p.post_type || 'single'}
          </span>
        </td>
        <td class="tweet-preview" title="${(p.topic || '').replace(/"/g, '&quot;')}">${p.topic || '—'}</td>
        <td>
          <span class="badge ${p.status === 'posted' ? 'badge-posted' : p.status === 'failed' ? 'badge-failed' : 'badge-pending'}">
            ${p.status || 'pending'}
          </span>
        </td>
        <td>
          ${p.status === 'pending' ? `<button class="btn btn-primary btn-sm" onclick="publishNow(${i})">Post Now</button>` : ''}
          ${p.tweet_ids?.length ? `<a href="https://x.com/i/web/status/${p.tweet_ids[0]}" target="_blank" style="font-size:0.72rem; color: var(--accent);">View</a>` : ''}
        </td>
      </tr>
    `).join('');
  } catch(e) {
    console.error('Load posts failed:', e);
  }
}

async function publishNow(index) {
  if (!confirm('Post this tweet now?')) return;
  try {
    const r = await fetch('/api/posts/' + index + '/publish', {method: 'POST'});
    const d = await r.json();
    if (d.success) {
      alert('Posted! Tweet IDs: ' + d.tweet_ids.join(', '));
      loadTodayPosts();
    } else {
      alert('Failed: ' + d.detail);
    }
  } catch(e) {
    alert('Error: ' + e.message);
  }
}

async function runResearch() {
  const log = document.getElementById('research-log');
  log.innerHTML = '<span class="log-tool">Starting research...</span>\\n';

  try {
    const r = await fetch('/api/research/stream');
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const event = JSON.parse(line.slice(6));
          if (event.type === 'tool_start') {
            log.innerHTML += `<span class="log-tool">🔍 Searching: ${event.tool}...</span>\\n`;
          } else if (event.type === 'tool_end') {
            log.innerHTML += `<span style="color:#4ade80;">✓ Search complete</span>\\n`;
          } else if (event.type === 'done') {
            log.innerHTML += `<span class="log-done">✅ Research complete — generating posts...</span>\\n`;
            // Trigger post generation in background
            await fetch('/api/research/run', {method: 'POST'});
            setTimeout(() => loadTodayPosts(), 5000);
          } else if (event.type === 'error') {
            log.innerHTML += `<span class="log-err">❌ Error: ${event.message}</span>\\n`;
          }
          log.scrollTop = log.scrollHeight;
        } catch(e) {}
      }
    }
  } catch(e) {
    log.innerHTML += `<span class="log-err">Connection error: ${e.message}</span>\\n`;
  }
}

// Initialize
refreshStatus();
loadTodayPosts();
statusInterval = setInterval(() => {
  refreshStatus();
  loadTodayPosts();
}, 30000);
</script>
</body>
</html>
"""
