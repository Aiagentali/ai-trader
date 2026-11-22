'use strict';

function formatBytes(value) { return value == null ? '—' : `${Math.round(value / 1048576)} MiB`; }
function toModelIds(data) { return [...new Set((Array.isArray(data.models) ? data.models : []).map(m => typeof m === 'string' ? m : m.id).filter(m => typeof m === 'string'))]; }

const LOG_RANK = {DEBUG: 0, INFO: 1, WARN: 2, WARNING: 2, ERROR: 3, CRITICAL: 4};

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

function renderSvgCurve(svgId, areaId, lineId, points, initialBalance = 0) {
  const svg = document.getElementById(svgId);
  const area = document.getElementById(areaId);
  const line = document.getElementById(lineId);
  if (!svg || !line || !points || points.length < 2) return false;

  const vals = points.map(p => typeof p === 'object' ? (p.balance ?? p.pnl ?? 0) : p);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = (max - min) || 1.0;

  const W = 800, H = 200, padY = 20;
  const stepX = W / (vals.length - 1);

  const coords = vals.map((v, i) => {
    const x = Math.round(i * stepX);
    const y = Math.round(H - padY - ((v - min) / range) * (H - 2 * padY));
    return [x, y];
  });

  const lineD = coords.reduce((acc, [x, y], i) => (i === 0 ? `M${x},${y}` : `${acc} L${x},${y}`), '');
  line.setAttribute('d', lineD);

  if (area) {
    const lastX = coords[coords.length - 1][0];
    const areaD = `${lineD} L${lastX},${H} L0,${H} Z`;
    area.setAttribute('d', areaD);
  }

  // Set line color: jade green if final >= start, else crimson red
  const finalVal = vals[vals.length - 1];
  const startVal = initialBalance > 0 ? initialBalance : vals[0];
  const color = finalVal >= startVal ? '#10b981' : '#f43f5e';
  line.setAttribute('stroke', color);
  return true;
}

function summariseDiagnostics(diag) {
  if (!diag || typeof diag !== 'object') return [];
  const rows = [];
  (diag.ai_by_model || []).forEach(m => {
    const failed = m.failed || 0;
    const calls = m.calls || 0;
    const pct = calls ? Math.round((failed / calls) * 100) : 0;
    rows.push({
      label: `مدل ${m.model}`,
      value: `${calls} فراخوانی · ${failed} خطا (${pct}٪)`,
      bad: failed > 0
    });
  });
  if (diag.ai_by_status) {
    const err = diag.ai_by_status.error || 0;
    rows.push({
      label: 'وضعیت درخواست‌ها',
      value: `موفق: ${diag.ai_by_status.ok || 0} · خطا: ${err}`,
      bad: err > 0
    });
  }
  if (diag.recovered) {
    rows.push({
      label: 'پوزیشن‌های بازیابی‌شده',
      value: `${diag.recovered.length} مورد`,
      bad: false
    });
  }
  if (diag.decisions_by_kind) {
    for (const [kind, count] of Object.entries(diag.decisions_by_kind)) {
      rows.push({
        label: `تصمیم ${kind}`,
        value: `${count} بار`,
        bad: kind === 'PARSE_ERROR'
      });
    }
  }
  return rows;
}

