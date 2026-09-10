# AI Autonomous Forex Trader

Autonomous AI-powered Forex trading system for MetaTrader 5.

> **V1: Demo-only.** No live money. Architecture supports BACKTEST → PAPER → DEMO → LIVE (LIVE disabled).

## Architecture

```
Market Data (MT5) → Technical Engine + News Engine + Calendar
                          ↓
                    Market Context
                          ↓
                     AI Trading Agent  ← Trading Constitution
                          ↓
                      Risk Engine (deterministic, can't be overridden)
                          ↓
                    Execution Engine → MT5
                          ↓
                    Trade Journal → Post-Trade Review → AI Memory
```

## Quick Start

```bash
# Clone
git clone https://github.com/Aiagentali/ai-trader.git
cd ai-trader

# Install deps
pip install -r requirements.txt

# Set up env
cp .env.example .env
# Edit .env with your AI_API_KEY

# Run (simulation mode, no MT5 needed)
python main.py
```

## Configuration

| File | Purpose |
|------|---------|
| `app/config/settings.yaml` | Trading mode, AI limits, timeframes |
| `app/config/symbols.yaml` | Currency pairs to trade |
| `app/config/risk.yaml` | Risk parameters |
| `.env` | Secrets (API keys, MT5 credentials) |

## Risk Engine

The AI proposes. The Risk Engine validates. The AI **cannot** override:
- Max risk per trade (0.5%)
- Max daily loss (2%)
- Max open positions (5)
- Correlated exposure limits
- Spread validation
- High-impact news windows
- Consecutive loss circuit breaker
- Daily loss limit → all trading halted

## Trading Constitution

Hard rules embedded in every AI prompt:
1. Every trade needs a stop loss
2. No martingale, no revenge trading
3. Every decision must be explainable
4. Missing data → WAIT
5. AI cannot override safety limits
6. All decisions logged

## Token Budget

Daily hard cap: 200,000 tokens. Enforced programmatically.

```
Hourly analysis:  120K
News analysis:     25K
Trade decisions:   20K
Position monitor:  15K
Emergency reserve:  20K
```

## Development Phases

- [x] Phase 1: Foundation (config, DB, MT5 feed, logging)
- [x] Phase 2: Technical Engine (indicators, snapshot)
- [x] Phase 3: News Engine (RSS, dedup, calendar)
- [x] Phase 4: AI Engine (provider, prompts, schemas, token budget)
- [x] Phase 5: Autonomous Loop (state machine, hourly analysis)
- [x] Phase 6: Risk Engine (sizing, limits, correlation, news)
- [x] Phase 7: Simulation Execution
- [ ] Phase 8: MT5 Demo Execution
- [ ] Phase 9: Trade Management
- [ ] Phase 10: Trade Review
- [ ] Phase 11: Backtesting
- [ ] Phase 12: Dashboard

## License

Private — All rights reserved.
