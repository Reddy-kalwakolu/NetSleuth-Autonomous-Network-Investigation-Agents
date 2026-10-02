import pytest

from netsleuth.storage import DuckDBSession
from netsleuth.tools import TOOLS, ToolError, call_tool, make_ref, rerun


def test_every_result_can_be_rerun_to_the_same_answer(after: DuckDBSession) -> None:
    first = call_tool(after, "get_device", {"device_id": "node-hub1-04"})

    again = rerun(after, first.query_ref)

    assert again == first
    assert first.query_ref == make_ref("get_device", {"device_id": "node-hub1-04"})


def test_unknown_tool_is_refused(after: DuckDBSession) -> None:
    with pytest.raises(ToolError, match="unknown tool"):
        call_tool(after, "apply_action", {})


def test_bad_arguments_are_refused(after: DuckDBSession) -> None:
    with pytest.raises(ToolError, match="get_device"):
        call_tool(after, "get_device", {"device": "node-hub1-04"})


def test_no_tool_takes_a_time_or_run_argument() -> None:
    import inspect

    for name, fn in TOOLS.items():
        params = set(inspect.signature(fn).parameters) - {"session"}
        assert not params & {"as_of", "run_id", "ts", "time"}, name
