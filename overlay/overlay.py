"""Always-on-top advice overlay.

Tails the latest-advice file and shows it in a small, semi-transparent window.
Auto-shows when the bridge's heartbeat file is fresh and hides when it goes
stale (i.e. the game/advisor isn't running). Standalone — run separately:

    python overlay/overlay.py
"""
from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (ROOT / path)


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class Overlay:
    def __init__(self, cfg: dict):
        oc = cfg["overlay"]
        self.advice_path = _resolve(cfg["paths"]["advice_file"])
        self.heartbeat_path = _resolve(cfg["paths"]["heartbeat_file"])
        self.poll_ms = int(oc["poll_interval_s"] * 1000)
        self.heartbeat_stale_s = float(oc["heartbeat_stale_s"])

        self.root = tk.Tk()
        self.root.title("STS2 A10 本地顾问")
        self.root.configure(bg="#11131a")
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", float(oc.get("opacity", 0.92)))
        except tk.TclError:
            pass
        self.root.overrideredirect(True)  # frameless

        w, h = int(oc["width"]), int(oc["height"])
        x = oc.get("x")
        y = oc.get("y")
        if x is None or y is None:
            sw = self.root.winfo_screenwidth()
            x = sw - w - 24
            y = 60
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

        bar = tk.Frame(self.root, bg="#1d2230", height=20)
        bar.pack(fill="x", side="top")
        tk.Label(bar, text="  STS2 A10 本地顾问", bg="#1d2230", fg="#7fd1ff",
                 font=(oc["font_family"], 9, "bold")).pack(side="left")
        close = tk.Label(bar, text="✕  ", bg="#1d2230", fg="#888",
                         font=(oc["font_family"], 9))
        close.pack(side="right")
        close.bind("<Button-1>", lambda _event: self.root.destroy())
        bar.bind("<Button-1>", self._start_move)
        bar.bind("<B1-Motion>", self._on_move)

        self.text = tk.Text(
            self.root, bg="#11131a", fg="#e6e6e6", wrap="word",
            font=(oc["font_family"], int(oc["font_size"])),
            borderwidth=0, highlightthickness=0, padx=10, pady=8)
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", "Waiting for game…")
        self.text.configure(state="disabled")

        self._last_mtime = 0.0
        self._visible = True
        self._ever_seen = False   # have we ever seen a fresh heartbeat?
        self._drag = (0, 0)

    def _start_move(self, e):
        self._drag = (e.x, e.y)

    def _on_move(self, e):
        x = self.root.winfo_x() + e.x - self._drag[0]
        y = self.root.winfo_y() + e.y - self._drag[1]
        self.root.geometry(f"+{x}+{y}")

    def _heartbeat_fresh(self) -> bool:
        try:
            age = time.time() - self.heartbeat_path.stat().st_mtime
            return age <= self.heartbeat_stale_s
        except OSError:
            return False

    def _set_text(self, content: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")

    def _tick(self) -> None:
        fresh = self._heartbeat_fresh()
        if fresh:
            self._ever_seen = True

        # Stay visible while the advisor is live (fresh) OR on a cold start before
        # we've ever seen it (so a manually-launched overlay shows a "waiting" hint
        # instead of being invisible). Hide only once it was live and then went
        # stale — i.e. the game/advisor actually closed.
        should_show = fresh or not self._ever_seen
        if should_show and not self._visible:
            self.root.deiconify()
            self._visible = True
        elif not should_show and self._visible:
            self.root.withdraw()
            self._visible = False

        if fresh:
            try:
                mtime = self.advice_path.stat().st_mtime
                if mtime != self._last_mtime:
                    self._last_mtime = mtime
                    self._set_text(self.advice_path.read_text(encoding="utf-8"))
            except OSError:
                pass
        elif not self._ever_seen:
            self._set_text("STS2 A10 本地顾问\n\n正在等待《杀戮尖塔2》和 "
                           "STS2MCP…\n\n进入可决策界面后，这里会显示中文建议。")

        self.root.after(self.poll_ms, self._tick)

    def run(self) -> None:
        self.root.after(self.poll_ms, self._tick)
        self.root.mainloop()


if __name__ == "__main__":
    Overlay(load_config()).run()
