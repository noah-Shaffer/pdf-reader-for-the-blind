"""Entry point for the packaged desktop build: runs the same Flask app used
by the website, but in a background thread inside this process, and opens
it in a native window via pywebview instead of a system browser tab. Not
used by the web deployment (app.py's own __main__ block covers that) --
this is only what PyInstaller points at when building the desktop app."""

import logging
import os
import shutil
import socket
import sys
import threading

if sys.platform == "win32":
    # Pin qtpy to PyQt5 before webview (or qtpy itself) gets imported --
    # left unset, qtpy probes for whichever Qt binding is importable at
    # runtime, which is one more failure mode to rule out in a frozen build
    # that only actually bundles PyQt5.
    os.environ.setdefault("QT_API", "pyqt5")

import webview

from app import UPLOAD_DIR, _JOB_ID_RE, app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Api:
    """Exposed to the page as window.pywebview.api. A plain HTML <a
    download> link (what the web app's result.html normally uses) has
    nothing to trigger inside pywebview's embedded webview -- unlike a real
    browser, it has no download-handling UI of its own, so clicking it does
    nothing visible at all. result.html detects it's running inside the
    desktop app (window.pywebview only exists there) and calls this instead,
    which shows a real native save dialog."""

    window = None

    def save_file(self, job_id, filename):
        if not _JOB_ID_RE.match(job_id):
            return {"error": "invalid job id"}
        src = os.path.join(UPLOAD_DIR, job_id, filename)
        if not os.path.isfile(src):
            return {"error": "file not found"}

        result = self.window.create_file_dialog(webview.SAVE_DIALOG, save_filename=filename)
        if not result:
            return {"cancelled": True}
        dest = result[0] if isinstance(result, (list, tuple)) else result
        shutil.copyfile(src, dest)
        return {"saved": dest}


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

    api = Api()
    window = webview.create_window(
        "STEM-Access",
        f"http://127.0.0.1:{port}/upload",
        width=1000,
        height=800,
        min_size=(600, 500),
        js_api=api,
    )
    api.window = window
    # Windows only: pywebview's other backends (winforms/mshtml/edgechromium)
    # all render through pythonnet's .NET Framework CLR hosting, which has a
    # long-standing, often-unfixable PyInstaller incompatibility -- frozen
    # builds intermittently fail with "Failed to resolve
    # Python.Runtime.Loader.Initialize" trying to load Python.Runtime.dll
    # (see https://github.com/r0x0r/pywebview/issues/1215, unresolved even
    # upstream). The Qt backend doesn't touch .NET/pythonnet at all, so it
    # sidesteps the bug entirely. macOS (Cocoa) and Linux (GTK) backends
    # aren't affected, so leave them on pywebview's own default.
    webview.start(gui="qt" if sys.platform == "win32" else None)


if __name__ == "__main__":
    main()
