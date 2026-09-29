'use strict';
function formatBytes(value) { return value == null ? '—' : `${Math.round(value / 1048576)} MiB`; }
function toModelIds(data) { return [...new Set((Array.isArray(data.models) ? data.models : []).map(m => typeof m === 'string' ? m : m.id).filter(m => typeof m === 'string'))]; }
const LOG_RANK = {DEBUG: 0, INFO: 1, WARN: 2, WARNING: 2, ERROR: 3, CRITICAL: 4};
/** Keep only the log lines at or above the selected severity. Unknown lines pass through. */
function filterLogs(lines, mode) {
  if (!Array.isArray(lines)) return [];
  if (!mode || mode === 'all') return lines.slice();
  const min = LOG_RANK[String(mode).toUpperCase()];
  if (min === undefined) return lines.slice();
  return lines.filter(line => {
    const m = /"level"\s*:\s*"([A-Z]+)"/.exec(String(line));
    return m ? (LOG_RANK[m[1]] ?? 0) >= min : true;
  });
}
/** Summarise provider health straight from the diagnostics payload. */
function summariseDiagnostics(d) {
  if (!d || typeof d !== 'object') return [];
  const out = [];
  for (const m of Array.isArray(d.ai_by_model) ? d.ai_by_model : []) {
    const failed = Number(m.failed || 0);
    const calls = Number(m.calls || 0);
    const pct = calls ? Math.round((failed / calls) * 100) : 0;
    out.push({label: `مدل ${m.model}`, value: `${calls} فراخوانی · ${failed} خطا (${pct}٪)`, bad: failed > 0});
  }
  const statuses = d.ai_by_status && typeof d.ai_by_status === 'object' ? d.ai_by_status : {};
  for (const [status, n] of Object.entries(statuses)) {
    if (String(status).toLowerCase() !== 'ok') out.push({label: 'وضعیت درخواست‌ها', value: `${status}: ${n}`, bad: true});
  }
  const recovered = Array.isArray(d.recovered) ? d.recovered : [];
  if (recovered.length) out.push({label: 'پاسخ‌های ناقص/بازیابی‌شده', value: String(recovered.length), bad: true});
  for (const [status, n] of Object.entries(d.decisions_by_kind || {})) {
    out.push({label: `تصمیم ${status}`, value: String(n), bad: /PARSE_ERROR|REJECTED/.test(String(status))});
  }
  return out;
}
if (typeof module !== 'undefined') module.exports = {formatBytes, toModelIds, filterLogs, summariseDiagnostics};
if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', () => {
  const $ = id => document.getElementById(id);
  let csrf = '', refreshing = false, activeTab = 'settings';
  const titles = {settings:'تنظیمات هوش مصنوعی', monitor:'وضعیت معاملات', debug:'دیباگ و لاگ'};
  function notice(text, error = false) { $('notice').hidden = !text; $('notice').textContent = text; $('notice').className = error ? 'error' : ''; }
  async function api(path, body) {
    const headers = {};
    if (body !== undefined) { headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = csrf; }
    const res = await fetch(path, {method:body === undefined ? 'GET' : 'POST', headers, body:body === undefined ? undefined : JSON.stringify(body), signal:AbortSignal.timeout(95000)});
    let data;
    try { data = await res.json(); } catch { throw new Error(`پاسخ نامعتبر سرور (HTTP ${res.status})`); }
    if (!res.ok || data.ok === false) throw new Error(data.error || data.message || `HTTP ${res.status}`);
    return data;
  }
  function formData() { return Object.fromEntries(new FormData($('ai-settings')).entries()); }
  async function action(button, work) {
    button.disabled = true;
    notice('');
    try { await work(); } catch (e) { notice(e.message, true); } finally {
      button.disabled = false;
      if (button.id === 'start-agent' || button.id === 'stop-agent') await refresh();
    }
  }
  function table(id, rows, fields) {
    const root = $(id); root.replaceChildren();
    if (!Array.isArray(rows) || !rows.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'هنوز داده‌ای ثبت نشده است.'; root.append(empty); return; }
    const t = document.createElement('table'), h = document.createElement('thead'), b = document.createElement('tbody'), tr = document.createElement('tr');
    for (const [,label] of fields) { const c = document.createElement('th'); c.textContent = label; tr.append(c); }
    h.append(tr);
    for (const row of rows.slice(0, 50)) { const r = document.createElement('tr'); for (const [key] of fields) { const c = document.createElement('td'); c.textContent = typeof row[key] === 'object' && row[key] !== null ? JSON.stringify(row[key]) : (row[key] ?? '—'); r.append(c); } b.append(r); }
    t.append(h,b); root.append(t);
  }
  async function loadSettings() {
    const s = await api('/api/settings');
    for (const key of ['base_url','model_normal','model_watch','model_critical']) if (s[key] !== undefined) $('ai-settings').elements[key].value = s[key];
    $('key-status').textContent = s.api_key_set ? 'کلید ذخیره شده' : 'بدون کلید';
  }
  async function monitor() {
    const [positions,trades,theses,performance] = await Promise.all(['/api/positions','/api/trades','/api/thesis','/api/performance'].map(p => api(p)));
    table('positions',positions,[['symbol','نماد'],['direction','جهت'],['entry_price','ورود'],['stop_loss','SL'],['take_profit','TP'],['volume','حجم']]);
    table('trades',trades,[['symbol','نماد'],['direction','جهت'],['status','وضعیت'],['profit_loss','PnL'],['exit_reason','دلیل خروج']]);
    $('theses').replaceChildren();
    for (const [symbol,t] of Object.entries(theses)) { const d = document.createElement('details'), s = document.createElement('summary'), p = document.createElement('p'); s.textContent = `${symbol} · ${t.bias} · ${t.regime}`; p.textContent = t.thesis; d.append(s,p); $('theses').append(d); }
    $('performance').textContent = JSON.stringify(performance.summary ?? performance,null,2);
  }
  function renderDiagnostics(d) {
    const root = $('diagnostics'); root.replaceChildren();
    const rows = summariseDiagnostics(d);
    if (!rows.length) { const e = document.createElement('div'); e.className = 'empty'; e.textContent = 'هنوز داده‌ای برای خلاصه‌سازی وجود ندارد.'; root.append(e); return; }
    for (const r of rows) {
      const row = document.createElement('div'); row.className = 'diag-row' + (r.bad ? ' bad' : '');
      const l = document.createElement('span'); l.textContent = r.label;
      const v = document.createElement('strong'); v.textContent = r.value;
      row.append(l, v); root.append(row);
    }
  }
  async function debug() {
    const d = await api('/api/debug');
    renderDiagnostics(d);
    table('ai-requests',d.ai_requests,[['ts','زمان'],['model','مدل'],['status','وضعیت'],['total_tokens','توکن'],['task','کار']]);
    table('decisions',d.decisions,[['ts','زمان'],['symbol','نماد'],['decision','تصمیم'],['reject_reasons','دلایل'],['payload','جزئیات']]);
    table('events',d.events,[['ts','زمان'],['event','رویداد'],['payload','جزئیات']]);
  }
  function renderLogs(logs) {
    const lines = filterLogs(Array.isArray(logs) ? logs : [], $('log-filter').value);
    $('process-logs').textContent = lines.join('\n') || (Array.isArray(logs) && logs.length ? 'هیچ خطی با این فیلتر مطابقت ندارد.' : 'No process logs yet.');
    $('log-state').textContent = `${Array.isArray(logs) ? logs.length : 0} خط`;
  }
  async function refreshMt5Status() {
    const box = $('mt5-status');
    if ($('run-mode').value !== 'mt5') { box.textContent = 'شبیه‌سازی فعال است؛ به MT5 نیازی نیست.'; return; }
    box.textContent = 'در حال بررسی ترمینال…';
    try {
      const s = await api('/api/mt5/status');
      box.textContent = s.ready ? 'ترمینال MT5 متصل و روی حساب دمو تأیید شد.' : s.reason;
    } catch (e) { box.textContent = e.message; }
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const [health,process,overview,lastError] = await Promise.all(['/api/health','/api/process','/api/overview','/api/last_error'].map(p => api(p)));
      overview.ai_failing = lastError.failing ? lastError : null;
      overview.ai_recent_failures = lastError.recent_failures || 0;
      const memory = health.memory || {};
      $('memory-label').textContent = `${formatBytes(memory.current_bytes)} / ${formatBytes(memory.limit_bytes)}`;
      const pct = memory.percent ?? (memory.limit_bytes ? 100 * memory.current_bytes / memory.limit_bytes : 0);
      $('memory-bar').style.width = `${Math.max(0,Math.min(100,pct))}%`;
      $('memory-detail').textContent = memory.limit_bytes ? `${Math.round(pct)}% مصرف · Swap limit: ${formatBytes(memory.swap_limit_bytes)}` : 'محدودیت حافظه در این سیستم قابل اندازه‌گیری نیست';
      $('process-state').textContent = process.running ? 'در حال اجرا' : 'متوقف';
      $('process-state').className = process.running ? 'good' : '';
      $('process-detail').textContent = process.running ? `PID ${process.pid}` : `Exit: ${process.exit_code ?? '—'}`;
      $('start-agent').disabled = !!process.running;
      $('stop-agent').disabled = !process.running;
      window.__lastLogs = Array.isArray(process.logs) ? process.logs : [];
      renderLogs(window.__lastLogs);
      $('token-used').textContent = (overview.token_usage?.total_tokens ?? 0).toLocaleString();
      $('open-pos').textContent = overview.open_positions ?? '—';
      $('pnl-label').textContent = `PnL امروز (ثبت‌شده): ${overview.today_pnl ?? '—'}`;
      if (activeTab === 'monitor') await monitor();
      if (activeTab === 'debug') await debug();
      $('connection').textContent = 'پنل متصل'; $('connection').className = 'badge good';
      const failing = $('ai-alert');
      if (overview.ai_failing) {
        failing.hidden = false;
        const why = overview.ai_failing.reason ? ` دلیل: ${overview.ai_failing.reason}` : '';
        failing.textContent = `آخرین خطای AI روی مدل ${overview.ai_failing.model} (${overview.ai_failing.task}) — ${overview.ai_recent_failures} خطا در ۱۵ دقیقهٔ اخیر.${why}`;
      } else {
        failing.hidden = true;
      }
      $('last-update').textContent = `Updated ${new Date().toLocaleTimeString()}`;
    } catch (e) { $('connection').textContent = 'خطای اتصال'; $('connection').className = 'badge bad'; notice(e.message,true); }
    finally { refreshing = false; }
  }
  document.querySelectorAll('[data-tab]').forEach(btn => btn.addEventListener('click', () => {
    activeTab = btn.dataset.tab;
    document.querySelectorAll('.tab').forEach(t => { t.hidden = t.id !== activeTab; });
    document.querySelectorAll('.nav').forEach(n => n.classList.toggle('active',n === btn));
    $('page-title').textContent = titles[activeTab]; refresh();
  }));
  $('ai-settings').addEventListener('submit', e => { e.preventDefault(); action($('save-settings'),async () => { await api('/api/settings',formData()); $('api-key').value = ''; await loadSettings(); notice('تنظیمات ذخیره شد. برای اعمال در تریدرِ در حال اجرا، آن را متوقف و دوباره شروع کنید.'); }); });
  $('load-models').addEventListener('click', () => action($('load-models'),async () => { const ids = toModelIds(await api('/api/ai/models',formData())); $('model-options').replaceChildren(); for (const id of ids) { const opt = document.createElement('option'); opt.value = id; $('model-options').append(opt); } notice(`${ids.length} مدل دریافت شد؛ شناسه دلخواه را در فیلد مدل تایپ یا انتخاب کنید.`); }));
  $('test-ai').addEventListener('click', () => action($('test-ai'),async () => { $('ai-result').textContent = 'Sending one test request…'; try { const f = formData(), result = await api('/api/ai/test',{...f,model:f[$('test-level').value]}); $('ai-result').textContent = JSON.stringify(result,null,2); } catch (e) { $('ai-result').textContent = e.message; throw e; } }));
  $('start-agent').addEventListener('click', () => action($('start-agent'),async () => { if (!confirm('تریدر در محیط انتخاب‌شده شروع شود؟ شروع می‌تواند درخواست API و سفارش شبیه‌سازی/دمو ایجاد کند.')) return; await api('/api/process/start',{mode:$('run-mode').value}); await refresh(); }));
  $('stop-agent').addEventListener('click', () => action($('stop-agent'),async () => { await api('/api/process/stop',{}); await refresh(); }));
  $('refresh').addEventListener('click',refresh);
  $('run-mode').addEventListener('change',refreshMt5Status);
  $('log-filter').addEventListener('change',() => renderLogs((window.__lastLogs || [])));
  $('log-clear').addEventListener('click',() => { $('process-logs').textContent = 'نما پاک شد (لاگ اصلی در حافظه باقی است).'; });
  $('log-copy').addEventListener('click',async () => {
    const text = $('process-logs').textContent || '';
    try { await navigator.clipboard.writeText(text); notice('لاگ کپی شد.'); }
    catch { notice('کپی خودکار ممکن نبود؛ متن را دستی انتخاب کنید.', true); }
  });
  async function boot() { try { csrf = (await api('/api/session')).csrf_token; await loadSettings(); await refresh(); await refreshMt5Status(); } catch(e) { notice(e.message,true); $('connection').textContent = 'خطای راه‌اندازی'; } }
  boot();
  // No overlapping polls and no background-tab traffic. Never polls the AI provider.
  setInterval(() => { if (!document.hidden) refresh(); },15000);
});
