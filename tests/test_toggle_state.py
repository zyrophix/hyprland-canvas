"""The state file is read before anything else and trusted to be ours.

Every value in it is written by the daemon, but the file lives in
/run/user/<uid> and is plain JSON, so a hand edit, a truncated write or a
merge conflict can leave anything in it. The parsers therefore have to decide
what a value means from its shape — never from the file saying so.
"""

import json

import pytest

from canvas import toggle_state


def _write(tmp_path, payload):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload))
    return str(path)


V4_WS = {
    "active": True,
    "tiled": {"0x1": {"at": [0, 0], "size": [935, 493]}},
    "floating": {"0x1": {"at": [10, 20], "size": [800, 600]}},
    "pre_floating": ["0x9"],
    "spawn_rules": ["canvas-spawn-ws4-default"],
}


def test_v4_payload_survives_a_round_trip(tmp_path):
    state = toggle_state.load(_write(tmp_path, {"_v": 4, "4": V4_WS}))

    assert state[4]["floating"] == {"0x1": {"at": [10, 20], "size": [800, 600]}}
    assert state[4]["pre_floating"] == ["0x9"]
    assert state[4]["active"] is True


@pytest.mark.parametrize(
    "version_field", [{}, {"_v": "4"}, {"_v": None}, {"_v": -1}, {"_v": True}, {"_v": []}]
)
def test_the_format_is_decided_by_shape_not_by_the_version_field(tmp_path, version_field):
    """A bad `_v` must not route a v4 file into the oldest parser.

    That parser reads the section names as if they were window addresses and
    drops the geometry, so the canvas came back to the wrong place and the next
    save wrote five junk addresses into the file. `_v` is a field we write
    ourselves; the shape of a workspace entry is not something to take on
    trust from it.
    """
    state = toggle_state.load(_write(tmp_path, {**version_field, "4": V4_WS}))

    assert state[4]["floating"] == {"0x1": {"at": [10, 20], "size": [800, 600]}}
    assert state[4]["pre_floating"] == ["0x9"]
    assert state[4]["tiled"] == {"0x1": {"at": [0, 0], "size": [935, 493]}}
    assert set(state[4]["tiled"]) == {"0x1"}, "section names leaked in as addresses"


def test_a_bare_list_is_still_read_as_the_oldest_format(tmp_path):
    """Shape detection must not swallow the format it is meant to keep."""
    state = toggle_state.load(_write(tmp_path, {"_v": 1, "2": ["0x1", "0x2"]}))

    assert state[2]["tiled"] == {"0x1": {}, "0x2": {}}
    assert state[2]["floating"] == {}
    assert state[2]["active"] is True


def test_v2_infers_active_from_a_non_empty_tiled_snapshot(tmp_path):
    """v2 had no active bit; the snapshot was the only signal there was."""
    state = toggle_state.load(
        _write(tmp_path, {"_v": 2, "1": {"tiled": {"0x1": {"at": [0, 0], "size": [1, 1]}}}})
    )

    assert state[1]["active"] is True


def test_v3_explicit_active_false_survives_a_non_empty_snapshot(tmp_path):
    """The v4 bit exists to say "tiled boxes saved, canvas is off"."""
    state = toggle_state.load(
        _write(
            tmp_path,
            {
                "_v": 3,
                "1": {
                    "active": False,
                    "tiled": {"0x1": {"at": [0, 0], "size": [1, 1]}},
                    "floating": {},
                },
            },
        )
    )

    assert state[1]["active"] is False


def test_a_newer_version_is_read_as_ours_but_warns(tmp_path):
    state = toggle_state.load(_write(tmp_path, {"_v": 99, "1": V4_WS}))

    assert state[1]["floating"] == {"0x1": {"at": [10, 20], "size": [800, 600]}}


def test_geometry_that_cannot_be_parsed_degrades_to_empty_rather_than_raising(tmp_path):
    """`_parse_snapshot` emits {} for these, and the geometry code relies on it."""
    state = toggle_state.load(
        _write(
            tmp_path,
            {
                "_v": 4,
                "1": {
                    "active": True,
                    "tiled": {"0x1": {}},
                    "floating": {
                        "0x1": {"at": [1], "size": [2, 3]},
                        "0x2": {"at": ["x", 2], "size": [4, 5]},
                        "0x3": "not a dict",
                    },
                },
            },
        )
    )

    assert state[1]["floating"] == {"0x1": {}, "0x2": {}, "0x3": {}}
