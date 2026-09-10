# AI Autonomous Forex Trader

سیستم ترید خودکار هوش مصنوعی برای MetaTrader 5 — فقط روی حساب دمو.

---

## ⚡ Quick Start (3 دقیقه)

### 1. Clone
```bash
git clone https://github.com/Aiagentali/ai-trader.git
cd ai-trader
```

### 2. Install
```bash
pip install -r requirements.txt
```
اگه روی ویندوز با MT5 هستی:
```bash
pip install MetaTrader5
```

### 3. Config
```bash
cp .env.example .env
```
فایل `.env` رو باز کن و مقدارها رو پر کن:
```
AI_API_KEY=sk-or-v1-xxxxxxxxxxxx
AI_BASE_URL=https://openrouter.ai/api/v1
MT5_LOGIN=12345678
MT5_PASSWORD=your_password
MT5_SERVER=YourBroker-Demo
```

### 4. Run
```bash
# حالت شبیه‌سازی (بدون MT5 — همه جا کار می‌کنه)
python main.py

# حالت دمو (با MT5 وصل — فقط ویندوز)
# اول settings.yaml → execution.mode: demo
python main.py
```

### 5. Dashboard
```bash
python -m app.dashboard.server
# باز کن: http://localhost:5000
```

---

## 🧪 تست‌ها

```bash
# همه تست‌ها
python -m pytest tests/ -v

# فقط Phase 1-7
python -m pytest tests/test_all.py -v

# فقط Phase 8-11
python -m pytest tests/test_phase8_11.py -v

# فقط Dashboard
python -m pytest tests/test_dashboard.py -v
```

**104 تست — همه باید pass باشن.**

---

## 📁 ساختار پروژه

```
ai-trader/
│
├── app/
│   ├── agent/loop.py              ← حلقه خودمختار اصلی
│   ├── ai/
│   │   ├── client.py              ← ارائه‌دهنده AI (OpenRouter/OpenAI)
│   │   ├── prompts.py             ← پرامپت + قانون اساسی 20 ماده‌ای
│   │   ├── schemas.py             ← اعتبارسنجی JSON خروجی AI
│   │   └── token_budget.py        ← سقف روزانه 200K توکن
│   ├── backtest/engine.py         ← موتور بک‌تست بدون look-ahead
│   ├── config/
│   │   ├── settings.yaml          ← تنظیمات کلی
│   │   ├── symbols.yaml           ← جفت‌ارزها
│   │   └── risk.yaml              ← پارامترهای ریسک
│   ├── database/
│   │   ├── models.py              ← 12 جدول SQLite
│   │   └── repository.py          ← تمام SQL در یک لایه
│   ├── dashboard/
│   │   ├── server.py              ← Flask dashboard
│   │   ├── templates/index.html   ← رابط کاربری تاریک
│   │   └── static/style.css       ← تم Linear/Stripe
│   ├── execution/
│   │   ├── engine.py              ← Simulation executor
│   │   ├── mt5_demo.py            ← اجرای واقعی MT5 demo
│   │   └── trade_manager.py       ← مدیریت پوزیشن
│   ├── fundamentals/calendar.py   ← تقویم اقتصادی
│   ├── journal/performance.py     ← آمار عملکرد
│   ├── market/
│   │   ├── indicators.py          ← SMA, EMA, RSI, ATR, ADX
│   │   ├── mt5.py                 ← MockFeed + MT5 real
│   │   └── snapshot.py            ← snapshot چند تایم‌فریم
│   ├── news/rss.py                ← خبرخوان + dedup
│   └── risk/engine.py             ← موتور ریسک تعیین‌شده
│
├── tests/
│   ├── test_all.py                ← 59 تست Phase 1-7
│   ├── test_phase8_11.py          ← 29 تست Phase 8-11
│   └── test_dashboard.py          ← 16 تست Dashboard
│
├── main.py                        ← نقطه شروع
├── requirements.txt
├── .env.example
└── README.md
```

---

## ⚙️ تنظیمات

### `app/config/settings.yaml`
```yaml
trading:
  mode: simulation          # simulation | demo | live (live غیرفعال)
  live_trading_enabled: false

symbols:
  - EURUSD
  - GBPUSD
  - USDJPY
  - USDCHF
  - AUDUSD
  - USDCAD

analysis:
  primary_timeframes: [D1, H4, H1, M15]
  hourly_analysis: true
  normal_interval_minutes: 60
  watch_interval_minutes: 10

ai:
  daily_token_limit: 200000

execution:
  mode: simulation          # simulation | demo
```

### `app/config/risk.yaml`
```yaml
risk:
  max_risk_per_trade: 0.005     # 0.5%
  max_daily_loss: 0.02          # 2%
  max_open_positions: 5
  max_correlated_exposure: 2
  min_rr: 1.5
  max_spread_multiplier: 3.0
  max_consecutive_losses: 4
```

---

## 🏗️ معماری

