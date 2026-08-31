"""Claude Code CLI model layer.

Runs `claude` as a subprocess to get terse text advice on the user's Claude Code
subscription (no API key). Prefers ONE warm, long-lived process per model (pays
agent startup once) using stream-json I/O; if that turns out not to persist
across turns on this CLI version, it transparently falls back to spawning a
fresh process per call. Tools are hard-disabled so the model can only emit text.

Two things are version-dependent — run `python -m bridge.model_client --selftest`
to validate them on your machine:
  * whether `--input-format stream-json` keeps the process alive across turns
  * the exact stdin user-message JSON shape (config: model.cli.input_message_format)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time

from . import util

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# Env markers Claude Code sets for its own session. We MUST strip these from the
# subprocess env or the spawned `claude` refuses to start ("Claude Code cannot be
# launched inside another Claude Code session"). The advisor always spawns a
# clean, standalone CLI, so dropping these is correct in every case.
_SCRUB_ENV_KEYS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENABLE_TASKS",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_AGENT_SDK_VERSION",
)


def _clean_env(base_env: dict) -> dict:
    env = dict(base_env)
    for key in list(env):
        if key in _SCRUB_ENV_KEYS or key.startswith("CLAUDE_CODE_"):
            env.pop(key, None)
    return env


class WarmUnsupported(Exception):
    """Raised when the warm session won't accept a follow-up turn."""


def _build_system_prompt(prompts_dir: str) -> str:
    base = util.resolve(prompts_dir)
    parts = []
    for fname in ("system_prompt.md", "screen_playbooks.md"):
        p = base / fname
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def _encode_user_message(text: str, fmt: str) -> str:
    if fmt == "simple":
        obj = {"type": "user", "message": text}
    else:  # "sdk" canonical
        obj = {"type": "user", "message": {"role": "user", "content": text}}
    return json.dumps(obj)


def _extract_result(line_obj: dict) -> str | None:
    """Return the final text if this event is a turn-complete result."""
    if line_obj.get("type") == "result":
        return line_obj.get("result", "")
    return None


