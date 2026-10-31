"""The trace vocabulary in the code and in `docs/debugging.md` must agree.

`AGENTS.md` lists "a new event belongs in the doc" as a known trap, which means
it is the kind of thing that gets forgotten. This makes forgetting fail the gate
instead. It compares names only: the prose stays hand-written, because a
generated list reads worse than the one there is.
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "debugging.md"


def _events_in_code() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "canvas").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in ("dbg", "dbg2"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return found


def _events_in_doc() -> set[str]:
    # Backticked all-caps identifiers. `CANVAS_DEBUG=1` and `<seconds>` do not
    # match because of the `=` and the angle brackets.
    return set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", DOC.read_text()))


def test_every_trace_event_is_documented():
    missing = _events_in_code() - _events_in_doc()

    assert not missing, f"add to docs/debugging.md: {sorted(missing)}"


def test_the_docs_do_not_name_events_that_do_not_exist():
    ghosts = _events_in_doc() - _events_in_code()

    assert not ghosts, f"docs name events the code never emits: {sorted(ghosts)}"
