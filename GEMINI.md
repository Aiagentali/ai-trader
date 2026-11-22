# ai-trader Development & Quantitative Rules

When working on this repository, strictly adhere to the following principles:

## 1. Mathematical & Risk Invariants
- **Expectancy Over Win Rate:** Never optimize solely for win rate. Evaluate all strategies and changes by Expected Value ($E$), Profit Factor, and Maximum Drawdown.
- **Minimum Risk-to-Reward (R:R):** Maintain a minimum R:R of 1:1.5 (`min_rr: 1.5`).
- **Position Sizing:** Position size must remain dynamically calculated from stop-loss distance and risk percentage (0.5% max per trade).
- **Hard Boundaries:** Never modify code to weaken or bypass the deterministic `RiskEngine` (`app/risk/engine.py`).

## 2. Trading Constitution Compliance
Always preserve the 20 immutable rules from `app/ai/prompts.py`:
- Mandatory Stop Loss on every order.
- Zero tolerance for Martingale or revenge trading.
- News event blackout windows must remain enforced.
- When market regime is ambiguous or data is missing, the default output must be `WAIT`.

## 3. Credential & Data Security
- Never hardcode or commit API tokens, MT5 passwords, or credentials into repository files or git configuration.
- Keep all credentials strictly isolated in `.env` (ignored by git).