class _WarmSession:
    """One long-lived `claude` process for a single model alias."""

    def __init__(self, cmd: list[str], env: dict, logger: logging.Logger):
        self.logger = logger
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            bufsize=1, env=env, creationflags=_CREATE_NO_WINDOW,
        )
        self.turns = 0
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                self._q.put(line)
        finally:
            self._q.put(None)  # EOF sentinel

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, payload: str, fmt: str, timeout: float) -> str:
        if not self.alive():
            raise WarmUnsupported("warm process is not running")
        try:
            self.proc.stdin.write(_encode_user_message(payload, fmt) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WarmUnsupported(f"stdin write failed: {exc}") from exc

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("model response timed out")
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("model response timed out")
            if line is None:
                # process exited mid/after turn -> warm path unusable
                raise WarmUnsupported("process exited before returning a result")
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            result = _extract_result(obj)
            if result is not None:
                self.turns += 1
                return result

    def close(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
        except OSError:
            pass


class ModelClient:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.logger = logger
        self.model_cfg = cfg["model"]
        cli = self.model_cfg.get("cli", {})
        self.executable = util.resolve_executable(cli.get("executable", "claude"))
        self.base_flags = list(cli.get("base_flags", ["-p", "--output-format", "stream-json", "--verbose"]))
        self.warm_flags = list(cli.get("warm_flags", ["--input-format", "stream-json"]))
        self.input_fmt = cli.get("input_message_format", "sdk")
        self.timeout = float(cli.get("response_timeout_s", 45.0))
        self.warm_enabled = bool(cli.get("warm_session", True))
        self.recycle_after = int(self.model_cfg.get("recycle_after_turns", 25))
        mode = cli.get("system_prompt_mode", "append")
        self._sys_flag = "--system-prompt" if mode == "replace" else "--append-system-prompt"
        self.system_prompt = _build_system_prompt(cfg["paths"]["prompts_dir"])

        self.env = _clean_env(os.environ)
        for k, v in (cli.get("env") or {}).items():
            self.env[str(k)] = str(v)

        self._sessions: dict[str, _WarmSession] = {}
        self._lock = threading.Lock()

    # --- model routing ----------------------------------------------------
    def model_for(self, screen_kind: str) -> str:
        return self.model_cfg.get("per_screen", {}).get(
            screen_kind, self.model_cfg.get("default", "sonnet"))

    # --- command construction --------------------------------------------
    def _sys_flags(self) -> list[str]:
        return [self._sys_flag, self.system_prompt] if self.system_prompt else []

    def _warm_cmd(self, alias: str) -> list[str]:
        return ([self.executable] + self.base_flags + self.warm_flags
                + ["--model", alias] + self._sys_flags())

    def _oneshot_cmd(self, alias: str) -> list[str]:
        return ([self.executable] + self.base_flags
                + ["--model", alias] + self._sys_flags())

    # --- public API -------------------------------------------------------
    def advise(self, payload: str, alias: str) -> str:
        if self.warm_enabled:
            try:
                return self._warm_advise(payload, alias)
            except WarmUnsupported as exc:
                self.logger.warning("Warm session unsupported (%s); using one-shot mode.", exc)
                self.warm_enabled = False
                self._close_all()
            except TimeoutError as exc:
                self.logger.warning("Warm session timed out (%s); recycling.", exc)
                self._close_session(alias)
        return self._oneshot(payload, alias)

    def _warm_advise(self, payload: str, alias: str) -> str:
        with self._lock:
            sess = self._sessions.get(alias)
            if sess and sess.turns >= self.recycle_after:
                self.logger.info("Recycling %s session after %d turns.", alias, sess.turns)
                sess.close()
                sess = None
            if sess is None or not sess.alive():
                sess = _WarmSession(self._warm_cmd(alias), self.env, self.logger)
                self._sessions[alias] = sess
        try:
            return sess.send(payload, self.input_fmt, self.timeout)
        except WarmUnsupported:
            self._close_session(alias)
            raise

    def _oneshot(self, payload: str, alias: str) -> str:
        try:
            proc = subprocess.run(
                self._oneshot_cmd(alias), input=payload, capture_output=True,
                text=True, encoding="utf-8", timeout=self.timeout, env=self.env,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return "PICK: (model timed out)\nWHY: no response in time\nRISK: -"
        text = proc.stdout or ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            result = _extract_result(obj)
            if result is not None:
                return result
        # not stream-json? return whatever came back
        if text.strip():
            return text.strip()
        return f"PICK: (no model output)\nWHY: {(proc.stderr or '').strip()[:160]}\nRISK: -"

    def reset_sessions(self) -> None:
        """Recycle all warm sessions (e.g. on a new run) to clear context."""
        self._close_all()

    def _close_session(self, alias: str) -> None:
        with self._lock:
            sess = self._sessions.pop(alias, None)
        if sess:
            sess.close()

    def _close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.close()

    def shutdown(self) -> None:
        self._close_all()


# ---------------------------------------------------------------------------
# Self-test: validate the CLI invocation, warm-session persistence, and the
# stdin message schema on THIS machine.
# ---------------------------------------------------------------------------
def _selftest() -> int:
    cfg = util.load_config()
    logger = util.setup_logging(cfg["paths"]["log_file"])
    mc = ModelClient(cfg, logger)
    alias = mc.model_cfg.get("default", "sonnet")
    print(f"Executable: {mc.executable}")
    print(f"Model alias: {alias}")
    print(f"Warm cmd: {' '.join(mc._warm_cmd(alias)[:-1])} <system-prompt:{len(mc.system_prompt)} chars>")
    print(f"input_message_format: {mc.input_fmt}\n")

    probe = ("SCREEN KIND: selftest\nReply with EXACTLY this and nothing else:\n"
             "PICK: OK\nWHY: selftest\nRISK: -\n<<<LOCKED\n- selftest\n>>>\n<<<PLAN\nok\n>>>")

    print("[1/3] Trying WARM session, turn 1 ...")
    try:
        sess = _WarmSession(mc._warm_cmd(alias), mc.env, logger)
        r1 = sess.send(probe, mc.input_fmt, mc.timeout)
        print("  turn 1 OK. First 120 chars:", repr(r1[:120]))
        print("[2/3] Trying WARM session, turn 2 (persistence) ...")
        try:
            r2 = sess.send(probe, mc.input_fmt, mc.timeout)
            print("  turn 2 OK -> warm session PERSISTS. First 120:", repr(r2[:120]))
            print("\nRESULT: warm_session works. Keep model.cli.warm_session: true")
        except WarmUnsupported:
            print("  turn 2 failed -> process exits after one turn.")
            print("\nRESULT: warm NOT persistent on this version. The advisor will")
            print("        auto-fall back to one-shot mode (works, pays startup each call).")
        finally:
            sess.close()
    except (WarmUnsupported, TimeoutError, OSError) as exc:
        print(f"  warm turn 1 failed: {exc}")
        print("[3/3] Trying ONE-SHOT mode ...")
        try:
            r = mc._oneshot(probe, alias)
            print("  one-shot OK. First 120 chars:", repr(r[:120]))
            print("\nRESULT: use one-shot mode (set model.cli.warm_session: false).")
        except Exception as exc2:  # noqa: BLE001 - diagnostic path
            print(f"  one-shot also failed: {exc2}")
            print("\nRESULT: CLI invocation is wrong. Check `claude --version`, that")
            print("        `claude` is on PATH, and the flags in config.yaml model.cli.")
            return 1
    finally:
        mc.shutdown()
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("Run with --selftest to validate the Claude Code CLI invocation.")
