"""Background advice worker.

Runs model calls off the poll loop on a single thread. When a new decision is
requested it immediately writes a "thinking…" placeholder so the overlay never
shows stale advice, then commits the real advice only if that decision is still
the current one (otherwise it's dropped). Latest request always wins.
"""
from __future__ import annotations

import logging
import threading

from . import thesis as thesis_mod
from . import util


class AdviceWorker:
    def __init__(self, model_client, thesis_store: thesis_mod.ThesisStore,
                 advice_file: str, logger: logging.Logger):
        self.model_client = model_client
        self.thesis = thesis_store
        self.advice_path = util.resolve(advice_file)
        self.advice_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logger

        self._cv = threading.Condition()
        self._job = None          # (signature, payload, alias, kind)
        self._latest_sig = None   # most recently requested signature
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def request(self, signature: str, payload: str, alias: str, kind: str) -> None:
        with self._cv:
            self._job = (signature, payload, alias, kind)
            self._latest_sig = signature
            self._cv.notify()
        self._write(f"[ {kind} ]  thinking…", advice_only=True)

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()

    # ---- internals -------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cv:
                while self._job is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                signature, payload, alias, kind = self._job
                self._job = None

            try:
                response = self.model_client.advise(payload, alias)
            except Exception as exc:  # noqa: BLE001 - surface to overlay, keep loop alive
                self.logger.exception("model call failed")
                if self._is_current(signature):
                    self._write(f"[ {kind} ]  advisor error: {exc}", advice_only=True)
                continue

            if not self._is_current(signature):
                self.logger.info("Dropping stale advice for %s", signature)
                continue

            advice, locked, plan = thesis_mod.parse_response(response)
            self.thesis.update(locked, plan)
            self.thesis.save()
            display = advice if advice else response.strip()
            self._write(f"[ {kind} ]\n{display}", advice_only=True)
            self.logger.info("Advice committed for %s (%s)", kind, alias)

    def _is_current(self, signature: str) -> bool:
        with self._cv:
            return self._latest_sig == signature

    def _write(self, text: str, advice_only: bool = False) -> None:
        try:
            self.advice_path.write_text(text + "\n", encoding="utf-8")
        except OSError:
            self.logger.exception("failed writing advice file")
