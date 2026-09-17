'use strict';
function formatBytes(value) { return value == null ? '—' : `${Math.round(value / 1048576)} MiB`; }
function toModelIds(data) { return [...new Set((Array.isArray(data.models) ? data.models : []).map(m => typeof m === 'string' ? m : m.id).filter(m => typeof m === 'string'))]; }
if (typeof module !== 'undefined') module.exports = {formatBytes, toModelIds};
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
  async function debug() {
    const d = await api('/api/debug');
    table('ai-requests',d.ai_requests,[['ts','زمان'],['model','مدل'],['status','وضعیت'],['total_tokens','توکن'],['task','کار']]);
    table('decisions',d.decisions,[['ts','زمان'],['symbol','نماد'],['decision','تصمیم'],['reject_reasons','دلایل'],['payload','جزئیات']]);
    table('events',d.events,[['ts','زمان'],['event','رویداد'],['payload','جزئیات']]);
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const [health,process,overview] = await Promise.all(['/api/health','/api/process','/api/overview'].map(p => api(p)));
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
      $('process-logs').textContent = Array.isArray(process.logs) ? process.logs.join('\n') || 'No process logs yet.' : process.logs || 'No process logs yet.';
      $('token-used').textContent = (overview.token_usage?.total_tokens ?? 0).toLocaleString();
      $('open-pos').textContent = overview.open_positions ?? '—';
      $('pnl-label').textContent = `PnL امروز (ثبت‌شده): ${overview.today_pnl ?? '—'}`;
      if (activeTab === 'monitor') await monitor();
      if (activeTab === 'debug') await debug();
      $('connection').textContent = 'پنل متصل'; $('connection').className = 'badge good';
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
  async function boot() { try { csrf = (await api('/api/session')).csrf_token; await loadSettings(); await refresh(); } catch(e) { notice(e.message,true); $('connection').textContent = 'خطای راه‌اندازی'; } }
  boot();
  // No overlapping polls and no background-tab traffic. Never polls the AI provider.
  setInterval(() => { if (!document.hidden) refresh(); },15000);
});
