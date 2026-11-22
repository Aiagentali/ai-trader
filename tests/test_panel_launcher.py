import os
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_panel_launcher_serves_loopback_only():
    port = 8977
    process = subprocess.Popen([sys.executable, "panel.py", "--port", str(port)],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 15
        token = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session", timeout=1) as resp:
                    token = json.load(resp)["csrf_token"]
                break
            except OSError:
                time.sleep(0.1)
        assert token, "panel did not become ready"
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/settings",
                                             headers={"Host": "example.com"})
            urllib.request.urlopen(request, timeout=2)
            raise AssertionError("non-loopback Host header accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        try:
            ip = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                                timeout=5).stdout.split()
            if ip:
                urllib.request.urlopen(f"http://{ip[0].strip()}:{port}/api/session", timeout=2)
                raise AssertionError("panel reachable on non-loopback address")
        except (OSError, subprocess.SubprocessError):
            pass  # expected: non-loopback address refuses connection
    finally:
        process.terminate()
        process.wait(timeout=10)
