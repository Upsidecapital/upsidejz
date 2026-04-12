// Upside - Polytracker Dashboard Client
// Connects to /ws WebSocket and renders live state.

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // ---------- WebSocket ----------
  let ws = null;
  let reconnectDelay = 1000;
  let lastState = null;

  function connect() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/ws`;
    ws = new WebSocket(url);

    ws.onopen = () => {
      reconnectDelay = 1000;
      setConnection("CONNECTED", "connected");
    };

    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (data._ping) return;
        lastState = data;
        render(data);
      } catch (e) {
        console.error("Parse error", e);
      }
    };

    ws.onclose = () => {
      setConnection("DISCONNECTED · RECONNECTING", "disconnected");
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    };

    ws.onerror = () => ws.close();
  }

  function setConnection(text, cls) {
    const el = $("connection-state");
    el.textContent = text;
    el.className = "conn " + cls;
  }

  // ---------- Format helpers ----------
  const fmtUSD = (n) => {
    const sign = n < 0 ? "-" : "";
    const abs = Math.abs(n);
    if (abs >= 1_000_000)
      return sign + "$" + (abs / 1_000_000).toFixed(2) + "M";
    if (abs >= 10_000)
      return sign + "$" + (abs / 1000).toFixed(1) + "k";
    return sign + "$" + abs.toFixed(2);
  };

  const fmtUSDRaw = (n) => {
    const sign = n < 0 ? "-" : "";
    return (
      sign +
      "$" +
      Math.abs(n).toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })
    );
  };

  const fmtSigned = (n) => (n >= 0 ? "+" : "") + fmtUSDRaw(n);
  const fmtPct = (n, digits = 2) =>
    (n >= 0 ? "+" : "") + n.toFixed(digits) + "%";
  const fmtTime = (ts) => {
    const d = new Date(ts * 1000);
    return (
      d.getHours().toString().padStart(2, "0") +
      ":" +
      d.getMinutes().toString().padStart(2, "0") +
      ":" +
      d.getSeconds().toString().padStart(2, "0")
    );
  };

  const fmtUptime = (secs) => {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = Math.floor(secs % 60);
    return (
      String(h).padStart(2, "0") +
      ":" +
      String(m).padStart(2, "0") +
      ":" +
      String(s).padStart(2, "0")
    );
  };

  // ---------- Clock ----------
  function tickClock() {
    const d = new Date();
    const t =
      d.getHours().toString().padStart(2, "0") +
      ":" +
      d.getMinutes().toString().padStart(2, "0") +
      ":" +
      d.getSeconds().toString().padStart(2, "0");
    $("clock").textContent = t;
    if (lastState) {
      $("status-uptime").textContent = fmtUptime(lastState.uptime_seconds || 0);
    }
  }
  setInterval(tickClock, 1000);
  tickClock();

  // ---------- Canvas chart (line) ----------
  function drawEquityChart(canvas, points, startValue) {
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = rect.height;
    ctx.clearRect(0, 0, w, h);

    if (!points || points.length < 2) {
      ctx.fillStyle = "#4a525c";
      ctx.font = "11px JetBrains Mono, monospace";
      ctx.textAlign = "center";
      ctx.fillText("Awaiting data…", w / 2, h / 2);
      return;
    }

    const vals = points.map((p) => p.v);
    const times = points.map((p) => p.t);
    const minV = Math.min(...vals, startValue);
    const maxV = Math.max(...vals, startValue);
    const range = maxV - minV || 1;
    const tMin = times[0];
    const tMax = times[times.length - 1];
    const tRange = tMax - tMin || 1;

    const padL = 48,
      padR = 12,
      padT = 16,
      padB = 24;
    const cw = w - padL - padR;
    const ch = h - padT - padB;

    // Grid lines
    ctx.strokeStyle = "#1b2028";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = padT + (ch / 4) * i;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + cw, y);
      ctx.stroke();

      const v = maxV - (range / 4) * i;
      ctx.fillStyle = "#4a525c";
      ctx.font = "9px JetBrains Mono, monospace";
      ctx.textAlign = "right";
      ctx.fillText(fmtUSD(v), padL - 6, y + 3);
    }

    // Start baseline
    const baselineY = padT + ch - ((startValue - minV) / range) * ch;
    ctx.strokeStyle = "#242932";
    ctx.setLineDash([3, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, baselineY);
    ctx.lineTo(padL + cw, baselineY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Gradient fill
    const lastVal = vals[vals.length - 1];
    const up = lastVal >= startValue;
    const stroke = up ? "#3fcf8e" : "#e5484d";
    const fillStart = up
      ? "rgba(63, 207, 142, 0.25)"
      : "rgba(229, 72, 77, 0.25)";
    const fillEnd = up ? "rgba(63, 207, 142, 0)" : "rgba(229, 72, 77, 0)";

    const grad = ctx.createLinearGradient(0, padT, 0, padT + ch);
    grad.addColorStop(0, fillStart);
    grad.addColorStop(1, fillEnd);

    // Compute path
    ctx.beginPath();
    points.forEach((p, i) => {
      const x = padL + ((p.t - tMin) / tRange) * cw;
      const y = padT + ch - ((p.v - minV) / range) * ch;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });

    // Fill under curve
    ctx.lineTo(padL + cw, padT + ch);
    ctx.lineTo(padL, padT + ch);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line stroke
    ctx.beginPath();
    points.forEach((p, i) => {
      const x = padL + ((p.t - tMin) / tRange) * cw;
      const y = padT + ch - ((p.v - minV) / range) * ch;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1.8;
    ctx.stroke();

    // Last point dot
    const lastPt = points[points.length - 1];
    const lastX = padL + ((lastPt.t - tMin) / tRange) * cw;
    const lastY = padT + ch - ((lastPt.v - minV) / range) * ch;
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
    ctx.fillStyle = stroke;
    ctx.fill();
  }

  function drawMiniChart(canvas, points) {
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = rect.height;
    ctx.clearRect(0, 0, w, h);
    if (!points || points.length < 2) return;

    const vals = points.map((p) => p.v);
    const minV = Math.min(...vals);
    const maxV = Math.max(...vals);
    const range = maxV - minV || 1;

    const first = vals[0];
    const last = vals[vals.length - 1];
    const color = last >= first ? "#3fcf8e" : "#e5484d";

    ctx.beginPath();
    points.forEach((p, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h - ((p.v - minV) / range) * (h - 6) - 3;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.4;
    ctx.stroke();
  }

  // ---------- View switching ----------
  let currentView = "dashboard";

  function switchView(name) {
    currentView = name;
    document.querySelectorAll("[data-view]").forEach((el) => {
      if (el.dataset.view === name) {
        el.hidden = false;
      } else {
        el.hidden = true;
      }
    });
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === name);
    });
    if (lastState) render(lastState);
  }

  // ---------- Render ----------
  function render(s) {
    // Mode pill
    const modePill = $("mode-pill");
    modePill.textContent = s.is_paper ? "PAPER" : "LIVE";
    modePill.className = "mode-pill" + (s.is_paper ? "" : " live");

    // Hero P&L
    const heroEl = $("hero-pnl");
    heroEl.textContent = fmtSigned(s.total_pnl);
    heroEl.className =
      "hero-value" +
      (s.total_pnl > 0 ? "" : s.total_pnl < 0 ? " neg" : " zero");

    const subEl = $("hero-sub");
    subEl.textContent = fmtPct(s.total_pnl_pct) + " since inception";
    subEl.className = "hero-sub" + (s.total_pnl_pct < 0 ? " neg" : "");

    // KPIs
    $("kpi-trades").textContent = s.total_trades;
    $("kpi-winrate").textContent = s.win_rate.toFixed(1) + "%";
    $("kpi-edge").textContent = s.avg_edge.toFixed(2) + "%";
    $("kpi-dd").textContent = s.total_drawdown_pct.toFixed(2) + "%";

    // Prices
    const btc = s.prices.BTC || 0;
    const eth = s.prices.ETH || 0;
    $("btc-badge").textContent =
      "BTC $" + btc.toLocaleString("en-US", { maximumFractionDigits: 0 });
    $("eth-badge").textContent =
      "ETH $" + eth.toLocaleString("en-US", { maximumFractionDigits: 0 });

    // Portfolio sidebar
    $("portfolio-value").textContent = fmtUSDRaw(s.portfolio_value);
    $("portfolio-time").textContent = fmtTime(s.updated_at);
    $("portfolio-sub").textContent =
      "Initial: " +
      fmtUSD(s.initial_portfolio) +
      " · Peak: " +
      fmtUSD(s.peak_portfolio_value);

    $("daily-pnl").textContent = fmtSigned(s.daily_pnl);
    $("daily-dd").textContent = fmtPct(-Math.abs(s.daily_drawdown_pct));
    $("total-dd").textContent = fmtPct(-Math.abs(s.total_drawdown_pct));
    $("open-positions").textContent = s.open_positions_count;

    const killEl = $("kill-switch");
    if (s.trading_halted) {
      killEl.textContent = "TRIGGERED";
      killEl.className = "bad";
    } else if (
      s.daily_drawdown_pct > s.daily_drawdown_limit * 0.6 ||
      s.total_drawdown_pct > s.total_drawdown_limit * 0.6
    ) {
      killEl.textContent = "CAUTION";
      killEl.className = "warn";
    } else {
      killEl.textContent = "ARMED";
      killEl.className = "ok";
    }

    // Status
    $("status-bot").textContent = s.bot_status;
    $("status-binance").textContent = s.price_source
      ? s.price_source
      : s.prices && (s.prices.BTC || s.prices.ETH)
        ? "LIVE"
        : "CONNECTING";
    $("status-polymarket").textContent =
      s.bot_status === "RUNNING" ? "LIVE" : "INIT";

    const dot = $("status-dot");
    if (s.trading_halted) dot.className = "dot bad";
    else if (s.bot_status === "RUNNING") dot.className = "dot ok";
    else dot.className = "dot warn";

    // Asset cards
    renderAssetCards(s);

    // Trade table
    renderTradeTable(s.recent_trades || []);

    // Activity
    renderActivity(s.activity || []);

    // Charts (only when dashboard view is visible — canvas has no size otherwise)
    if (currentView === "dashboard") {
      const canvas = $("equity-chart");
      drawEquityChart(canvas, s.equity_curve || [], s.initial_portfolio);
      $("chart-meta").textContent =
        (s.equity_curve || []).length + " points · " + fmtSigned(s.total_pnl);

      drawMiniChart($("mini-chart"), s.equity_curve || []);
    }

    // Positions view
    renderPositionsView(s);

    // Trade log view
    renderTradeLogView(s);

    // System view
    renderSystemView(s);
  }

  function renderPositionsView(s) {
    const positions = s.open_positions || [];
    $("positions-sub").textContent =
      positions.length + " open" + (positions.length === 1 ? "" : "");
    const tbody = $("positions-tbody");
    if (!positions.length) {
      tbody.innerHTML =
        '<tr><td colspan="10" class="empty">No open positions</td></tr>';
      return;
    }
    tbody.innerHTML = positions
      .map((t) => {
        const time = fmtTime(t.timestamp);
        const pnlCls =
          t.pnl > 0 ? "pnl-pos" : t.pnl < 0 ? "pnl-neg" : "pnl-zero";
        return `
          <tr>
            <td>${time}</td>
            <td>${t.asset}</td>
            <td>${t.timeframe}</td>
            <td>${String(t.direction || "").toUpperCase()}</td>
            <td>${t.side}</td>
            <td>$${Number(t.size_usdc).toFixed(2)}</td>
            <td>${Number(t.entry_price).toFixed(4)}</td>
            <td>${Number(t.edge_pct).toFixed(2)}%</td>
            <td class="${pnlCls}">${
              t.pnl !== 0 ? fmtSigned(t.pnl) : "—"
            }</td>
            <td class="status-open">${t.status}</td>
          </tr>
        `;
      })
      .join("");
  }

  function renderTradeLogView(s) {
    const trades = s.recent_trades || [];
    $("trades-sub").textContent = (s.total_trades || 0) + " trades total";
    $("log-total").textContent = s.total_trades || 0;
    $("log-wins").textContent = s.wins || 0;
    $("log-losses").textContent = s.losses || 0;
    $("log-winrate").textContent = (s.win_rate || 0).toFixed(1) + "%";
    $("log-best").textContent = fmtSigned(s.best_trade || 0);
    $("log-worst").textContent = fmtSigned(s.worst_trade || 0);

    const tbody = $("trades-log-tbody");
    if (!trades.length) {
      tbody.innerHTML =
        '<tr><td colspan="11" class="empty">No trades yet</td></tr>';
      return;
    }
    tbody.innerHTML = trades
      .map((t) => {
        const time = fmtTime(t.timestamp);
        const pnlCls =
          t.pnl > 0 ? "pnl-pos" : t.pnl < 0 ? "pnl-neg" : "pnl-zero";
        const statusCls =
          t.status === "OPEN" ? "status-open" : "status-closed";
        const exit =
          t.exit_price && t.exit_price > 0
            ? Number(t.exit_price).toFixed(4)
            : "—";
        return `
          <tr>
            <td>${time}</td>
            <td>${t.asset}</td>
            <td>${t.timeframe}</td>
            <td>${String(t.direction || "").toUpperCase()}</td>
            <td>${t.side}</td>
            <td>$${Number(t.size_usdc).toFixed(2)}</td>
            <td>${Number(t.entry_price).toFixed(4)}</td>
            <td>${exit}</td>
            <td>${Number(t.edge_pct).toFixed(2)}%</td>
            <td class="${pnlCls}">${
              t.pnl !== 0 ? fmtSigned(t.pnl) : "—"
            }</td>
            <td class="${statusCls}">${t.status}</td>
          </tr>
        `;
      })
      .join("");
  }

  function renderSystemView(s) {
    $("sys-bot").textContent = s.bot_status;
    $("sys-mode").textContent = s.is_paper ? "PAPER" : "LIVE";
    $("sys-binance").textContent = s.price_source
      ? s.price_source
      : s.prices && (s.prices.BTC || s.prices.ETH)
        ? "LIVE"
        : "CONNECTING";
    $("sys-polymarket").textContent =
      s.bot_status === "RUNNING" ? "LIVE" : "INIT";
    $("sys-halted").textContent = s.trading_halted ? "YES" : "No";
    $("sys-halt-reason").textContent = s.halt_reason || "—";
    $("sys-uptime").textContent = fmtUptime(s.uptime_seconds || 0);

    $("sys-daily-dd-limit").textContent =
      (s.daily_drawdown_limit || 0).toFixed(2) + "%";
    $("sys-total-dd-limit").textContent =
      (s.total_drawdown_limit || 0).toFixed(2) + "%";
    $("sys-daily-dd").textContent =
      (s.daily_drawdown_pct || 0).toFixed(2) + "%";
    $("sys-total-dd").textContent =
      (s.total_drawdown_pct || 0).toFixed(2) + "%";
    $("sys-initial").textContent = fmtUSD(s.initial_portfolio || 0);
    $("sys-peak").textContent = fmtUSD(s.peak_portfolio_value || 0);
    $("sys-current").textContent = fmtUSD(s.portfolio_value || 0);

    const dot = $("sys-dot");
    if (s.trading_halted) dot.className = "dot bad";
    else if (s.bot_status === "RUNNING") dot.className = "dot ok";
    else dot.className = "dot warn";

    // Mirror activity feed on the system page
    const list = $("sys-activity-list");
    const activity = s.activity || [];
    if (!activity.length) {
      list.innerHTML = '<div class="empty">No activity yet</div>';
    } else {
      const items = [...activity].reverse().slice(0, 100);
      list.innerHTML = items
        .map((a) => {
          const time = fmtTime(a.timestamp);
          return `
            <div class="activity-item activity-level-${a.level}">
              <span class="activity-time">${time}</span>
              <span class="activity-msg">${escapeHTML(a.message)}</span>
            </div>
          `;
        })
        .join("");
    }
  }

  function renderAssetCards(s) {
    const grid = $("asset-grid");
    const contracts = [
      { asset: "BTC", tf: "5m" },
      { asset: "BTC", tf: "15m" },
      { asset: "ETH", tf: "5m" },
      { asset: "ETH", tf: "15m" },
    ];

    const stats = s.asset_stats || {};
    grid.innerHTML = contracts
      .map((c) => {
        const key = `${c.asset}_${c.tf}`;
        const stat = stats[key] || {
          pnl: 0,
          trades: 0,
          wins: 0,
          edge: 0,
        };
        const cls =
          stat.pnl > 0 ? "pos" : stat.pnl < 0 ? "neg" : "";
        const dotCls =
          stat.trades > 0
            ? stat.pnl >= 0
              ? "active"
              : "bad"
            : "";
        const wr =
          stat.trades > 0
            ? ((stat.wins / stat.trades) * 100).toFixed(0) + "%"
            : "—";
        return `
          <div class="asset-card">
            <div class="asset-card-top">
              <span class="asset-tag">${c.asset} · ${c.tf}</span>
              <span class="asset-dot ${dotCls}"></span>
            </div>
            <div class="asset-pnl ${cls}">${fmtSigned(stat.pnl || 0)}</div>
            <div class="asset-meta">
              <span>${stat.trades || 0} trades</span>
              <span>WR ${wr}</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function renderTradeTable(trades) {
    const tbody = $("trade-tbody");
    if (!trades.length) {
      tbody.innerHTML =
        '<tr><td colspan="10" class="empty">No trades yet</td></tr>';
      return;
    }
    tbody.innerHTML = trades
      .map((t) => {
        const time = fmtTime(t.timestamp);
        const pnlCls =
          t.pnl > 0 ? "pnl-pos" : t.pnl < 0 ? "pnl-neg" : "pnl-zero";
        const statusCls =
          t.status === "OPEN" ? "status-open" : "status-closed";
        return `
          <tr>
            <td>${time}</td>
            <td>${t.asset}</td>
            <td>${t.timeframe}</td>
            <td>${t.direction.toUpperCase()}</td>
            <td>${t.side}</td>
            <td>$${Number(t.size_usdc).toFixed(2)}</td>
            <td>${Number(t.entry_price).toFixed(4)}</td>
            <td>${Number(t.edge_pct).toFixed(2)}%</td>
            <td class="${pnlCls}">${
              t.pnl !== 0 ? fmtSigned(t.pnl) : "—"
            }</td>
            <td class="${statusCls}">${t.status}</td>
          </tr>
        `;
      })
      .join("");
  }

  function renderActivity(activity) {
    const list = $("activity-list");
    if (!activity.length) {
      list.innerHTML = '<div class="empty">No activity yet</div>';
      return;
    }
    // Reverse so most recent is first
    const items = [...activity].reverse().slice(0, 30);
    list.innerHTML = items
      .map((a) => {
        const time = fmtTime(a.timestamp);
        return `
          <div class="activity-item activity-level-${a.level}">
            <span class="activity-time">${time}</span>
            <span class="activity-msg">${escapeHTML(a.message)}</span>
          </div>
        `;
      })
      .join("");
  }

  function escapeHTML(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // ---------- Tabs (real view switching) ----------
  document.querySelectorAll(".tab").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      const target = el.dataset.tab || "dashboard";
      switchView(target);
    });
  });

  // Start
  connect();

  // Redraw chart on window resize
  window.addEventListener("resize", () => {
    if (lastState) render(lastState);
  });
})();
