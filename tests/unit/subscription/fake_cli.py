"""A scripted stand-in for the official CLI, used to drive the real service.

It is a real executable the service spawns exactly as it spawns the official
binary. It records what it was started with (argv, cwd, environment *names*,
prompt length) next to itself and then plays the scenario in ``scenario.json``.
It never records environment values.
"""

from __future__ import annotations

import json
import stat
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

SCRIPT = """#!{python}
import hashlib, json, os, pathlib, sys, time

here = pathlib.Path(__file__).parent
scenario = json.loads((here / "scenario.json").read_text())
args = sys.argv[1:]
prompt = sys.stdin.read()
settings = json.loads(pathlib.Path(args[args.index("--settings") + 1]).read_text())
record = {{
    "pid": os.getpid(),
    "argv": args,
    "cwd": os.getcwd(),
    "env_names": sorted(os.environ),
    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    "prompt_len": len(prompt),
    "mcp_config": (
        json.loads(pathlib.Path(args[args.index("--mcp-config") + 1]).read_text())
        if "--mcp-config" in args else None
    ),
    "settings": settings,
}}
(here / f"record-{{os.getpid()}}.json").write_text(json.dumps(record))


def emit(event):
    print(json.dumps(event), flush=True)


def say(text):
    block = {{"type": "text", "text": text}}
    emit({{"type": "assistant", "message": {{"content": [block]}}}})


def log_span(kind):
    with open(here / "spans.log", "a") as handle:
        handle.write(f"{{kind}} {{time.monotonic()}}\\n")


mode = scenario.get("mode", "success")
tools = scenario.get("tools", settings["permissions"]["allow"])
emit({{"type": "system", "subtype": "init", "tools": tools}})
if mode == "success":
    say("hello")
    emit({{"type": "result", "subtype": "success", "is_error": False,
           "result": "done:" + str(len(prompt)),
           "usage": {{"input_tokens": 3, "output_tokens": 4}}}})
elif mode == "stderr_secret":
    sys.stderr.write("SYNTHETIC-STDERR-SECRET-5521\\n")
    sys.stderr.flush()
    emit({{"type": "result", "subtype": "success", "is_error": False, "result": "ok",
           "usage": {{}}}})
elif mode == "slow":
    log_span("start")
    time.sleep(scenario.get("seconds", 0.3))
    log_span("end")
    emit({{"type": "result", "subtype": "success", "is_error": False, "result": "ok",
           "usage": {{}}}})
elif mode == "sleep":
    time.sleep(600)
elif mode == "auth_error":
    emit({{"type": "assistant", "error": "authentication_failed",
           "message": {{"content": [{{"type": "text", "text": "Not logged in"}}]}}}})
    emit({{"type": "result", "subtype": "success", "is_error": True,
           "result": "Not logged in · Please run /login", "usage": {{}}}})
elif mode == "rate_limit":
    emit({{"type": "assistant", "error": "rate_limit",
           "message": {{"content": [{{"type": "text", "text": "limit"}}]}}}})
    emit({{"type": "result", "subtype": "success", "is_error": True,
           "result": "limit reached", "usage": {{}}}})
elif mode == "many_turns":
    for _ in range(100):
        say("t")
    time.sleep(600)
elif mode == "no_result":
    say("half")
elif mode == "crash":
    sys.exit(3)
"""


def install_fake_cli(directory: Path, scenario: dict[str, Any] | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "claude"
    script.write_text(SCRIPT.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    set_scenario(directory, scenario or {})
    return script


def set_scenario(directory: Path, scenario: dict[str, Any]) -> None:
    (directory / "scenario.json").write_text(json.dumps(scenario))


def records(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text()) for path in sorted(directory.glob("record-*.json"))
    ]