if (typeof module !== 'undefined') module.exports = {formatBytes, toModelIds, filterLogs, renderSvgCurve, summariseDiagnostics};

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', () => {
  const $ = id => document.getElementById(id);
  const setTxt = (id, val) => { const el = $(id); if (el && val !== undefined && val !== null) el.textContent = val; };
  const setCls = (id, cls) => { const el = $(id); if (el && cls !== undefined && cls !== null) el.className = cls; };
  let csrf = '', activeTab = 'monitor';
  let consecutiveNetErrors = 0;

  const titles = {
    monitor: 'داشبورد معاملات و رشد سرمایه',
    settings: 'پیکربندی استراتژی و Decision AI',
    backtest: 'شبیه‌ساز تاریخی و خودآموزی',
    debug: 'کنسول لاگ زنده و سلامت سیستم'
  };

  function notice(text, error = false) {
    const n = $('notice');
    if (!n) return;
    n.hidden = !text;
    n.textContent = text;
    n.className = error ? 'error' : '';
  }

  // Safe DOM node builder (pure DOM APIs)
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  async function api(path, body) {
    const headers = {};
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      if (csrf) headers['X-CSRF-Token'] = csrf;
    }
    const res = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(95000)
    });
    let data;
    try { data = await res.json(); } catch { throw new Error(`پاسخ سرور نامعتبر است (HTTP ${res.status})`); }
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || data.message || `HTTP ${res.status}`);
    }
    return data;
  }

  // --- Tab Navigation ---
  document.querySelectorAll('nav .nav').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('nav .nav').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('section.tab-pane').forEach(s => s.hidden = true);
      btn.classList.add('active');
      activeTab = btn.dataset.tab;
      const target = $(activeTab);
      if (target) target.hidden = false;
      const pageTitle = $('page-title');
      if (pageTitle) pageTitle.textContent = titles[activeTab] || 'میز معاملاتی';
      if (activeTab === 'monitor') refresh();
      if (activeTab === 'settings') loadSettings();
    });
  });

  // --- Load Full Settings ---
  async function loadSettings() {
    try {
      const cfg = await api('/api/config/full');
      const trading = cfg.trading || {};
      const ai = cfg.decision_ai || {};

      if ($('cfg-engine')) $('cfg-engine').value = trading.engine || 'quant';
      if ($('cfg-tf-mode')) $('cfg-tf-mode').value = trading.timeframe_mode || 'scalp_m5';
      if ($('cfg-exec-type')) $('cfg-exec-type').value = trading.execution_type || 'market';
      if ($('cfg-use-mw')) $('cfg-use-mw').checked = trading.use_market_watch ?? true;
      if ($('cfg-vol-filter')) $('cfg-vol-filter').checked = trading.volume_filter?.enabled ?? true;
      if ($('cfg-session-filter')) $('cfg-session-filter').checked = trading.session_filter?.enabled ?? false;
      if ($('cfg-order-book')) $('cfg-order-book').checked = trading.check_order_book ?? true;

      if ($('cfg-ai-enabled')) $('cfg-ai-enabled').checked = ai.enabled ?? true;
      if ($('cfg-ai-model')) $('cfg-ai-model').value = ai.model || 'gemini-decision-1';
      if ($('cfg-ai-confidence')) $('cfg-ai-confidence').value = ai.min_confidence || 60;
    } catch (e) {
      console.error('Failed to load settings:', e);
    }
  }

  // --- Save Strategy Settings ---
  $('save-strategy-btn')?.addEventListener('click', async () => {
    const btn = $('save-strategy-btn');
    btn.disabled = true;
    notice('');
    try {
      const payload = {
        trading: {
          engine: $('cfg-engine').value,
          timeframe_mode: $('cfg-tf-mode')?.value || 'scalp_m5',
          execution_type: $('cfg-exec-type').value,
          use_market_watch: $('cfg-use-mw').checked,
          check_order_book: $('cfg-order-book').checked,
          volume_filter: { enabled: $('cfg-vol-filter').checked, min_volume_ratio: 0.85 },
          session_filter: { enabled: $('cfg-session-filter').checked, allowed_sessions: ['london', 'new_york'], block_rollover: true }
        }
      };
      await api('/api/config/full', payload);
      notice('تنظیمات استراتژی با موفقیت ذخیره شد.');
    } catch (e) {
      notice(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // --- Save AI Settings ---
  $('save-ai-btn')?.addEventListener('click', async () => {
    const btn = $('save-ai-btn');
    btn.disabled = true;
    notice('');
    try {
      const apiKey = $('cfg-api-key').value.trim();
      const payload = {
        decision_ai: {
          enabled: $('cfg-ai-enabled').checked,
          model: $('cfg-ai-model').value,
          min_confidence: parseInt($('cfg-ai-confidence').value, 10) || 60,
          fallback_on_error: true
        }
      };
      await api('/api/config/full', payload);
      if (apiKey) {
        await api('/api/settings', { api_key: apiKey });
      }
      notice('تنظیمات هوش مصنوعی ذخیره شد.');
      $('decision-ai-badge').textContent = $('cfg-ai-enabled').checked ? 'فعال' : 'غیرفعال';
    } catch (e) {
      notice(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // --- Test Decision AI Connection ---
  const testAiBtn = $('test-ai');
  testAiBtn?.addEventListener('click', async () => {
    const out = $('ai-test-result');
    testAiBtn.disabled = true;
    out.textContent = 'در حال ارسال درخواست آزمایشی به سرور گوگل...';
    try {
      const res = await api('/api/test_ai', { model: $('cfg-ai-model').value });
      out.textContent = `پاسخ موفق دریافت شد! تاخیر: ${res.latency_ms}ms\n${res.content || JSON.stringify(res, null, 2)}`;
    } catch (e) {
      out.textContent = `خطا در برقراری ارتباط: ${e.message}`;
    } finally {
      testAiBtn.disabled = false;
    }
  });

  // --- Start / Stop Trader ---
  async function toggleTrader(action) {
    notice('');
    const btn = action === 'start' ? startBtn : stopBtn;
    if (btn) btn.disabled = true;
    try {
      if (action === 'start') {
        await api('/api/process/start', { mode: 'mt5' });
        notice('فرآیند تریدر با موفقیت روی حساب متاتریدر ۵ شروع به کار کرد.');
      } else {
        await api('/api/process/stop', {});
        notice('فرآیند تریدر متوقف گردید.');
      }
      await refresh();
    } catch (e) {
      const msg = e.message || '';
      if (msg.includes('not reachable') || msg.includes('terminal')) {
        notice('خطا: ترمینال MetaTrader 5 باز نیست! لطفاً نرم‌افزار متاتریدر ۵ را در ویندوز باز کرده و وارد حساب شوید، سپس مجدداً تلاش کنید.', true);
      } else if (msg.includes('NOT a demo')) {
        notice('خطای ایمنی: اکانت متاتریدر دمو نیست! ربات تنها مجاز به فعالیت روی حساب‌های آزمایشی است.', true);
      } else {
        notice(`خطا در اجرای فرآیند تریدر: ${msg}`, true);
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  const startBtn = $('start-agent');
  const stopBtn = $('stop-agent');
  startBtn?.addEventListener('click', () => toggleTrader('start'));
  stopBtn?.addEventListener('click', () => toggleTrader('stop'));

  // --- Render Positions Table ---
  function renderPositions(rows) {
    const root = $('positions');
    if (!root) return;
    root.replaceChildren();
    const countBadge = $('pos-count-badge');
    if (countBadge) countBadge.textContent = `${(rows || []).length} باز`;
    if (!rows || !rows.length) {
      root.appendChild(el('div', 'empty', 'در حال حاضر هیچ پوزیشن بازی وجود ندارد.'));
      return;
    }
    const t = document.createElement('table');
    const thead = document.createElement('thead');
    const headerTr = document.createElement('tr');
    ['تیکت', 'نماد', 'جهت', 'حجم', 'قیمت ورود', 'حد ضرر', 'حد سود', 'سود / زیان'].forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      headerTr.appendChild(th);
    });
    thead.appendChild(headerTr);
    t.appendChild(thead);

    const b = document.createElement('tbody');
    rows.forEach(r => {
      const tr = document.createElement('tr');
      tr.appendChild(el('td', '', String(r.ticket || 'N/A')));
      const tdSym = el('td');
      tdSym.appendChild(el('strong', '', r.symbol));
      tr.appendChild(tdSym);
      const tdDir = el('td');
      tdDir.appendChild(el('span', r.direction === 'BUY' ? 'pill-buy' : 'pill-sell', r.direction || 'N/A'));
      tr.appendChild(tdDir);
      tr.appendChild(el('td', '', `${r.volume} lot`));
      tr.appendChild(el('td', '', String(r.entry_price || 'N/A')));
      tr.appendChild(el('td', '', r.stop_loss ? String(r.stop_loss) : 'N/A'));
      tr.appendChild(el('td', '', r.take_profit ? String(r.take_profit) : 'N/A'));
      const pnl = Number(r.profit_loss || 0);
      const pnlTd = el('td', pnl >= 0 ? 'pill-win' : 'pill-loss', `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)} $`);
      tr.appendChild(pnlTd);
      b.appendChild(tr);
    });
    t.appendChild(b);
    root.appendChild(t);
  }

  function updateBacktestSymbols(symbols) {
    const sel = $('bt-symbol');
    if (!sel || !Array.isArray(symbols) || !symbols.length) return;
    const existing = Array.from(sel.options).map(o => o.value);
    const isSame = existing.length === symbols.length && existing.every((v, i) => v === symbols[i]);
    if (isSame) return;
    const curVal = sel.value;
    sel.replaceChildren();
    symbols.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s;
      opt.textContent = `${s} (واچ‌لیست)`;
      sel.appendChild(opt);
    });
    if (symbols.includes(curVal)) {
      sel.value = curVal;
    }
  }

  function renderBacktestTrades(trades) {
    const card = $('bt-trades-card');
    const view = $('bt-trades-view');
    const badge = $('bt-trades-badge');
    if (!card || !view) return;
    card.hidden = false;
    if (!trades || !trades.length) {
      if (badge) badge.textContent = '0 معامله';
      view.replaceChildren(el('div', 'empty', 'در این بازه تاریخی هیچ ستاپی طبق فیلترهای استراتژی صادر نشد (فیلترهای مومنتوم ADX و عدم ورود در رِنج مانع ورود پرریسک شدند).'));
      return;
    }
    if (badge) badge.textContent = `${trades.length} معامله انجام شد`;
    view.replaceChildren();

    const t = document.createElement('table');
    const thead = document.createElement('thead');
    const headerTr = document.createElement('tr');
    ['#', 'جهت', 'زمان ورود', 'زمان خروج', 'قیمت ورود', 'قیمت خروج', 'حجم', 'سود / زیان', 'نتیجه'].forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      headerTr.appendChild(th);
    });
    thead.appendChild(headerTr);
    t.appendChild(thead);

    const b = document.createElement('tbody');
    trades.slice().reverse().forEach((trData, idx) => {
      const tr = document.createElement('tr');
      tr.appendChild(el('td', '', String(trades.length - idx)));
      const tdDir = el('td');
      tdDir.appendChild(el('span', trData.direction === 'BUY' ? 'pill-buy' : 'pill-sell', trData.direction));
      tr.appendChild(tdDir);
      tr.appendChild(el('td', '', (trData.entry_time || '').replace('T', ' ').substring(5, 16)));
      tr.appendChild(el('td', '', (trData.exit_time || '').replace('T', ' ').substring(5, 16)));
      tr.appendChild(el('td', '', String(trData.entry_price)));
      tr.appendChild(el('td', '', String(trData.exit_price)));
      tr.appendChild(el('td', '', `${trData.lots} lot`));
      const pnl = Number(trData.pnl || 0);
      tr.appendChild(el('td', pnl >= 0 ? 'pill-win' : 'pill-loss', `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)} $`));
      const tdOutcome = el('td');
      tdOutcome.appendChild(el('span', trData.outcome === 'WIN' ? 'pill-win' : 'pill-loss', trData.outcome === 'WIN' ? 'برد' : 'باخت'));
      tr.appendChild(tdOutcome);
      b.appendChild(tr);
    });
    t.appendChild(b);
    view.appendChild(t);
  }

  let activeTradeFilter = 'all';
  let cachedTrades = [];

  function setTradeFilter(filter) {
    activeTradeFilter = filter;
    document.querySelectorAll('.trade-tab-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.filter === filter);
    });
    renderTrades(cachedTrades);
  }

  $('filter-all-btn')?.addEventListener('click', () => setTradeFilter('all'));
  $('filter-crypto-btn')?.addEventListener('click', () => setTradeFilter('crypto'));
  $('filter-tradfi-btn')?.addEventListener('click', () => setTradeFilter('tradfi'));

  function getAssetLabel(sym, rawClass) {
    if (rawClass === 'crypto') return { label: 'CRYPTO', cls: 'crypto' };
    if (rawClass === 'metals') return { label: 'METAL', cls: 'metals' };
    if (rawClass === 'commodities') return { label: 'OIL', cls: 'commodities' };
    if (rawClass === 'indices') return { label: 'INDEX', cls: 'indices' };
    const s = (sym || '').toUpperCase();
    if (s.includes('BTC') || s.includes('ETH') || s.includes('SOL') || s.includes('CRYPTO')) {
      return { label: 'CRYPTO', cls: 'crypto' };
    }
    if (s.includes('XAU') || s.includes('GOLD')) return { label: 'GOLD', cls: 'metals' };
    if (s.includes('BRENT') || s.includes('OIL')) return { label: 'OIL', cls: 'commodities' };
    if (s.includes('US30') || s.includes('DJ')) return { label: 'INDEX', cls: 'indices' };
    return { label: 'FOREX', cls: 'forex' };
  }

  function renderTrades(rows) {
    cachedTrades = rows || [];
    const root = $('trades');
    if (!root) return;

    // Filter by asset class
    const cryptoTrades = cachedTrades.filter(r => (r.asset_class === 'crypto' || /BTC|ETH|SOL|BNB|LTC/i.test(r.symbol || '')));
    const tradfiTrades = cachedTrades.filter(r => !(r.asset_class === 'crypto' || /BTC|ETH|SOL|BNB|LTC/i.test(r.symbol || '')));

    if ($('count-all')) $('count-all').textContent = String(cachedTrades.length);
    if ($('count-crypto')) $('count-crypto').textContent = String(cryptoTrades.length);
    if ($('count-tradfi')) $('count-tradfi').textContent = String(tradfiTrades.length);

    let displayRows = cachedTrades;
    if (activeTradeFilter === 'crypto') displayRows = cryptoTrades;
    else if (activeTradeFilter === 'tradfi') displayRows = tradfiTrades;

    root.replaceChildren();
    if (!displayRows || !displayRows.length) {
      const msg = activeTradeFilter === 'crypto'
        ? 'هنوز معامله‌ای در بازار کریپتو بسته نشده است (با افزودن BTCUSD یا ETHUSD فعال می‌شود).'
        : 'هیچ معامله بسته‌شده‌ای در این دسته‌بندی وجود ندارد.';
      root.appendChild(el('div', 'empty', msg));
      return;
    }

    const t = document.createElement('table');
    const thead = document.createElement('thead');
    const headerTr = document.createElement('tr');
    ['نماد و بازار', 'جهت', 'حجم', 'قیمت ورود', 'قیمت خروج', 'سود / زیان', 'نتیجه'].forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      headerTr.appendChild(th);
    });
    thead.appendChild(headerTr);
    t.appendChild(thead);

    const b = document.createElement('tbody');
    displayRows.slice(0, 40).forEach(r => {
      const pnl = Number(r.profit_loss || 0);
      const pnlClass = pnl >= 0 ? 'pill-win' : 'pill-loss';
      const pnlText = (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + ' $';

      const tr = document.createElement('tr');

      // Symbol + Asset Badge
      const tdSym = el('td');
      const asset = getAssetLabel(r.symbol, r.asset_class);
      const assetPill = el('span', `asset-pill ${asset.cls}`, asset.label);
      tdSym.appendChild(assetPill);
      tdSym.appendChild(el('strong', '', r.symbol));
      tr.appendChild(tdSym);

      // Direction
      const tdDir = el('td');
      tdDir.appendChild(el('span', r.direction === 'BUY' ? 'pill-buy' : 'pill-sell', r.direction || 'N/A'));
      tr.appendChild(tdDir);

      // Volume
      tr.appendChild(el('td', '', `${r.position_size || 0.01} lot`));

      // Entry & Exit
      const entryVal = r.entry != null ? String(r.entry) : (r.entry_price != null ? String(r.entry_price) : 'N/A');
      tr.appendChild(el('td', '', entryVal));
      tr.appendChild(el('td', '', r.exit_price != null ? String(r.exit_price) : 'N/A'));

      // PnL
      tr.appendChild(el('td', pnlClass, pnlText));

      // Outcome Badge
      const tdOutcome = el('td');
      tdOutcome.appendChild(el('span', pnl >= 0 ? 'pill-win' : 'pill-loss', pnl >= 0 ? 'برد' : 'باخت'));
      tr.appendChild(tdOutcome);

      b.appendChild(tr);
    });
    t.appendChild(b);
    root.appendChild(t);
  }

  // --- Main Real-Time Refresh Loop ---
  async function refresh() {
    try {
      // 1. Session & CSRF
      if (!csrf) {
        const sess = await api('/api/session');
        csrf = sess.csrf_token;
      }

      // 2. Overview & Process State Fetch
      const [overview, proc, perf, pos, trades, theses] = await Promise.all([
        api('/api/overview').catch(() => ({})),
        api('/api/process').catch(() => ({ state: 'UNKNOWN' })),
        api('/api/performance').catch(() => ({})),
        api('/api/positions').catch(() => []),
        api('/api/trades').catch(() => []),
        api('/api/thesis').catch(() => ({}))
      ]);

      // Reset network error count on success
      consecutiveNetErrors = 0;

      // Header & Process buttons
      const isRunning = Boolean(proc.running || proc.state === 'RUNNING');
      setTxt('process-state', isRunning ? 'فعال و آنلاین' : 'متوقف');
      setCls('process-state', `kpi-val ${isRunning ? 'good' : 'bad'}`);
      const startEl = $('start-agent');
      const stopEl = $('stop-agent');
      if (startEl) startEl.disabled = isRunning;
      if (stopEl) stopEl.disabled = !isRunning;
      setTxt('connection', isRunning ? 'ترمینال متصل است' : 'آماده به کار');
      setCls('connection', 'telemetry-pill ' + (isRunning ? 'good' : ''));

      // Database Alert check
      const dbAlert = $('db-alert');
      if (dbAlert) {
        if (overview.database_broken) {
          dbAlert.hidden = false;
          dbAlert.textContent = 'پایگاه داده قفل یا معیوب است. برای بازیابی خودکار: python recover_db.py';
        } else {
          dbAlert.hidden = true;
        }
      }

      // Balance & MT5
      const acc = overview.account || {};
      const bal = Number(acc.balance ?? 29.56);
      if (acc.balance != null) {
        setTxt('live-balance', `$${acc.balance.toFixed(2)}`);
        setTxt('live-equity', `Equity: $${acc.equity?.toFixed(2) || acc.balance.toFixed(2)}`);
        setTxt('sidebar-account-desc', `لایو دمو (${acc.server || 'MT5'}) · لاگین: ${acc.login || 'N/A'}`);
      } else {
        setTxt('live-balance', `$${bal.toFixed(2)}`);
        setTxt('live-equity', `Equity: $${bal.toFixed(2)}`);
      }

      // Today PnL & Percentage
      const todayPnl = Number(overview.today_pnl || 0);
      const todayPct = Number(overview.today_pnl_percent || (bal > 0 ? (todayPnl / bal * 100) : 0));
      setTxt('today-pnl', `${todayPnl >= 0 ? '+' : ''}${todayPnl.toFixed(2)} $`);
      setCls('today-pnl', `kpi-val num ${todayPnl >= 0 ? 'good' : 'bad'}`);
      setTxt('today-pnl-pct', `${todayPct >= 0 ? '+' : ''}${todayPct.toFixed(2)}% امروز`);
      setCls('today-pnl-pct', `kpi-sub num ${todayPnl >= 0 ? 'good' : 'bad'}`);

      // Total PnL & Percentage
      const totalPnl = Number(overview.total_pnl || 0);
      const totalPct = Number(overview.total_pnl_percent || (totalPnl / 30.0 * 100));
      setTxt('total-pnl', `${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)} $`);
      setCls('total-pnl', `kpi-val num ${totalPnl >= 0 ? 'good' : 'bad'}`);
      setTxt('total-pnl-pct', `${totalPct >= 0 ? '+' : ''}${totalPct.toFixed(1)}% بازدهی کل`);
      setCls('total-pnl-pct', `kpi-sub num ${totalPnl >= 0 ? 'good' : 'bad'}`);

      setTxt('open-pos', `${(pos || []).length} معامله باز`);

      // KPIs
      const sum = perf.summary || {};
      const totalTrades = Number(sum.total_trades || (trades || []).length);
      const winRate = Number(sum.win_rate || 0);
      setTxt('kpi-winrate', totalTrades ? `${winRate.toFixed(1)}%` : 'N/A');
      setTxt('winrate-badge', `وین‌ریت: ${totalTrades ? winRate.toFixed(0) + '%' : '0%'}`);
      setTxt('kpi-pf', totalTrades && sum.profit_factor != null ? (sum.profit_factor >= 99 ? 'Max' : Number(sum.profit_factor).toFixed(2)) : 'N/A');
      setTxt('kpi-dd', totalTrades && sum.max_drawdown != null ? `$${Number(sum.max_drawdown).toFixed(2)}` : '$0.00');

      // Crypto vs TradFi Asset Breakdown
      const cStats = sum.crypto_stats || {};
      const tStats = sum.tradfi_stats || {};
      const cPnl = Number(cStats.total_pnl || 0);
      const tPnl = Number(tStats.total_pnl != null ? tStats.total_pnl : totalPnl);

      setTxt('crypto-pnl', `${cPnl >= 0 ? '+' : ''}${cPnl.toFixed(2)} $`);
      setCls('crypto-pnl', `deck-value num ${cPnl > 0 ? 'good' : (cPnl < 0 ? 'bad' : 'neutral')}`);
      setTxt('crypto-pct', `${Number(cStats.return_percent || 0) >= 0 ? '+' : ''}${Number(cStats.return_percent || 0).toFixed(1)}% بازدهی`);
      setCls('crypto-pct', `deck-badge num ${cPnl > 0 ? 'good' : (cPnl < 0 ? 'bad' : 'neutral')}`);
      setTxt('crypto-count', String(cStats.trades || 0));
      setTxt('crypto-wl', `${cStats.wins || 0} برد / ${cStats.losses || 0} باخت`);
      setTxt('crypto-winrate', cStats.trades ? `${cStats.win_rate}%` : 'N/A');
      setTxt('crypto-pf', cStats.trades ? `ضریب سود: ${cStats.profit_factor}` : 'ضریب سود: N/A');

      setTxt('tradfi-pnl', `${tPnl >= 0 ? '+' : ''}${tPnl.toFixed(2)} $`);
      setCls('tradfi-pnl', `deck-value num ${tPnl >= 0 ? 'good' : 'bad'}`);
      const retVal = Number(tStats.return_percent != null ? tStats.return_percent : totalPct);
      setTxt('tradfi-pct', `${retVal >= 0 ? '+' : ''}${retVal.toFixed(1)}% بازدهی`);
      setCls('tradfi-pct', `deck-badge num ${tPnl >= 0 ? 'good' : 'bad'}`);
      setTxt('tradfi-count', String(tStats.trades != null ? tStats.trades : totalTrades));
      setTxt('tradfi-wl', `${tStats.wins != null ? tStats.wins : (sum.wins || 0)} برد / ${tStats.losses != null ? tStats.losses : (sum.losses || 0)} باخت`);
      setTxt('tradfi-winrate', tStats.trades ? `${tStats.win_rate}%` : (totalTrades ? `${winRate.toFixed(1)}%` : 'N/A'));
      setTxt('tradfi-pf', tStats.trades ? `ضریب سود: ${tStats.profit_factor}` : `ضریب سود: ${sum.profit_factor || 'N/A'}`);

      // Dynamic backtest symbols sync from active watchlist
      if (theses && Object.keys(theses).length > 0) {
        updateBacktestSymbols(Object.keys(theses));
      }

      // Equity Curve
      const curve = sum.equity_curve || [];
      const hasDrawn = renderSvgCurve('equity-svg', 'equity-area', 'equity-line', curve);
      const chartMsg = $('chart-empty-msg');
      if (chartMsg) chartMsg.hidden = hasDrawn;

      // Positions & Trades
      renderPositions(pos);
      renderTrades(trades);

      // Theses & Market Regimes
      const thBox = $('theses');
      if (thBox) {
        thBox.replaceChildren();
        const thEntries = Object.entries(theses);
        if (!thEntries.length) {
          thBox.appendChild(el('div', 'empty', 'در حال حاضر هیچ نمادی برای تحلیل ثبت نشده است.'));
        } else {
          for (const [sym, t] of thEntries) {
            const d = document.createElement('details');
            const s = document.createElement('summary');
            const p = document.createElement('p');
            s.textContent = `${sym} : ${t.bias || 'WAIT'} : ${t.regime || 'UNCLEAR'}`;
            p.textContent = t.thesis || 'درحال تحلیل ساختار تکنیکال مارکت...';
            d.append(s, p);
            thBox.appendChild(d);
          }
        }
      }

      // Rejections Banner
      const rejections = overview.recent_rejections || [];
      setTxt('rejections-count-badge', `${rejections.length} وتو`);
      const rejBanner = $('rejections-banner');
      if (rejBanner) {
        if (rejections.length > 0) {
          rejBanner.hidden = false;
          rejBanner.replaceChildren();
          const strong = document.createElement('strong');
          strong.textContent = 'آخرین موقعیت‌های وتوشده توسط مدیریت ریسک یا هوش مصنوعی: ';
          rejBanner.appendChild(strong);
          const ul = document.createElement('ul');
          ul.style.margin = '6px 0 0';
          ul.style.paddingRight = '18px';
          rejections.forEach(r => {
            const li = document.createElement('li');
            const timeStr = r.ts ? r.ts.substring(11, 16) : '';
            li.textContent = `${r.symbol} (${r.decision}) در ${timeStr}: ${r.persian_reason || (r.reasons && r.reasons.join(', '))}`;
            ul.appendChild(li);
          });
          rejBanner.appendChild(ul);
        } else {
          rejBanner.hidden = true;
        }
      }

      // Non-Entry & Filter Reasons Grid
      const filterGrid = $('filter-reasons-grid');
      if (filterGrid) {
        filterGrid.replaceChildren();
        const entries = Object.entries(theses || {});
        if (!entries.length) {
          filterGrid.appendChild(el('div', 'empty', 'هنوز تحلیلی برای وضعیت نمادها ثبت نشده است.'));
        } else {
          for (const [sym, t] of entries) {
            const card = el('div', 'filter-item');
            const head = el('div', 'filter-item-head');
            const symTag = el('strong', '', sym);
            
            const cat = t.persian_category || 'پایش بازار';
            let catClass = 'category-tag';
            if (cat.includes('رد شده') || cat.includes('Rollover')) catClass += ' tag-warning';
            else if (cat.includes('سیگنال')) catClass += ' tag-signal';
            const catTag = el('span', catClass, cat);

            head.append(symTag, catTag);

            const pDesc = el('p', '', t.persian_desc || t.thesis || 'در حال پایش دیتای زنده مارکت...');
            const sm = el('small', '', `وضعیت تکنیکال: ${t.regime || 'نامشخص'} : سیگنال: ${t.bias || 'WAIT'}`);

            card.append(head, pDesc, sm);
            filterGrid.appendChild(card);
          }
        }
      }

      // Live Logs & Output View
      const logsEl = $('process-logs');
      if (logsEl) {
        const rawLogs = (proc && Array.isArray(proc.logs) && proc.logs.length) ? proc.logs : [];
        const lines = filterLogs(rawLogs, $('log-filter')?.value);
        if (lines.length) {
          const isAtBottom = logsEl.scrollHeight - logsEl.clientHeight <= logsEl.scrollTop + 50;
          logsEl.textContent = lines.join('\n');
          if (isAtBottom) {
            logsEl.scrollTop = logsEl.scrollHeight;
          }
        } else if (isRunning) {
          logsEl.textContent = 'فرآیند تریدر فعال است و در حال اتصال و اجرای چرخه معاملاتی می‌باشد...';
        } else {
          logsEl.textContent = 'فرآیند تریدر متوقف است. برای شروع، دکمه «شروع تریدر» را در بالای صفحه فشار دهید.';
        }
      }

      setTxt('last-update', `آخرین به‌روزرسانی: ${new Date().toLocaleTimeString('fa-IR')}`);
    } catch (e) {
      console.error('Refresh loop error:', e);
      consecutiveNetErrors++;
      // Only switch status to error after 3 consecutive failures to avoid flickering
      if (consecutiveNetErrors >= 3) {
        setTxt('connection', 'خطا در شبکه');
        setCls('connection', 'telemetry-pill bad');
      }
    }
  }

  // --- Backtester & Weekend Self-Train Handlers ---
  $('run-backtest-btn')?.addEventListener('click', async () => {
    const btn = $('run-backtest-btn');
    btn.disabled = true;
    notice('');
    $('bt-summary-title').textContent = 'در حال اجرای بک‌تست تاریخی و شبیه‌سازی گام‌به‌گام...';
    try {
      const payload = {
        symbol: $('bt-symbol').value,
        count: parseInt($('bt-count').value, 10),
        balance: parseFloat($('bt-balance').value) || 30.0
      };
      const res = await api('/api/backtest/run', payload);
      $('bt-final-bal').textContent = `$${res.final_balance.toFixed(2)}`;
      $('bt-net-pnl').textContent = `${res.net_profit >= 0 ? '+' : ''}$${res.net_profit.toFixed(2)} (${res.return_percent}%)`;
      $('bt-net-pnl').className = `kpi-val num ${res.net_profit >= 0 ? 'good' : 'bad'}`;
      $('bt-winrate').textContent = `${res.win_rate}% (${res.wins} برد / ${res.losses} باخت)`;
      $('bt-trades-count').textContent = `${res.total_trades} ترید`;
      $('bt-pf-badge').textContent = `Profit Factor: ${res.profit_factor}`;
      $('bt-summary-title').textContent = `بک‌تست ${res.symbol} روی ${res.candles_analyzed} کندل پایان یافت (${res.total_trades} معامله انجام شد).`;

      // Render backtest equity curve
      renderSvgCurve('bt-equity-svg', 'bt-area', 'bt-line', res.equity_curve || [], payload.balance);

      // Render detailed trades log table
      renderBacktestTrades(res.trades || []);
    } catch (e) {
      notice(e.message, true);
      $('bt-summary-title').textContent = `خطا در اجرای بک‌تست: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  $('run-train-btn')?.addEventListener('click', async () => {
    const btn = $('run-train-btn');
    btn.disabled = true;
    notice('');
    $('bt-summary-title').textContent = 'در حال آموزش مدل و بهینه‌سازی پارامترها (Walk-Forward Self-Training)...';
    try {
      const payload = {
        symbol: $('bt-symbol').value,
        count: parseInt($('bt-count').value, 10),
        balance: parseFloat($('bt-balance').value) || 30.0
      };
      const res = await api('/api/train/run', payload);
      const finBal = (payload.balance + Number(res.net_profit || 0));
      $('bt-final-bal').textContent = `$${finBal.toFixed(2)}`;
      const retP = ((Number(res.net_profit || 0) / payload.balance) * 100).toFixed(1);
      $('bt-net-pnl').textContent = `${Number(res.net_profit || 0) >= 0 ? '+' : ''}$${Number(res.net_profit || 0).toFixed(2)} (${retP}%)`;
      $('bt-net-pnl').className = `kpi-val num ${Number(res.net_profit || 0) >= 0 ? 'good' : 'bad'}`;
      $('bt-winrate').textContent = `${res.win_rate}%`;
      $('bt-trades-count').textContent = `${res.total_trades} ترید`;
      $('bt-pf-badge').textContent = `Profit Factor: ${res.optimized_profit_factor}`;
      $('bt-summary-title').textContent = `آموزش ${res.symbol} پایان یافت! ضریب سود به ${res.optimized_profit_factor} ارتقا یافت (${res.total_trades} معامله).`;

      // Render lessons learned
      const lBox = $('bt-lessons-box');
      const lList = $('bt-lessons-list');
      if (lBox && lList) {
        lBox.hidden = false;
        lList.replaceChildren();
        (res.lessons || []).forEach(l => {
          const li = document.createElement('li');
          li.textContent = l;
          lList.appendChild(li);
        });
      }

      renderSvgCurve('bt-equity-svg', 'bt-area', 'bt-line', res.equity_curve || [], payload.balance);
      renderBacktestTrades(res.trades || []);
      notice('پارامترهای بهینه‌شده ذخیره شدند و درس‌ها در حافظه سیستم ثبت گردیدند.');
    } catch (e) {
      notice(e.message, true);
      $('bt-summary-title').textContent = `خطا در بهینه‌سازی: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  // --- Log View Tools ---
  $('log-copy')?.addEventListener('click', () => {
    const txt = $('process-logs').textContent;
    navigator.clipboard.writeText(txt).then(() => notice('لاگ‌ها با موفقیت در کلیپ‌بورد کپی شدند.'));
  });
  $('log-clear')?.addEventListener('click', () => {
    $('process-logs').textContent = 'نمای لاگ پاک شد.';
  });
  $('refresh')?.addEventListener('click', refresh);

  // Auto-refresh interval (every 4 seconds)
  refresh();
  setInterval(refresh, 4000);
});
