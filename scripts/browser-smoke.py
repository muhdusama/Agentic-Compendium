"""Run the browser suite only against an isolated, temporary local database."""

import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="compendium-browser-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        password = secrets.token_hex(24)
        env = {**os.environ, "APP_HOST": "127.0.0.1", "APP_PORT": str(port),
               "APP_DB_PATH": str(Path(directory) / "test.sqlite3"),
               "APP_PUBLIC_ORIGIN": base, "APP_OWNER_PASSWORD": password,
               "APP_READ_TOKEN": secrets.token_hex(24),
               "APP_PUBLISH_TOKEN": secrets.token_hex(24),
               "APP_HERMES_ALLOWED_ORIGINS": "", "APP01_TEST_BASE": base,
               "APP01_TEST_PASSWORD": password, "APP01_TEST_DISPOSABLE": "1"}
        with open(Path(directory) / "server.log", "w+") as log:
            server = subprocess.Popen([sys.executable, "-m", "server.app", "serve"],
                                      cwd=root, env=env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError("Disposable test server stopped")
                    try:
                        with urlopen(base + "/healthz", timeout=1) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError("Disposable test server did not become ready")
                # The full smoke changes the owner password, so run it last.
                for suite in ("server/tests/service-worker.cjs", "server/tests/browser-notifications.cjs", "server/tests/browser-smoke.cjs"):
                    result = subprocess.run(["node", suite], cwd=root, env=env, timeout=240)
                    if result.returncode:
                        return result.returncode
                return 0
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