```
Market Data (MT5)
       ↓
┌──────────────────────────────────────────┐
│  Technical Engine  +  News  +  Calendar  │
└──────────────────┬───────────────────────┘
                   ↓
            Market Context
                   ↓
          AI Trading Agent  ← Trading Constitution
                   ↓
            Risk Engine (تعیین‌شده — AI نمی‌تونه override کنه)
                   ↓
          Execution Engine → MT5
                   ↓
          Trade Journal → Post-Trade Review → AI Memory
```

---

## 🔒 Trading Constitution

20 قانون ثابت که در هر درخواست AI جاسازی شده:

1. هر ترید باید stop loss داشته باشه
2. حداکثر ریسک حساب رعایت بشه
3. بعد از ضرر ریسک زیاد نشه
4. Martingale ممنوع
5. Revenge trading ممنوع
6. پوزیشن‌های همبسته بیش از حد ممنوع
7. فقط با یک اندیکاتور ترید نکن
8. هر ترید باید thesis مشخص داشته باشه
9. هر ترید باید invalidation condition داشته باشه
10. Risk/Reward مشخص باشه
11. AI نمی‌تونه Risk Engine رو override کنه
12. AI نمی‌تونه safety limits رو غیرفعال کنه
13. AI نمی‌تونه live trading رو فعال کنه
14. همه تریدها log بشه
15. همه تصمیمات قابل توضیح باشن
16. اگه داده ناقصه → WAIT
17. اگه regime نامشخصه → WAIT
18. اگه spread غیرعادیه → ورود نکن
19. قبل از خبرهای پرتأثیر → ترید جدید ممنوع
20. سقف ضرر روزانه اجرا بشه

---

## 📊 Dashboard

```bash
python -m app.dashboard.server
# http://localhost:5000
```

نمایش میده:
- Account balance / equity / PnL
- Win rate / Profit factor / Drawdown
- Token budget usage (پیشرفت بار)
- AI thesis هر جفت‌ارز
- پوزیشن‌های باز
- تریدهای اخیر
- PnL روزانه
- اخبار + تقویم اقتصادی
- **Auto-refresh هر 30 ثانیه**

---

## 🔄 فازها

| فاز | وضعیت | توضیح |
|-----|--------|-------|
| 1 | ✅ | Foundation: config, DB, logging |
| 2 | ✅ | Technical Engine: indicators, snapshot |
| 3 | ✅ | News Engine: RSS, dedup, calendar |
| 4 | ✅ | AI Engine: provider, schemas, token budget |
| 5 | ✅ | Autonomous Loop: state machine |
| 6 | ✅ | Risk Engine: 15+ بررسی تعیین‌شده |
| 7 | ✅ | Simulation Execution |
| 8 | ✅ | MT5 Demo Execution |
| 9 | ✅ | Trade Management: MODIFY/PARTIAL/CLOSE |
| 10 | ✅ | Performance Metrics + Trade Review |
| 11 | ✅ | Backtesting Engine |
| 12 | ✅ | Dashboard: Flask + Dark UI |

---

## 🧪 تست‌های اختصاصی

### MT5 Demo Safety
```python
# MT5DemoExecutor فقط با live_trading_enabled=false کار می‌کنه
MT5DemoExecutor(repo, feed, live_trading_enabled=True)  # → RuntimeError
```

### Risk Engine Rejects
```python
# بدون stop loss → REJECT
risk.validate({"decision": "BUY", "stop_loss": None, ...})  # → approved=False

# RR پایین → REJECT
risk.validate({"decision": "BUY", "stop_loss": 1.082, "take_profit": [1.083], ...})
# → approved=False ("RR 0.33 < min 1.5")

# پوزیشن‌های همبسته → REJECT
# BUY EURUSD + BUY GBPUSD → هر دو USD exposure
```

### Token Budget
```python
# سقف 200K/روز — CRITICAL می‌تونه از emergency reserve استفاده کنه
budget.check_and_allow("normal", "NORMAL")  # False بعد از 90%
budget.check_and_allow("critical", "CRITICAL")  # True تا 100%
```

### Backtest
```python
from app.backtest.engine import BacktestEngine
engine = BacktestEngine({"H1": candle_data})
result = engine.run(decision_fn=my_strategy)
# → {"total_trades": 15, "win_rate": 60.0, "profit_factor": 1.8, ...}
```

---

## 🛡️ امنیت

- کلیدها در `.env` ذخیره میشن (هرگز commit نمیشن)
- `.env.example` بدون credentials
- API keys هرگز log نمیشن
- Live trading **غیرفعال** — سیستم اجازه نمیده

---

## 📝 نکات مهم

- **فقط دمو** — V1 هرگز با پول واقعی ترید نمی‌کنه
- **AI تصمیم می‌گیره — سیستم اجرا می‌کنه** — AI نمی‌تونه safety limits رو دور بزنه
- **بیشترین اکشن: WAIT** — سیستم مجبور نیست هر ساعت ترید کنه
- **هر ترید لاگ میشه** — هیچ ضرری حذف نمیشه
- **بک‌تست بدون look-ahead** — فقط داده‌های گذشته در دسترسه
- **addle loss** — بعد از ضرر متوالی، ترید متوقف میشه
