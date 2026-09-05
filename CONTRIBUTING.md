# Contributing to Spire Oracle

Thanks for helping out! This is a small, read-only advisor for Slay the Spire 2.

## Dev setup

```bash
python -m pip install -r requirements.txt
python data/fetch_data.py            # download the card/relic dataset locally
python -m bridge.model_client --selftest   # validate the Claude Code CLI
```

`requirements.txt` is intentionally the small runtime layer. Training and
NumPy/PyTorch/Gymnasium tests are optional: use the adjacent emulator checkout's
`.venv`, or install `requirements-training.txt` into a separate Python 3.11+
environment. Set `STS2_TRAINING_PYTHON` to that interpreter before running
`scripts\test.ps1`; the script defaults to
`..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe` and
checks all five training imports before starting those tests. The `sts2_gym`
package is local source from that checkout, not a PyPI dependency.

For one-environment full discovery instead:

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests
```

The native-emulator integration tests skip when the adjacent checkout or its
native DLL is unavailable.

Run against the live game (StS2 + the [STS2MCP](https://github.com/Gennadiyev/STS2MCP)
mod):

```bash
python -m bridge.main      # the bridge/advisor
python overlay/overlay.py  # the overlay window
```

Or test the pipeline without the game: set `dev.use_sample_state: true` in
`config.yaml` and run `python -m bridge.main` (feeds `data/sample_state.json`).
Set `dev.log_prompts: true` to log the exact prompt sent to the model.

## Project shape

- `bridge/` — poll loop, screen classification, grounding, model client, deck/run
  tracking. Start at `bridge/main.py`.
- `prompts/` — the strategist persona + per-screen playbooks (edit these to tune
  advice).
- `overlay/`, `autostart/`, `setup.ps1`, `*.sh` — UI and platform glue.

## Ground rules

- **Observe-only.** Never add a code path that sends input/actions to the game.
  `bridge/client.py` is GET-only on purpose — keep it that way.
- The STS2MCP JSON shape is read in exactly one place per screen:
  `bridge/screens.py::extract_options`. If a game/mod patch changes a field, fix it
  there (and update `data/sample_state.json`).
- Don't commit game data: `data/cards.json`, `data/relics.json`, and `runtime/` are
  gitignored. Don't redistribute Mega Crit's content.

## Reporting bugs

Open an issue with: what screen you were on, what advice appeared (or didn't), and a
snippet of `runtime/advisor.log`. If it's a data-shape issue, the relevant
`/api/v1/singleplayer` JSON helps a lot.

## PRs

Keep changes focused. Quick self-check before opening a PR:

```bash
python -c "import py_compile,glob; [py_compile.compile(f,doraise=True) for f in glob.glob('bridge/*.py')]"
```
