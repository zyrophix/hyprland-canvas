"""Property tests for the parts where examples are not enough.

Everything here is pure logic: no compositor, no sockets, no time. These are the
places where a wrong answer is silent — a state file parsed as the wrong shape, a
size that passes validation and then reaches Lua, an ordering that changes
between runs — and where a generator covers the input space far better than a
list of cases would.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from canvas.navigation import Navigator
from canvas.panning import PanningState
from canvas.spawnrules import SpawnRuleError, parse_size, size_spec_error
from canvas.toggle_state import _parse_snapshot, _parse_workspace

WORKAREA = (0, 44, 1920, 1036)

ADDRESS = st.text(alphabet="0123456789abcdef", min_size=1, max_size=8).map(lambda h: "0x" + h)

GEOMETRY = st.fixed_dictionaries(
    {
        "at": st.lists(st.integers(-10000, 10000), min_size=2, max_size=2),
        "size": st.lists(st.integers(-10000, 10000), min_size=2, max_size=2),
    }
)


# --- toggle state migration -------------------------------------------------


@given(
    st.one_of(
        st.none(),
        st.lists(st.text(max_size=12)),
        st.dictionaries(st.text(max_size=12), st.text(max_size=12)),
        st.dictionaries(ADDRESS, st.one_of(GEOMETRY, st.text(max_size=4), st.none())),
        st.dictionaries(
            st.sampled_from(["active", "tiled", "floating", "pre_floating", "spawn_rules"]),
            st.one_of(st.booleans(), st.text(max_size=4), st.lists(st.text(max_size=4))),
        ),
    )
)
def test_parse_workspace_survives_any_shape(value):
    """The format is decided by shape, so every shape has to come out usable."""
    parsed = _parse_workspace(value)

    assert set(parsed) == {"active", "tiled", "floating", "pre_floating", "spawn_rules"}
    assert isinstance(parsed["active"], bool)
    assert isinstance(parsed["tiled"], dict)
    assert isinstance(parsed["floating"], dict)
    assert isinstance(parsed["pre_floating"], list)
    assert isinstance(parsed["spawn_rules"], list)


@given(st.dictionaries(ADDRESS, GEOMETRY, min_size=1))
def test_a_current_snapshot_round_trips(snapshot):
    """v4 geometry is authoritative, so reading it back must change nothing."""
    assert _parse_snapshot(snapshot) == snapshot


@given(st.lists(ADDRESS, min_size=1))
def test_the_legacy_list_form_keeps_addresses_and_drops_geometry(addresses):
    """Old entries described tiled slots; replaying them as floating positions
    is exactly what used to move windows the layout had placed."""
    parsed = _parse_snapshot(addresses)

    assert set(parsed) == set(addresses)
    assert all(geometry == {} for geometry in parsed.values())


# --- size specs -------------------------------------------------------------


@given(st.text(max_size=14))
def test_parse_size_agrees_with_size_spec_error(spec):
    """Config validation asks size_spec_error; registration then calls
    parse_size. They must agree on what is a size, or a typo reaches Lua."""
    problem = size_spec_error(spec)

    if problem is not None:
        with pytest.raises(SpawnRuleError):
            parse_size(spec, WORKAREA)
        return

    width, height = parse_size(spec, WORKAREA)
    assert 1 <= width <= WORKAREA[2]
    assert 1 <= height <= WORKAREA[3]


# --- tiling order -----------------------------------------------------------


@given(st.dictionaries(ADDRESS, st.one_of(GEOMETRY, st.just({})), min_size=1))
def test_toggle_order_is_a_stable_permutation(snapshot):
    """The order is what makes a toggle sequence reproducible instead of
    dependent on the snapshot's iteration order."""
    first = Navigator._toggle_order(snapshot)

    assert sorted(first) == sorted(snapshot)
    assert len(first) == len(set(first))
    assert Navigator._toggle_order(snapshot) == first


# --- pan delta --------------------------------------------------------------


@given(
    st.lists(
        st.tuples(st.integers(-100_000, 100_000), st.integers(-100_000, 100_000)),
        min_size=2,
        max_size=50,
    ),
)
def test_pan_delta_carries_no_accumulated_error(positions):
    """The whole reason the total is kept instead of per-frame steps: the result
    is the cursor's own displacement, to the pixel, whatever path it took."""
    state = PanningState()
    state.start_pan()
    for x, y in positions:
        state.update_cursor(x, y)

    dx, dy = state.get_total_delta()

    assert dx == -(positions[-1][0] - positions[0][0])
    assert dy == -(positions[-1][1] - positions[0][1])
