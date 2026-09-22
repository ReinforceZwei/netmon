/* netmon data page: prune, export, report browser. */
const $ = (id) => document.getElementById(id);

async function api(url, options = {}) {
  const res = await fetch(url, options);
  let data = {};
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) throw new Error(data.error || data.detail || `${res.status} ${res.statusText}`);
  return data;
}

function stamp() {
  document.querySelectorAll('[data-epoch]').forEach((el) => {
    const epoch = Number(el.dataset.epoch);
    if (!epoch) return;
    el.textContent = new Date(epoch * 1000).toLocaleString();
  });
}

async function loadReports() {
  const body = document.querySelector('#reports-table tbody');
  try {
    const data = await api('/api/reports');
    if (!data.items.length) {
      body.innerHTML = '<tr><td colspan="4" class="muted">no reports generated yet — one is created for each daily digest, or from “Send report now”.</td></tr>';
      return;
    }
    body.innerHTML = data.items.map((r) => `<tr>
      <td class="mono">${r.day.slice(0, 4)}-${r.day.slice(4, 6)}-${r.day.slice(6)}</td>
      <td><a href="/reports/${r.name}" target="_blank">markdown</a></td>
      <td>${r.png ? `<a href="/reports/${r.png}" target="_blank">png</a>` : '<span class="muted">–</span>'}</td>
      <td class="muted small">${r.mtime.replace('T', ' ').slice(0, 16)}</td></tr>`).join('');
  } catch (err) {
    body.innerHTML = `<tr><td colspan="4" class="bad">${err.message}</td></tr>`;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  stamp();
  loadReports();

  $('prune-days-btn').addEventListener('click', async () => {
    const days = Number($('prune-days').value);
    const out = $('prune-result');
    if (!days || days < 1) { out.className = 'small bad'; out.textContent = 'enter a positive number of days'; return; }
    if (!confirm(`Delete all samples older than ${days} days?`)) return;
    out.className = 'muted small';
    out.textContent = 'pruning…';
    try {
      const res = await api('/api/prune', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ days }) });
      out.className = 'small good';
      out.textContent = `deleted ${res.deleted.ping} ping rows, ${res.deleted.speedtest} speedtests, ${res.deleted.events} events`;
      setTimeout(() => location.reload(), 1200);
    } catch (err) {
      out.className = 'small bad';
      out.textContent = err.message;
    }
  });

  $('prune-all-btn').addEventListener('click', async () => {
    const out = $('prune-result');
    const typed = prompt('This deletes ALL history and reports bookkeeping. Type DELETE to confirm:');
    if (typed !== 'DELETE') return;
    out.className = 'muted small';
    out.textContent = 'deleting everything…';
    try {
      const res = await api('/api/prune', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ everything: true, confirm: 'DELETE' }) });
      out.className = 'small good';
      out.textContent = `deleted ${res.deleted.ping} ping rows, ${res.deleted.speedtest} speedtests, ${res.deleted.events} events`;
      setTimeout(() => location.reload(), 1200);
    } catch (err) {
      out.className = 'small bad';
      out.textContent = err.message;
    }
  });

  document.querySelectorAll('.export-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      window.location = `/api/export.csv?table=${btn.dataset.table}&days=${btn.dataset.days}`;
    });
  });
});
