# Local control panel

Run `python panel.py` (or `start-panel.cmd` on Windows). Panel binds to
127.0.0.1 only, debug/reloader off, and opens your browser. No trading
starts on launch; use **Start** in the panel (simulation or MT5 demo
only). Use **Stop** before changing settings.

## Windows setup

1. Install Python 3.10+ from https://python.org ("Add python.exe to PATH").
2. In the project folder run `start-panel.cmd` — it creates `.venv`
   automatically the first time and installs `requirements.txt`.

## Notes

- Settings are saved to `.env` atomically; your existing keys are
  preserved (a blank API-key field keeps the stored key).
- The AI test/models buttons call your provider directly and never save
  the entered key. Logs bound to 200 lines; debug views to 100 rows.
- Process start refuses at ≥85% cgroup memory pressure.
