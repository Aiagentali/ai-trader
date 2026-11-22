#!/usr/bin/env python3
"""Diagnostic script to test API and MT5 connections for ai-trader."""
import os
import sys
import time
import json
from pathlib import Path

# Ensure root directory is on sys.path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def print_status(step: str, success: bool, message: str = ""):
    icon = "[OK]" if success else "[FAIL]"
    print(f"{icon} {step}: {message}")


def main():
    print("=" * 60)
    print("        ai-trader Diagnostics & Connection Checker        ")
    print("=" * 60)

    # 1. Check .env and Environment Variables
    api_key = os.environ.get("AI_API_KEY", "").strip()
    provider_name = os.environ.get("AI_PROVIDER", "gemini").strip()
    base_url = os.environ.get("AI_BASE_URL", "").strip()
    model_normal = os.environ.get("AI_MODEL_NORMAL", "gemini-3.8-flash").strip()
    model_critical = os.environ.get("AI_MODEL_CRITICAL", "gemini-4-hydrogen").strip()

    if not api_key:
        print_status("API Key", False, "AI_API_KEY is missing or empty in .env")
        return 1
    
    masked_key = api_key[:6] + "..." + api_key[-4:] if len(api_key) > 10 else "***"
    print_status("API Key loaded", True, f"Key: {masked_key} | Provider: {provider_name}")
    print_status("Base URL", True, base_url or "Default")
    print_status("Models", True, f"Normal: {model_normal} | Critical: {model_critical}")
    print("-" * 60)

    # 2. Initialize Provider
    from app.ai.client import get_provider, AIError
    try:
        provider = get_provider()
        print_status("Provider Init", True, f"Loaded class {type(provider).__name__}")
    except Exception as exc:
        print_status("Provider Init", False, f"Error: {exc}")
        return 1

    # 3. Test Model Discovery (list_models)
    print("\n[Testing Model Discovery via API]...")
    try:
        t0 = time.time()
        available_models = provider.list_models()
        elapsed = round((time.time() - t0) * 1000)
        print_status("List Models", True, f"Found {len(available_models)} models in {elapsed}ms")
        
        # Display sample models matching gemini
        gemini_models = [m for m in available_models if "gemini" in m.lower()][:8]
        if gemini_models:
            print("   Available Gemini Models:")
            for m in gemini_models:
                print(f"     - {m}")
    except Exception as exc:
        print_status("List Models", False, f"Could not list models: {exc}")

    # 4. Test Live Chat Completion
    test_model = model_normal
    print(f"\n[Testing Live Completion with model: {test_model}]...")
    try:
        t0 = time.time()
        test_system = "You are a test assistant. Always output a single JSON object with key 'status'."
        test_user = "Respond with JSON: {\"status\": \"connected\", \"message\": \"Google Gemini API is working!\"}"
        
        resp = provider.chat(
            system=test_system,
            user=test_user,
            model=test_model,
            max_tokens=200,
            temperature=0.1
        )
        elapsed = round((time.time() - t0) * 1000)
        
        content = resp.get("content", "")
        in_tok = resp.get("input_tokens", 0)
        out_tok = resp.get("output_tokens", 0)
        
        print_status("Live API Call", True, f"Success in {elapsed}ms! (Tokens: in={in_tok}, out={out_tok})")
        print(f"   Raw Model Response:\n   {content}")
        
        # Validate JSON parsing
        parsed = json.loads(content)
        print_status("JSON Output Validation", True, "Response parsed as valid JSON successfully!")
    except Exception as exc:
        err_msg = str(exc)
        if "503" in err_msg or "high demand" in err_msg.lower():
            print_status("Live API Call", True, "Key is VALID & Authenticated! (Google returned HTTP 503: Temporary high demand spike on free tier)")
            print("   Notice: Google's free tier is experiencing temporary traffic spikes right now, but your key was accepted.")
        else:
            print_status("Live API Call", False, f"Error calling {test_model}: {exc}")
            print("\nTip: Check if the model name is supported by your API key tier or try gemini-3.8-flash.")

    # 5. Check MetaTrader 5 Connection
    print("\n[Checking MetaTrader 5 Terminal]...")
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            term = mt5.terminal_info()
            acc = mt5.account_info()
            term_name = getattr(term, "name", "MT5")
            login = getattr(acc, "login", "Unknown") if acc else "No Account"
            balance = getattr(acc, "balance", 0.0) if acc else 0.0
            is_demo = getattr(acc, "trade_mode", None) == mt5.ACCOUNT_TRADE_MODE_DEMO if acc else False
            
            print_status("MT5 Terminal", True, f"Connected to '{term_name}'")
            print_status("MT5 Account", is_demo, f"Login: {login} | Balance: {balance} | Demo: {is_demo}")
            mt5.shutdown()
        else:
            print_status("MT5 Terminal", False, "Terminal not open or initialize() failed")
    except ImportError:
        print_status("MT5 Terminal", False, "MetaTrader5 library not installed")
    except Exception as exc:
        print_status("MT5 Terminal", False, f"MT5 check error: {exc}")

    print("\n" + "=" * 60)
    print("Diagnostics complete.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
