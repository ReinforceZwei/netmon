/* netmon dashboard: live status cards + history charts. */
const PALETTE = ['#4f9cf9', '#f2a03d', '#5ec26a', '#e46d8d', '#a78bfa', '#38bdf8', '#f97316', '#94a3b8'];
const charts = {};
let currentRange = null;

const $ = (id) => document.getElementById(id);

function fmtTime(epoch, withDate = true) {
  const d = new Date(epoch * 1000);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return withDate ? `${d.toLocaleDateString([], { month: '2-digit', day: '2-digit' })} ${time}` : time;
}

function fmtAgo(epoch) {
  if (!epoch) return 'never';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function fmtDur(secs) {
  secs = Math.max(0, Math.floor(secs));
  const d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m ${secs % 60}s`;
}

const num = (v, digits = 1) => (v === null || v === undefined || Number.isNaN(v)) ? '–' : Number(v).toFixed(digits);

async function api(url, options = {}) {
  const res = await fetch(url, options);
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) throw new Error(data.error || data.detail || `${res.status} ${res.statusText}`);
  return data;
}

function draw(id, config) {
  const canvas = $(id);
  if (!canvas) return;
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
  charts[id] = new Chart(canvas.getContext('2d'), config);
}

const timeAxis = { grid: { color: 'rgba(148,163,184,0.12)' }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } };

/* ------------------------------------------------------------------ status */
async function loadStatus() {
  let status;
  try {
    status = await api('/api/status');
    $('health-dot').className = 'dot ok';
  } catch (err) {
    $('health-dot').className = 'dot err';
    return;
  }

  $('card-uptime').textContent = fmtDur(status.uptime_sec);
  $('card-monitor-sub').textContent =
    `${status.targets.length} targets · every ${status.settings.ping_interval_sec}s · ${status.counts.ping.toLocaleString()} samples`;

  const targets = $('card-targets');
  const rows = status.targets.map((t) => {
    const info = status.per_target[t] || {};
    const down = (info.consecutive_losses || 0) > 0;
    const gw = t === status.gateway ? ' <span class="muted small">gw</span>' : '';
    const rtt = info.last_rtt_ms !== null && info.last_rtt_ms !== undefined ? `${num(info.last_rtt_ms)} ms` : '<span class="bad">timeout</span>';
    return `<div class="target-row${down ? ' down' : ''}"><span class="mono">${t}</span>${gw}<span>${rtt}</span></div>`;
  });
  targets.innerHTML = rows.length ? rows.join('') : '<span class="muted">no targets configured</span>';

  const s24 = status.summary_24h;
  const avgs = s24.targets.map((t) => t.avg_rtt).filter((v) => v !== null && v !== undefined);
  const avg = avgs.length ? avgs.reduce((a, b) => a + b, 0) / avgs.length : null;
  $('card-rtt24').textContent = avg === null ? '–' : `${num(avg)} ms`;
  const losses = s24.targets.map((t) => t.loss_pct).filter((v) => v !== null && v !== undefined);
  const worst = losses.length ? Math.max(...losses) : null;
  $('card-loss24').textContent = worst === null ? '–' : `${num(worst, 2)} %`;
  $('card-loss24-sub').textContent = `${s24.coverage.samples.toLocaleString()} samples`;
  const rttEl = $('card-rtt24');
  rttEl.className = 'value ' + (avg === null ? '' : avg > (status.settings.ping_interval_sec ? 200 : 200) ? '' : '');

  const last = status.speedtest.last;
  if (last && last.ok) {
    $('card-speed').textContent = `${num(last.download_mbps, 0)} / ${num(last.upload_mbps, 0)} Mbps`;
    $('card-speed-sub').textContent = `ping ${num(last.ping_ms)} ms · ${fmtAgo(last.epoch)}`;
  } else if (last) {
    $('card-speed').textContent = 'failed';
    $('card-speed-sub').textContent = last.ts || '';
  } else {
    $('card-speed').textContent = '–';
    $('card-speed-sub').textContent = 'not run yet';
  }

  const digest = status.settings;
  $('card-digest').textContent = digest.digest_enabled ? digest.digest_time : 'off';
  $('card-digest-sub').textContent = digest.webhook_configured
    ? (status.digest_due && status.digest_due.length ? `due: ${status.digest_due.join(', ')}` : 'webhook set')
    : 'no webhook configured';

  const toggle = $('speedtest-toggle');
  toggle.checked = !!digest.speedtest_enabled;
  const st = status.speedtest;
  $('speedtest-status').textContent = st.running
    ? 'speedtest running… (this briefly saturates the link)'
    : digest.speedtest_enabled
      ? `scheduled every ${digest.speedtest_interval_min} min${st.next_epoch ? `, next ${fmtTime(st.next_epoch, false)}` : ''}`
      : 'scheduled speedtests are off — use “Run speedtest now” when you want one';

  renderEvents(status.recent_events || []);
  renderSpeedTable((await api('/api/speedtests?limit=25')).items);
}

function renderEvents(events) {
  const list = $('event-list');
  if (!events.length) { list.innerHTML = '<li class="muted">nothing yet</li>'; return; }
  list.innerHTML = events.map((e) => {
    const cls = e.level === 'error' ? 'bad' : e.level === 'warn' ? 'warn' : '';
    return `<li><span class="mono small">${fmtTime(e.epoch)}</span> <span class="${cls}">${escapeHtml(e.message || '')}</span></li>`;
  }).join('');
}

function renderSpeedTable(items) {
  const body = document.querySelector('#speed-table tbody');
  if (!items.length) { body.innerHTML = '<tr><td colspan="7" class="muted">no speedtests yet</td></tr>'; return; }
  body.innerHTML = items.map((r) => {
    if (!r.ok) return `<tr class="bad"><td>${fmtTime(r.epoch)}</td><td colspan="6">failed: ${escapeHtml(r.error || '')}</td></tr>`;
    return `<tr><td class="mono">${fmtTime(r.epoch)}</td><td>${num(r.download_mbps)}</td><td>${num(r.upload_mbps)}</td>
      <td>${num(r.ping_ms)}</td><td>${num(r.jitter_ms)}</td><td>${num(r.loss_pct, 2)}</td>
      <td class="trunc" title="${escapeHtml(r.server || '')}">${escapeHtml(r.server || '')}</td></tr>`;
  }).join('');
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ------------------------------------------------------------------ series */
async function loadSeries(range, frm, to) {
  const params = new URLSearchParams();
  if (frm && to) { params.set('from', frm); params.set('to', to); }
  else { params.set('range', range || '24h'); }
  const data = await api(`/api/series?${params.toString()}`);
  currentRange = range;

  const bucket = data.range.bucket;
  // Build the label grid from the REQUESTED window, not from the buckets that
  // happen to have data: otherwise 15 minutes of samples look like a full day.
  const startBucket = Math.floor(data.range.from / bucket) * bucket;
  const labels = [];
  for (let e = startBucket; e <= data.range.to; e += bucket) labels.push(e);
  const index = new Map(labels.map((e, i) => [e, i]));
  const labelText = labels.map((e) => fmtTime(e));
  $('range-info').textContent =
    `${fmtTime(data.range.from)} → ${fmtTime(data.range.to)} · ${labels.length} buckets of ${bucket}s · ` +
    `${data.summary.coverage.samples.toLocaleString()} samples · ${data.speedtests.length} speedtests`;

  const datasets = Object.entries(data.latency).map(([target, pts], i) => {
    const values = new Array(labels.length).fill(null);
    pts.forEach((p) => { if (index.has(p[0])) values[index.get(p[0])] = p[1]; });
    return {
      label: target, data: values, borderColor: PALETTE[i % PALETTE.length],
      backgroundColor: PALETTE[i % PALETTE.length], borderWidth: 1.6,
      pointRadius: 0, tension: 0.25, spanGaps: true,
    };
  });
  draw('chart-latency', {
    type: 'line',
    data: { labels: labelText, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { color: '#cbd5e1', boxWidth: 12, font: { size: 11 } } } },
      scales: { x: timeAxis, y: { title: { display: true, text: 'ms', color: '#94a3b8' }, grid: { color: 'rgba(148,163,184,0.12)' } } },
    },
  });

  const lossDatasets = Object.entries(data.latency).map(([target, pts], i) => {
    const values = new Array(labels.length).fill(null);
    pts.forEach((p) => { if (index.has(p[0])) values[index.get(p[0])] = p[2]; });
    return {
      label: target, data: values, borderColor: PALETTE[i % PALETTE.length],
      backgroundColor: PALETTE[i % PALETTE.length] + '55', borderWidth: 1.2,
      pointRadius: 0, fill: true, tension: 0.2, spanGaps: true,
    };
  });
  draw('chart-loss', {
    type: 'line',
    data: { labels: labelText, datasets: lossDatasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { color: '#cbd5e1', boxWidth: 12, font: { size: 11 } } } },
      scales: {
        x: timeAxis,
        y: { beginAtZero: true, title: { display: true, text: '%', color: '#94a3b8' }, grid: { color: 'rgba(148,163,184,0.12)' } },
      },
    },
  });

  const speed = data.speedtests.slice().sort((a, b) => a.epoch - b.epoch);
  draw('chart-speed', {
    type: 'line',
    data: {
      labels: speed.map((s) => fmtTime(s.epoch)),
      datasets: [
        { label: 'download', data: speed.map((s) => (s.ok ? s.download_mbps : null)), borderColor: PALETTE[0], backgroundColor: PALETTE[0], pointRadius: 3, spanGaps: true, tension: 0.2 },
        { label: 'upload', data: speed.map((s) => (s.ok ? s.upload_mbps : null)), borderColor: PALETTE[2], backgroundColor: PALETTE[2], pointRadius: 3, spanGaps: true, tension: 0.2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { color: '#cbd5e1', boxWidth: 12, font: { size: 11 } } } },
      scales: { x: timeAxis, y: { beginAtZero: true, title: { display: true, text: 'Mbps', color: '#94a3b8' }, grid: { color: 'rgba(148,163,184,0.12)' } } },
    },
  });

  const hourly = data.hourly_profile || [];
  const byHour = new Map(hourly.map((h) => [h.hour, h.avg_rtt]));
  const hours = [...Array(24).keys()];
  draw('chart-hourly', {
    type: 'bar',
    data: {
      // always 24 slots: a single hour with data must not stretch across the axis
      labels: hours.map((h) => String(h).padStart(2, '0')),
      datasets: [{ label: 'avg rtt', data: hours.map((h) => (byHour.has(h) ? byHour.get(h) : null)), backgroundColor: PALETTE[0] + 'cc' }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { title: { display: true, text: 'hour of day (local)', color: '#94a3b8' }, grid: { display: false } },
        y: { beginAtZero: true, title: { display: true, text: 'ms', color: '#94a3b8' }, grid: { color: 'rgba(148,163,184,0.12)' } },
      },
    },
  });
}

/* ------------------------------------------------------------------ actions */
function flash(el, message, ok = true) {
  el.textContent = message;
  el.className = ok ? 'muted small good' : 'small bad';
}

async function runSpeedtest() {
  const status = $('speedtest-status');
  const btn = $('run-speedtest');
  try {
    await api('/api/speedtest/run', { method: 'POST' });
    btn.disabled = true;
    status.textContent = 'speedtest running… (30–60s, the link is busy)';
    const poll = setInterval(async () => {
      const s = await api('/api/status');
      if (!s.speedtest.running) {
        clearInterval(poll);
        btn.disabled = false;
        status.textContent = 'done';
        loadStatus();
      }
    }, 4000);
  } catch (err) {
    btn.disabled = false;
    status.textContent = err.message;
  }
}

async function sendReport() {
  const status = $('speedtest-status');
  status.textContent = 'generating + sending report…';
  try {
    const res = await api('/api/report/send', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    status.textContent = `report for ${res.day} sent`;
  } catch (err) {
    status.textContent = `report failed: ${err.message}`;
  }
}

async function toggleSpeedtest(enabled) {
  try {
    await api('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speedtest: { enabled } }),
    });
    loadStatus();
  } catch (err) {
    $('speedtest-status').textContent = err.message;
  }
}

/* ------------------------------------------------------------------ wiring */
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.range-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      $('from').value = ''; $('to').value = '';
      loadSeries(btn.dataset.range).catch((e) => { $('range-info').textContent = e.message; });
    });
  });

  $('apply-custom').addEventListener('click', () => {
    const frm = $('from').value, to = $('to').value;
    if (!frm || !to) { $('range-info').textContent = 'pick both a start and an end'; return; }
    document.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
    loadSeries(null, frm, to).catch((e) => { $('range-info').textContent = e.message; });
  });

  $('run-speedtest').addEventListener('click', runSpeedtest);
  $('send-report').addEventListener('click', sendReport);
  $('speedtest-toggle').addEventListener('change', (ev) => toggleSpeedtest(ev.target.checked));

  const active = document.querySelector('.range-btn.active');
  const range = active ? active.dataset.range : '24h';
  loadSeries(range).catch((e) => { $('range-info').textContent = e.message; });
  loadStatus().catch(() => {});
  setInterval(() => loadStatus().catch(() => {}), 15000);
  setInterval(() => {
    const btn = document.querySelector('.range-btn.active');
    if (btn) loadSeries(btn.dataset.range).catch(() => {});
  }, 120000);
});
