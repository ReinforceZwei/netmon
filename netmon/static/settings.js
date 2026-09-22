/* netmon settings page: load, edit, save. */
const $ = (id) => document.getElementById(id);
const TS = () => ({ 'Content-Type': 'application/json' });

async function api(url, options = {}) {
  const res = await fetch(url, options);
  let data = {};
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) throw new Error(data.error || data.detail || `${res.status} ${res.statusText}`);
  return data;
}

const lines = (value) => String(value || '')
  .split(/[\n,]/).map((s) => s.trim()).filter(Boolean);

function collect() {
  return {
    targets: lines($('s-targets').value),
    auto_add_gateway: $('s-gateway').checked,
    ping_interval_sec: Number($('s-ping-interval').value),
    ping_timeout_sec: Number($('s-ping-timeout').value),
    speedtest: {
      enabled: $('s-speed-enabled').checked,
      interval_min: Number($('s-speed-interval').value),
      timeout_sec: Number($('s-speed-timeout').value),
      ookla_bin: $('s-speed-ookla').value.trim() || 'speedtest',
      min_download_mbps: Number($('s-speed-floor').value),
    },
    digest: {
      enabled: $('s-digest-enabled').checked,
      time: $('s-digest-time').value || '08:00',
      catch_up: $('s-digest-catchup').checked,
      webhook_url: $('s-webhook').value.trim(),
    },
    thresholds: {
      loss_pct: Number($('s-th-loss').value),
      avg_ping_ms: Number($('s-th-ping').value),
      min_download_mbps: Number($('s-th-dl').value),
      min_upload_mbps: Number($('s-th-ul').value),
    },
    alerts: {
      enabled: $('s-alerts-enabled').checked,
      consecutive_losses: Number($('s-alerts-rounds').value),
      cooldown_min: Number($('s-alerts-cooldown').value),
    },
    auth: {
      enabled: $('s-auth-enabled').checked,
      username: $('s-auth-user').value.trim() || 'admin',
      password: $('s-auth-pass').value,
    },
  };
}

function fill(settings) {
  $('s-targets').value = (settings.targets || []).join('\n');
  $('s-gateway').checked = !!settings.auto_add_gateway;
  $('s-ping-interval').value = settings.ping_interval_sec;
  $('s-ping-timeout').value = settings.ping_timeout_sec;
  $('s-speed-enabled').checked = !!settings.speedtest.enabled;
  $('s-speed-interval').value = settings.speedtest.interval_min;
  $('s-speed-timeout').value = settings.speedtest.timeout_sec;
  $('s-speed-ookla').value = settings.speedtest.ookla_bin;
  $('s-speed-floor').value = settings.speedtest.min_download_mbps;
  $('s-digest-enabled').checked = !!settings.digest.enabled;
  $('s-digest-time').value = settings.digest.time;
  $('s-digest-catchup').checked = !!settings.digest.catch_up;
  $('s-webhook').placeholder = settings.digest.webhook_url_set
    ? `unchanged (${settings.digest.webhook_url_hint || 'set'})`
    : 'https://discord.com/api/webhooks/…';
  $('s-th-loss').value = settings.thresholds.loss_pct;
  $('s-th-ping').value = settings.thresholds.avg_ping_ms;
  $('s-th-dl').value = settings.thresholds.min_download_mbps;
  $('s-th-ul').value = settings.thresholds.min_upload_mbps;
  $('s-alerts-enabled').checked = !!settings.alerts.enabled;
  $('s-alerts-rounds').value = settings.alerts.consecutive_losses;
  $('s-alerts-cooldown').value = settings.alerts.cooldown_min;
  $('s-auth-enabled').checked = !!settings.auth.enabled;
  $('s-auth-user').value = settings.auth.username;
  $('s-auth-pass').value = '';
  $('s-webhook').value = '';
}

document.addEventListener('DOMContentLoaded', async () => {
  try {
    const data = await api('/api/settings');
    fill(data.settings);
  } catch (err) {
    $('save-result').textContent = err.message;
  }

  $('save-settings').addEventListener('click', async () => {
    const result = $('save-result');
    result.className = 'muted small';
    result.textContent = 'saving…';
    try {
      const data = await api('/api/settings', { method: 'POST', headers: TS(), body: JSON.stringify(collect()) });
      fill(data.settings);
      result.className = 'small good';
      result.textContent = 'saved — applied live';
    } catch (err) {
      result.className = 'small bad';
      result.textContent = `save failed: ${err.message}`;
    }
  });

  $('reload-settings').addEventListener('click', async () => {
    try {
      fill((await api('/api/settings')).settings);
      $('save-result').className = 'small good';
      $('save-result').textContent = 'reloaded';
    } catch (err) {
      $('save-result').textContent = err.message;
    }
  });

  $('test-webhook').addEventListener('click', async () => {
    const out = $('webhook-result');
    out.className = 'muted small';
    out.textContent = 'sending…';
    try {
      await api('/api/webhook/test', { method: 'POST' });
      out.className = 'small good';
      out.textContent = 'test message sent — check Discord';
    } catch (err) {
      out.className = 'small bad';
      out.textContent = err.message;
    }
  });
});
