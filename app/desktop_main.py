"""Entry point for the packaged desktop build: runs the same Flask app used
by the website, but in a background thread inside this process, and opens
it in a native window via pywebview instead of a system browser tab. Not
used by the web deployment (app.py's own __main__ block covers that) --
this is only what PyInstaller points at when building the desktop app."""

import logging
import socket
import threading

import webview

from app import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _free_port():
    # Bind to port 0 to let the OS hand back an unused one -- avoids
    # colliding with anything else the user has running on a fixed port
    # like 5000.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    port = _free_port()
    server_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    webview.create_window(
        "STEM-Access", f"http://127.0.0.1:{port}/upload", width=1000, height=800, min_size=(600, 500)
    )
    webview.start()


if __name__ == "__main__":
    main()
