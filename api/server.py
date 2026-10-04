"""Purpose: optional stoppable localhost debug API for the Termux daemon. Dependencies: Flask + Werkzeug."""

from __future__ import annotations

import threading

from flask import Flask, jsonify
from werkzeug.serving import make_server


class LocalApiServer:
    def __init__(self, get_status, host: str = "127.0.0.1", port: int = 8787) -> None:
        self.app = Flask(__name__)
        self._server = None
        self._thread: threading.Thread | None = None
        self._get_status = get_status
        self.host = host
        self.port = port

        @self.app.get("/health")
        def health():
            return jsonify({"ok": True})

        @self.app.get("/status")
        def status():
            return jsonify(self._get_status())

    def start(self) -> None:
        self._server = make_server(self.host, self.port, self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, name="local-debug-api", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)


def create_local_app(get_status):
    """Compatibility factory returning the Flask application used by LocalApiServer."""
    return LocalApiServer(get_status).app

