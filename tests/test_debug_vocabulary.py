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


def _trace_calls() -> list[tuple[pathlib.Path, ast.Call]]:
    calls: list[tuple[pathlib.Path, ast.Call]] = []
    for path in sorted((ROOT / "canvas").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("dbg", "dbg2"):
                calls.append((path, node))
    return calls


def _events_in_code() -> set[str]:
    found: set[str] = set()
    for _, node in _trace_calls():
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


def test_every_trace_call_names_its_event_with_a_literal():
    """A computed name would slip past both checks above: it is never in the
    code scan, so nothing would notice that it is undocumented."""
    computed = [
        f"{path.name}:{node.lineno}"
        for path, node in _trace_calls()
        if not (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        )
    ]

    assert not computed, f"trace event names must be string literals: {computed}"
