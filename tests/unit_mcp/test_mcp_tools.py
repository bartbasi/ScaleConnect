"""The MCP tools, through the SDK's own in memory client, against fake scales on localhost.

Made up items only. Each test reads what the fake scale received, not just what the tool said.
"""

from __future__ import annotations

import json
import threading

import anyio
import pytest
from mcp import Client
from mcp_types import Implementation

from scaleconnect.catalog import Catalog
from scaleconnect.mcp_server import STATUS_MEANINGS, _caller_name, _change_payload, _is_full_success, build_server
from scaleconnect.models import ChangeRecord, ItemResult, ScaleResult, ScaleStatus
from scaleconnect.service import ScaleConnectService

READ_TOOLS = {
    "scale_list_scales",
    "scale_search_items",
    "scale_get_item",
    "scale_compare_scales",
    "scale_read_items",
    "scale_preview_changes",
    "scale_get_change",
    "scale_preview_settle_drift",
    "scale_preview_mark_orphan",
}
LOCAL_READ_TOOLS = {"scale_list_scales", "scale_search_items", "scale_get_item", "scale_get_change"}
WRITE_TOOLS = {"scale_apply_changes", "scale_resolve_interrupted_change", "scale_mark_orphan"}


def tools_of(server, **client_kw):
    async def go():
        async with Client(server, **client_kw) as client:
            return (await client.list_tools()).tools

    return anyio.run(go)


class Session:
    """One server over one service. Every call goes through a real SDK client."""

    def __init__(self, env, **server_kw):
        self.env = env
        self.server = build_server(env.svc, **server_kw)

    def call(self, name, client_kw=None, **arguments):
        async def go():
            async with Client(self.server, **(client_kw or {})) as client:
                return await client.call_tool(name, arguments)

        return anyio.run(go)

    def data(self, name, **arguments):
        """Calls a tool, checks text and structured results agree, and returns the data."""
        result = self.call(name, **arguments)
        assert result.structured_content is not None, result.content
        assert result.content[0].text == result.structured_content["summary"]
        assert json.loads(result.content[1].text) == result.structured_content
        return result.structured_content

    def preview(self, changes, **arguments):
        data = self.data("scale_preview_changes", changes=changes, **arguments)
        assert data["ok"], data["summary"]
        return data

    def apply(self, preview_id, **arguments):
        return self.call("scale_apply_changes", preview_id=preview_id, **arguments)


def fields_of(record):
    return list(record)


def held_price(fake, item_id):
    return fake.item(item_id)[4]


def make_drift(fake, item_id, price):
    record = fields_of(fake.item(item_id))
    record[4] = price
    fake.preload(tuple(record))


EDIT_1001 = [{"action": "edit", "target_plu": "1001", "price": "12.00"}]


# ---------- the tool list ----------


def test_lists_every_tool(env):
    names = {tool.name for tool in tools_of(build_server(env.svc))}
    assert names == READ_TOOLS | WRITE_TOOLS


def test_only_tools_that_contact_no_scale_are_marked_read_only_and_writes_destructive(env):
    tools = {tool.name: tool for tool in tools_of(build_server(env.svc))}
    for name in READ_TOOLS:
        assert tools[name].annotations.destructive_hint is False, name
        assert tools[name].annotations.read_only_hint is (name in LOCAL_READ_TOOLS), name
        assert tools[name].annotations.idempotent_hint is (name in LOCAL_READ_TOOLS), name
    for name in WRITE_TOOLS:
        assert tools[name].annotations.read_only_hint is False, name
        assert tools[name].annotations.destructive_hint is True, name


def test_only_the_apply_tool_takes_a_preview_id_and_no_tool_takes_a_raw_record(env):
    tools = {tool.name: tool for tool in tools_of(build_server(env.svc))}
    apply_schema = tools["scale_apply_changes"].input_schema
    assert apply_schema["required"] == ["preview_id"]
    assert set(apply_schema["properties"]) == {"preview_id", "acknowledge_warnings", "acknowledge_drift"}
    change = tools["scale_preview_changes"].input_schema["$defs"]["ChangeInput"]["properties"]
    assert not set(change) & {"id", "field6", "fields_7_12", "fields_14_20", "timestamp"}


def test_descriptions_explain_the_statuses_for_a_reader_with_no_other_context(env):
    tools = {tool.name: tool for tool in tools_of(build_server(env.svc))}
    text = tools["scale_apply_changes"].description
    for status in ScaleStatus:
        assert status.value in text, status
    assert "accepted_not_verified: the scale said Ok but could not be read back. This is NOT confirmed." in text
    assert "success is true only if every configured scale is verified" in text
    assert "scale_get_change" in text
    for tool in tools.values():
        assert len(tool.description) > 100, tool.name


def test_tool_text_follows_the_writing_rule(env):
    server = build_server(env.svc)
    text = [server.instructions, *STATUS_MEANINGS.values()]
    for tool in tools_of(server):
        text.extend([tool.description, tool.title, json.dumps(tool.input_schema, ensure_ascii=False)])
    for piece in text:
        assert "—" not in piece and "–" not in piece and "--" not in piece, piece[:80]


# ---------- reads ----------


def test_list_scales_says_what_it_knows_and_holds_no_address(make_env):
    env = make_env({"A": True, "B": False})
    data = Session(env).data("scale_list_scales")
    assert {s["name"]: s["write_enabled"] for s in data["scales"]} == {"A": True, "B": False}
    assert data["capabilities"]["delete"] is True
    assert "Not available" not in data["summary"]
    dumped = json.dumps(data)
    assert "127.0.0.1" not in dumped and "TEST-A" not in dumped
    assert env.silent("A", "B")


def test_search_and_get_item_read_the_catalog_and_contact_no_scale(env):
    session = Session(env)
    found = session.data("scale_search_items", query="1002")
    assert [i["plu"] for i in found["items"]] == ["1002"]
    assert "not proof" in found["summary"]
    assert session.data("scale_search_items", query="zzz")["count"] == 0
    item = session.data("scale_get_item", plu="1001")
    assert item["item"]["price"] == "10.00" and item["source"] == "catalog"
    assert "A: nothing recorded" in item["summary"]
    assert env.silent("A")


def test_get_item_refuses_a_missing_item_and_both_targets(env):
    session = Session(env)
    missing = session.call("scale_get_item", plu="9999")
    assert missing.is_error and missing.structured_content["error"]["code"] == "item_not_found"
    both = session.call("scale_get_item", plu="1001", id=1)
    assert both.is_error and both.structured_content["error"]["code"] == "validation_failed"


# ---------- add and edit, all the way to the scale ----------


def test_add_then_edit_reach_the_scale_and_are_verified(env):
    session = Session(env)
    fake = env.fakes["A"]

    preview = session.preview([{"action": "add", "plu": "4001", "description": "Example Cheese", "price": "12.50"}])
    assert preview["scales_read"] == ["A"] and preview["drift"] == []
    assert fake.item(4) is None, "a preview must send no record"
    assert not fake.messages_of("GP")
    assert "Add PLU 4001 (id 4): price 12.50, sold by weight." in preview["summary"]
    assert "Example Cheese" not in preview["summary"]
    assert preview["differences"][0]["after"]["description"] == "Example Cheese"

    added = session.apply(preview["preview_id"])
    data = added.structured_content
    assert not added.is_error
    assert data["success"] is True and data["outcome"] == "success"
    assert data["scale_statuses"] == {"A": "verified"}
    assert data["summary"].startswith("SUCCESS.")
    assert fake.item(4)[1:5] == ("4001", "4001", "Example Cheese", "12.50")

    preview = session.preview([{"action": "edit", "target_plu": "4001", "price": "13.00"}])
    assert preview["differences"][0]["fields"] == {"price": {"before": "12.50", "after": "13.00"}}
    assert held_price(fake, 4) == "12.50"
    edited = session.apply(preview["preview_id"]).structured_content
    assert edited["success"] is True and edited["scale_statuses"] == {"A": "verified"}
    assert held_price(fake, 4) == "13.00"
    assert env.catalog.get_by_id(4).price == "13.00"


def test_success_needs_every_scale_verified_on_all_three(env3):
    session = Session(env3)
    preview = session.preview(EDIT_1001)
    data = session.apply(preview["preview_id"]).structured_content
    assert data["success"] is True
    assert data["scale_statuses"] == {"A": "verified", "B": "verified", "C": "verified"}
    assert all(held_price(fake, 1) == "12.00" for fake in env3.fakes.values())


def test_a_batch_of_changes_is_one_preview_and_one_apply(env):
    session = Session(env)
    changes = [
        {"action": "edit", "target_plu": "1001", "price": "11.00"},
        {"action": "edit", "target_plu": "1002", "price": "21.00"},
        {"action": "add", "plu": "4001", "description": "New One", "price": "1.25", "sold_by": "piece"},
    ]
    preview = session.preview(changes)
    assert len(preview["differences"]) == 3
    data = session.apply(preview["preview_id"]).structured_content
    assert data["success"] is True
    assert data["items_total"] == 3 and data["items_verified_everywhere"] == 3
    assert data["batch_partly_failed"] is False
    fake = env.fakes["A"]
    assert held_price(fake, 1) == "11.00" and held_price(fake, 2) == "21.00" and fake.item(4)[3] == "New One"
    assert len(fake.messages_of("GP")) == 1, "the batch goes out in one GP message"


def test_an_edit_keeps_every_other_field_of_the_record(env):
    session = Session(env)
    before = env.fakes["A"].item(3)
    preview = session.preview([{"action": "edit", "target_plu": "1003", "price": "5.75"}])
    session.apply(preview["preview_id"])
    after = env.fakes["A"].item(3)
    changed = [i for i, (old, new) in enumerate(zip(before, after)) if old != new]
    assert changed == [4, 12], "only the price and timestamp may change"


# ---------- nothing changes without a preview ----------


def test_apply_with_a_made_up_preview_id_sends_nothing(env):
    result = Session(env).apply("0" * 32)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "preview_not_found"
    assert result.structured_content["nothing_sent"] is True
    assert env.silent("A")


def test_apply_needs_a_preview_id_at_all(env):
    result = Session(env).call("scale_apply_changes")
    assert result.is_error
    assert env.silent("A")


def test_a_preview_applies_once(env):
    session = Session(env)
    preview = session.preview(EDIT_1001)
    assert session.apply(preview["preview_id"]).structured_content["success"] is True
    writes = len(env.fakes["A"].messages_of("GP"))
    again = session.apply(preview["preview_id"])
    assert again.is_error and again.structured_content["error"]["code"] == "preview_already_applied"
    assert "scale_get_change" in again.structured_content["next_step"]
    assert len(env.fakes["A"].messages_of("GP")) == writes


def test_a_catalog_change_makes_an_open_preview_stale(env):
    session = Session(env)
    first = session.preview(EDIT_1001)
    second = session.preview([{"action": "edit", "target_plu": "1002", "price": "21.00"}])
    session.apply(first["preview_id"])
    writes = len(env.fakes["A"].messages_of("GP"))
    stale = session.apply(second["preview_id"])
    assert stale.is_error and stale.structured_content["error"]["code"] == "preview_stale"
    assert len(env.fakes["A"].messages_of("GP")) == writes


# ---------- acknowledgements ----------


def test_a_large_price_change_needs_the_warning_acknowledged(env):
    session = Session(env)
    preview = session.preview([{"action": "edit", "target_plu": "1001", "price": "99.00"}])
    assert preview["must_acknowledge_warnings"] is True
    assert preview["warnings_requiring_ack"][0]["code"] == "price_change_large"
    assert "warnings_requiring_ack" in preview["next_step"]
    assert "acknowledge_warnings=true" not in preview["next_step"], "the call is not written out for Claude"

    refused = session.apply(preview["preview_id"])
    assert refused.is_error and refused.structured_content["error"]["code"] == "acknowledgement_required"
    assert not env.fakes["A"].messages_of("GP")

    # Near enough is not enough. It must be exactly true.
    for value in ("true", 1, "yes"):
        bad = session.apply(preview["preview_id"], acknowledge_warnings=value)
        assert bad.is_error, value
    assert not env.fakes["A"].messages_of("GP")

    done = session.apply(preview["preview_id"], acknowledge_warnings=True).structured_content
    assert done["success"] is True and done["acknowledged_warnings"] is True
    assert held_price(env.fakes["A"], 1) == "99.00"


def test_drift_is_shown_refused_and_written_only_when_acknowledged(env):
    session = Session(env)
    fake = env.fakes["A"]
    make_drift(fake, 1, "11.00")

    preview = session.preview(EDIT_1001)
    assert preview["drift"][0]["kind"] == "differs"
    assert preview["drift"][0]["fields"]["price"] == {"before": "10.00", "after": "11.00"}
    tokens = preview["drift_tokens_to_acknowledge"]
    assert len(tokens) == 1 and tokens[0] not in preview["next_step"], "tokens stay in their own field"
    assert "Drift that needs acknowledging" in preview["summary"]
    assert "price 11.00 on the scale and 10.00 expected" in preview["summary"]

    # Applied with no acknowledgement: refused, and nothing written.
    refused = session.apply(preview["preview_id"])
    data = refused.structured_content
    assert refused.is_error
    assert data["success"] is False and data["scale_statuses"] == {"A": "drift_refused"}
    assert data["drift_tokens"] == tokens
    assert "used up" in data["next_step"]
    assert held_price(fake, 1) == "11.00" and not fake.messages_of("GP")

    # The warning flag is not a drift acknowledgement, and neither is a wrong token.
    preview = session.preview(EDIT_1001)
    data = session.apply(preview["preview_id"], acknowledge_warnings=True, acknowledge_drift=["A:1:wrong"]).structured_content
    assert data["scale_statuses"] == {"A": "drift_refused"} and not fake.messages_of("GP")

    # Named exactly: written, and verified.
    preview = session.preview(EDIT_1001)
    data = session.apply(preview["preview_id"], acknowledge_drift=preview["drift_tokens_to_acknowledge"]).structured_content
    assert data["success"] is True and data["scale_statuses"] == {"A": "verified"}
    assert held_price(fake, 1) == "12.00"


def test_a_token_goes_stale_when_the_scale_changes_again(env):
    session = Session(env)
    fake = env.fakes["A"]
    make_drift(fake, 1, "11.00")
    preview = session.preview(EDIT_1001)
    make_drift(fake, 1, "13.00")
    data = session.apply(preview["preview_id"], acknowledge_drift=preview["drift_tokens_to_acknowledge"]).structured_content
    assert data["scale_statuses"] == {"A": "drift_refused"}
    assert held_price(fake, 1) == "13.00"


def test_drift_that_cannot_be_acknowledged_is_named(env):
    session = Session(env)
    stranger = ("4", "7777", "7777", "Stranger Item", "3.33", "", "1", "1", "0", "0", "0", "0",
                "2026-01-01 00:00:00", "0", "0", "0", "0", "0", "0", "0")
    env.fakes["A"].preload(stranger)
    preview = session.preview([{"action": "add", "plu": "4001", "description": "New", "price": "3.00"}])
    assert preview["drift_that_cannot_be_acknowledged"][0]["kind"] == "id_occupied"
    assert preview["drift_tokens_to_acknowledge"] == []
    assert "cannot be acknowledged" in preview["summary"]
    data = session.apply(preview["preview_id"]).structured_content
    assert data["success"] is False and data["scale_statuses"] == {"A": "drift_refused"}
    assert env.fakes["A"].item(4)[3] == "Stranger Item"


def test_a_scale_that_cannot_be_read_for_a_preview_is_named_as_not_checked(make_env):
    env = make_env({"A": True, "B": True}, connect_attempts=1)
    env.fakes["B"].stop_listening()
    preview = Session(env).preview(EDIT_1001)
    assert preview["scales_read"] == ["A"] and preview["drift_not_checked_on"] == ["B"]
    assert "scale_read_failed" in [w["code"] for w in preview["warnings"]]
    assert "Drift was NOT checked on B" in preview["summary"]
    assert "none found" not in preview["summary"]


def test_a_preview_that_skips_reading_does_not_claim_no_drift(env):
    session = Session(env)
    make_drift(env.fakes["A"], 1, "11.00")
    preview = session.preview(EDIT_1001, read_scales=False)
    assert env.silent("A")
    assert preview["scales_read"] == [] and preview["drift_not_checked_on"] == ["A"]
    assert "NOT checked" in preview["summary"] and "none found" not in preview["summary"]
    # Apply still checks.
    data = session.apply(preview["preview_id"]).structured_content
    assert data["scale_statuses"] == {"A": "drift_refused"}


# ---------- partial results are never called a success ----------


def assert_not_called_a_success(data):
    assert data["success"] is False
    assert not data["summary"].startswith("SUCCESS")
    assert "success" not in data["summary"].lower()
    assert "Every scale confirmed" not in data["summary"]


def test_a_scale_saying_error_partway_is_partial_and_named(env3):
    env3.fakes["B"].error_on_ids(1)
    session = Session(env3)
    preview = session.preview(EDIT_1001)
    result = session.apply(preview["preview_id"])
    data = result.structured_content
    assert result.is_error
    assert data["outcome"] == "partial"
    assert data["scale_statuses"] == {"A": "verified", "B": "rejected", "C": "verified"}
    assert_not_called_a_success(data)
    assert data["summary"].startswith("PARTIAL, NOT COMPLETE.")
    assert "B: rejected." in data["summary"] and "A: verified." in data["summary"]
    assert held_price(env3.fakes["A"], 1) == "12.00"
    assert held_price(env3.fakes["B"], 1) == "10.00"
    assert held_price(env3.fakes["C"], 1) == "12.00", "a failure must not stop the next scale"


def test_a_scale_going_away_partway_is_named_and_the_next_scale_still_runs(make_env):
    env = make_env({"A": True, "B": True, "C": True}, connect_attempts=1)
    session = Session(env)
    preview = session.preview(EDIT_1001)
    env.fakes["B"].stop_listening()
    data = session.apply(preview["preview_id"]).structured_content
    assert data["scale_statuses"] == {"A": "verified", "B": "unreachable", "C": "verified"}
    assert_not_called_a_success(data)
    assert held_price(env.fakes["C"], 1) == "12.00"


def test_a_stall_partway_is_unknown_and_says_not_to_resend(make_env):
    env = make_env({"A": True, "B": True}, reply_timeout_seconds=0.3)
    session = Session(env)
    preview = session.preview(EDIT_1001)
    env.fakes["B"].stall()
    result = session.apply(preview["preview_id"])
    data = result.structured_content
    assert result.is_error
    assert data["scale_statuses"] == {"A": "verified", "B": "unknown"}
    assert_not_called_a_success(data)
    assert "Do not resend" in data["next_step"]


def test_every_scale_failing_is_failed(make_env):
    env = make_env({"A": True}, connect_attempts=1)
    session = Session(env)
    preview = session.preview(EDIT_1001)
    env.fakes["A"].stop_listening()
    result = session.apply(preview["preview_id"])
    assert result.is_error
    assert result.structured_content["outcome"] == "failed"
    assert result.structured_content["summary"].startswith("FAILED.")


def test_a_batch_that_partly_fails_says_so_item_by_item(make_env):
    env = make_env({"A": True}, batch_size=1)
    env.fakes["A"].error_on_ids(2)
    session = Session(env)
    preview = session.preview(
        [
            {"action": "edit", "target_plu": "1001", "price": "11.00"},
            {"action": "edit", "target_plu": "1002", "price": "21.00"},
            {"action": "edit", "target_plu": "1003", "price": "5.75"},
        ]
    )
    result = session.apply(preview["preview_id"])
    data = result.structured_content
    assert result.is_error
    assert_not_called_a_success(data)
    assert data["batch_partly_failed"] is True
    by_plu = {row["plu"]: row["scales"]["A"]["status"] for row in data["items"]}
    assert by_plu["1001"] == "verified" and by_plu["1002"] == "rejected"
    assert by_plu["1003"] != "verified"
    assert data["items_verified_everywhere"] == 1
    assert "BATCH PARTLY FAILED" in data["summary"]
    assert "PLU 1002: A rejected." in data["summary"]
    assert held_price(env.fakes["A"], 1) == "11.00" and held_price(env.fakes["A"], 2) == "20.00"


def test_a_scale_with_writes_off_is_skipped_silent_and_never_a_success(make_env):
    env = make_env({"A": True, "B": False})
    session = Session(env)
    preview = session.preview(EDIT_1001)
    assert [w["code"] for w in preview["warnings"]] == ["scale_not_write_enabled"]
    assert preview["drift_not_checked_on"] == ["B"]
    result = session.apply(preview["preview_id"])
    data = result.structured_content
    assert not result.is_error, "writes being off is not a failure, but it is not a success either"
    assert data["outcome"] == "partial"
    assert data["scale_statuses"] == {"A": "verified", "B": "skipped_not_write_enabled"}
    assert_not_called_a_success(data)
    assert "still hold the old values" in data["next_step"]
    assert env.fakes["B"].nothing_received, "a scale with no flags is never contacted"
    assert held_price(env.fakes["A"], 1) == "12.00"


def test_all_scales_with_writes_off_sends_nothing(make_env):
    env = make_env({"A": False})
    session = Session(env)
    preview = session.preview(EDIT_1001)
    result = session.apply(preview["preview_id"])
    data = result.structured_content
    assert data["scale_statuses"] == {"A": "skipped_not_write_enabled"}
    assert result.is_error and data["outcome"] == "failed"
    assert env.silent("A")


def test_accepted_not_verified_is_not_called_confirmed(make_env):
    env = make_env(
        {"A": True},
        connect_attempts=1,
        read_back_window_seconds=0.2,
        fake_options={"post_write_refusal_seconds": 30.0},
    )
    session = Session(env)
    preview = session.preview(EDIT_1001)
    result = session.apply(preview["preview_id"])
    data = result.structured_content
    assert data["scale_statuses"] == {"A": "accepted_not_verified"}
    assert_not_called_a_success(data)
    assert "NOT confirmed" in data["summary"]
    assert "check the scales" in data["next_step"]
    assert data["summary"].startswith("PARTIAL, NOT COMPLETE.")


def test_a_subset_of_scales_needs_acknowledging_and_can_never_be_a_success(env3):
    session = Session(env3)
    preview = session.preview(EDIT_1001, scales=["A"])
    assert preview["must_acknowledge_warnings"] is True
    refused = session.apply(preview["preview_id"])
    assert refused.structured_content["error"]["code"] == "acknowledgement_required"
    data = session.apply(preview["preview_id"], acknowledge_warnings=True).structured_content
    assert data["verified"] is True, "the one targeted scale is verified"
    assert_not_called_a_success(data)
    assert data["not_touched"] == ["B", "C"]
    assert "Not touched: B, C." in data["summary"]
    assert env3.silent("B", "C")


def test_a_status_hidden_by_a_worse_one_on_the_same_scale_still_gets_its_next_step():
    """A scale shows only its worst status. An edit that was only accepted must still be
    flagged for a person to check beside a record that reports not_available. Nothing reports
    that now, but an old logged change can, and it must not send anyone to ScaleLink."""
    items = (ItemResult(1, "1001", "delete", "not_available"), ItemResult(2, "1002", "edit", "accepted_not_verified"))
    record = ChangeRecord(
        "c", "p", "x", False, "t", "t", "partial", False, (),
        (ScaleResult("A", "not_available", "", items=items),), "s", display="Partly done.",
    )
    data = _change_payload(record)
    assert "check the scales" in data["next_step"] and "was not done" in data["next_step"]
    assert "ScaleLink" not in data["next_step"]
    assert data["success"] is False


def test_a_scale_message_written_before_the_read_back_does_not_contradict_the_status():
    """The library's scale message can say unknown after the read back settled it as rejected."""
    result = ScaleResult("A", "rejected", "A: the result is unknown.", items=(ItemResult(1, "1001", "edit", "rejected"),))
    record = ChangeRecord("c", "p", "x", False, "t", "t", "failed", False, (), (result,), "s", display="Not done.")
    data = _change_payload(record)
    assert "A: rejected." in data["summary"]
    assert "result is unknown" not in data["summary"]
    assert data["scale_results"][0]["message"] == "A: the result is unknown.", "kept in the structured data"


def test_the_server_does_not_take_the_librarys_word_for_success(env):
    """If the flag and the scale results ever disagreed, the results win."""
    bad_results = [
        ScaleResult("A", "rejected", "", items=(ItemResult(1, "1001", "edit", "rejected"),)),
        ScaleResult("A", "verified", "", items=(ItemResult(1, "1001", "edit", "unknown"),)),
    ]
    for result in bad_results:
        record = ChangeRecord("c", "p", "x", False, "t", "t", "success", True, (), (result,), "s", display="Done.")
        assert _is_full_success(record) is False
        data = _change_payload(record)
        assert data["success"] is False
        assert data["summary"].startswith("PROBLEM:")
        assert "Done." not in data["summary"]
    empty = ChangeRecord("c", "p", "x", False, "t", "t", "success", True, (), (), "s", display="d")
    assert _is_full_success(empty) is False
    subset = ChangeRecord("c", "p", "x", False, "t", "t", "success", True, (),
                          (ScaleResult("A", "verified", "", items=(ItemResult(1, "1001", "edit", "verified"),)),),
                          "s", display="d", not_touched=("B",))
    assert _is_full_success(subset) is False


# ---------- bad input ----------


@pytest.mark.parametrize(
    "change, code",
    [
        ({"action": "edit", "target_plu": "1001", "price": "0.00"}, "invalid_price"),
        ({"action": "edit", "target_plu": "1001", "price": "1000.00"}, "invalid_price"),
        ({"action": "edit", "target_plu": "1001", "price": "9.9"}, "invalid_price"),
        ({"action": "edit", "target_plu": "1001", "price": "-1.00"}, "invalid_price"),
        ({"action": "edit", "target_plu": "1001", "plu": "44444"}, "invalid_plu"),
        ({"action": "edit", "target_plu": "1001", "plu": "4444", "code": "4445"}, "code_plu_mismatch"),
        ({"action": "edit", "target_plu": "1001", "plu": "9001"}, "reserved_plu"),
        ({"action": "edit", "target_plu": "1001", "plu": "1002"}, "duplicate_plu"),
        ({"action": "edit", "target_plu": "9999", "price": "1.00"}, "item_not_found"),
        ({"action": "edit", "target_plu": "1001"}, "invalid_change"),
        ({"action": "add", "plu": "123", "description": "X", "price": "1.00"}, "invalid_plu"),
        ({"action": "add", "plu": "12345", "description": "X", "price": "1.00"}, "invalid_plu"),
        ({"action": "add", "plu": "1001", "description": "X", "price": "1.00"}, "duplicate_plu"),
        ({"action": "add", "plu": "4001", "description": "X" * 36, "price": "1.00"}, "invalid_description"),
        ({"action": "add", "plu": "4001", "description": "Bad\ttab", "price": "1.00"}, "invalid_description"),
        ({"action": "add", "plu": "4001", "code": "4002", "description": "X", "price": "1.00"}, "code_plu_mismatch"),
        ({"action": "add", "plu": "4001", "description": "X"}, "invalid_change"),
    ],
)
def test_bad_values_are_refused_by_the_library_with_nothing_sent(env, change, code):
    result = Session(env).call("scale_preview_changes", changes=[change])
    data = result.structured_content
    assert result.is_error
    assert data["ok"] is False and data["preview_id"] is None
    assert data["nothing_sent"] is True
    assert code in [e["code"] for e in data["errors"]]
    assert data["summary"].startswith("REFUSED.")
    assert env.silent("A"), "bad input must not even read a scale"


@pytest.mark.parametrize(
    "change",
    [
        {"action": "edit", "target_plu": "1001", "price": 12.5},
        {"action": "edit", "target_plu": "1001", "price": 12},
        {"action": "edit", "target_plu": 1001, "price": "12.00"},
        {"action": "edit", "target_id": "1", "price": "12.00"},
        {"action": "edit", "target_plu": "1001", "price": "12.00", "timestamp": "x"},
        {"action": "edit", "target_plu": "1001", "fields_7_12": ["0", "0", "0", "0", "0", "0"]},
        {"action": "add", "id": 9, "plu": "4001", "description": "X", "price": "1.00"},
        {"action": "add", "plu": "4001", "description": "X", "price": "1.00", "sold_by": "slice"},
        {"action": "reset"},
    ],
)
def test_input_of_the_wrong_shape_is_refused_with_nothing_sent(env, change):
    result = Session(env).call("scale_preview_changes", changes=[change])
    assert result.is_error
    assert env.silent("A")


def test_one_bad_change_refuses_the_whole_batch_and_names_it(env):
    result = Session(env).call(
        "scale_preview_changes",
        changes=[
            {"action": "edit", "target_plu": "1001", "price": "11.00"},
            {"action": "edit", "target_plu": "1002", "price": "bad"},
            {"action": "edit", "target_plu": "1003", "price": "2000.00"},
        ],
    )
    data = result.structured_content
    assert result.is_error and data["preview_id"] is None
    assert sorted(e["index"] for e in data["errors"]) == [1, 2]
    assert "Change 1: invalid_price" in data["summary"] and "Change 2: invalid_price" in data["summary"]
    assert env.silent("A")


def test_an_unknown_scale_or_empty_list_is_refused_with_nothing_sent(env):
    session = Session(env)
    assert session.call("scale_preview_changes", changes=EDIT_1001, scales=["NOPE"]).is_error
    assert session.call("scale_preview_changes", changes=[]).is_error
    assert env.silent("A")


# ---------- delete ----------


def test_delete_needs_an_acknowledgement_and_then_writes_a_placeholder(env):
    session = Session(env)
    preview = session.preview([{"action": "delete", "target_plu": "1001"}])
    assert [w["code"] for w in preview["warnings"]] == ["delete_placeholder"]
    refused = session.apply(preview["preview_id"])
    assert refused.is_error and not env.fakes["A"].messages_of("GP")
    result = session.apply(preview["preview_id"], acknowledge_warnings=True)
    data = result.structured_content
    assert not result.is_error
    assert data["scale_statuses"] == {"A": "verified"}
    assert env.fakes["A"].item(1)[3:5] == ("UNUSED", "0.01")
    assert env.catalog.get_by_id(1) is None


def test_a_delete_in_a_batch_goes_with_the_edit(env):
    session = Session(env)
    preview = session.preview(
        [
            {"action": "delete", "target_plu": "1001"},
            {"action": "edit", "target_plu": "1002", "price": "21.00"},
        ]
    )
    result = session.apply(preview["preview_id"], acknowledge_warnings=True)
    data = result.structured_content
    assert not result.is_error
    assert data["batch_partly_failed"] is False
    assert {row["plu"]: row["scales"]["A"]["status"] for row in data["items"]} == {"1001": "verified", "1002": "verified"}
    assert held_price(env.fakes["A"], 2) == "21.00"


# ---------- who asked ----------


def logged_by(env, change_id):
    return next(e for e in env.catalog.read_log(change_id) if e["event"] == "started")["payload"]["requested_by"]


def test_the_change_log_names_the_client(env):
    session = Session(env)
    client_kw = {"client_info": Implementation(name="claude-code", version="9")}
    preview = session.call("scale_preview_changes", client_kw=client_kw, changes=EDIT_1001).structured_content
    assert preview["requested_by"] == "mcp:claude-code"
    data = session.call("scale_apply_changes", client_kw=client_kw, preview_id=preview["preview_id"]).structured_content
    assert data["requested_by"] == "mcp:claude-code"
    assert logged_by(env, data["change_id"]) == "mcp:claude-code"


def test_the_log_says_claude_when_the_client_gives_no_name(env):
    session = Session(env)
    preview = session.preview(EDIT_1001)
    data = session.apply(preview["preview_id"]).structured_content
    assert logged_by(env, data["change_id"]) == "mcp:claude"


def test_a_name_set_for_the_token_beats_what_the_client_says(env):
    session = Session(env, caller="invoice-desk")
    client_kw = {"client_info": Implementation(name="someone-else", version="1")}
    preview = session.call("scale_preview_changes", client_kw=client_kw, changes=EDIT_1001).structured_content
    data = session.call("scale_apply_changes", client_kw=client_kw, preview_id=preview["preview_id"]).structured_content
    assert logged_by(env, data["change_id"]) == "mcp:invoice-desk"


def test_a_client_name_cannot_put_odd_text_in_the_log():
    assert _caller_name("bad\nname") == "mcp:claude"
    assert _caller_name("x" * 65) == "mcp:claude"
    assert _caller_name(None) == "mcp:claude"
    assert _caller_name(12) == "mcp:claude"
    assert _caller_name("  claude-code ") == "mcp:claude-code"


# ---------- comparing and reading ----------


def test_compare_says_all_match_only_when_every_scale_was_read_and_matches(env3):
    data = Session(env3).data("scale_compare_scales")
    assert data["all_match"] is True
    assert {s["scale"]: s["status"] for s in data["scales"]} == dict.fromkeys("ABC", "compared")
    assert not env3.fakes["A"].messages_of("GP")


def test_compare_lists_differences_and_never_writes(env):
    fake = env.fakes["A"]
    make_drift(fake, 1, "11.00")
    fake.remove(2)
    result = Session(env).call("scale_compare_scales")
    data = result.structured_content
    assert not result.is_error
    assert data["all_match"] is False and data["scales_that_differ"] == ["A"]
    scale = data["scales"][0]
    assert scale["differences_total"] == 2
    assert scale["differences_by_kind"] == {"differs": 1, "missing": 1}
    assert data["summary"].startswith("NOT all matching.")
    assert not fake.messages_of("GP")


def test_compare_cuts_a_long_list_and_says_so(env):
    fake = env.fakes["A"]
    make_drift(fake, 1, "11.00")
    fake.remove(2)
    scale = Session(env).data("scale_compare_scales", max_differences=1)["scales"][0]
    assert scale["differences_total"] == 2 and scale["differences_shown"] == 1 and len(scale["differences"]) == 1


def test_compare_with_a_scale_not_read_is_not_all_match(make_env):
    env = make_env({"A": True, "B": False})
    data = Session(env).data("scale_compare_scales")
    assert data["all_match"] is False and data["not_compared"] == ["B"]
    assert "nobody knows" in data["summary"]
    assert env.fakes["B"].nothing_received


def test_compare_refuses_an_unknown_scale_rather_than_answering_empty(env):
    session = Session(env)
    for names in (["NOPE"], []):
        result = session.call("scale_compare_scales", scales=names)
        assert result.is_error, names
        assert result.structured_content.get("all_match") is None
    assert env.silent("A")


def test_compare_of_an_unreachable_scale_is_an_error(make_env):
    env = make_env({"A": True}, connect_attempts=1)
    env.fakes["A"].stop_listening()
    result = Session(env).call("scale_compare_scales")
    assert result.is_error
    assert result.structured_content["scales"][0]["status"] == "unreachable"
    assert result.structured_content["all_match"] is False


def test_read_items_reports_what_the_scale_holds_not_the_catalog(make_env):
    env = make_env({"A": True, "B": True, "C": False})
    make_drift(env.fakes["B"], 1, "11.00")
    env.fakes["B"].remove(2)
    data = Session(env).data("scale_read_items", plus=["1001", "1002", "9999"], ids=[1])
    scales = {s["scale"]: s for s in data["scales"]}

    a = scales["A"]["results"]
    assert a[0]["found"] and a[0]["records"][0]["matches_catalog"] and a[0]["records"][0]["record"]["price"] == "10.00"
    assert a[2] == {"plu": "9999", "found": False, "records": [], "catalog_record": None}
    assert a[3]["id"] == 1 and a[3]["found"]

    b = scales["B"]["results"]
    assert b[0]["records"][0]["matches_catalog"] is False and b[0]["records"][0]["record"]["price"] == "11.00"
    assert b[0]["catalog_record"]["price"] == "10.00"
    assert b[1]["found"] is False, "the scale holds nothing there, whatever the catalog says"
    assert "DIFFERS from the catalog" in data["summary"]

    assert scales["C"]["was_read"] is False and scales["C"]["results"] == []
    assert data["all_read"] is False
    assert "C: NOT READ (not_contacted), so nobody knows" in data["summary"]
    assert env.fakes["C"].nothing_received


def test_read_items_finds_a_plu_under_another_id(env):
    fake = env.fakes["A"]
    stray = fields_of(fake.item(1))
    stray[0], stray[1], stray[2], stray[3] = "9", "1002", "1002", "Stray Copy"
    fake.preload(tuple(stray))
    data = Session(env).data("scale_read_items", plus=["1002"])
    records = data["scales"][0]["results"][0]["records"]
    assert [r["id"] for r in records] == [2, 9]
    assert records[0]["matches_catalog"] is True and records[1]["matches_catalog"] is False


def test_read_items_needs_a_target_and_a_known_scale(env):
    session = Session(env)
    assert session.call("scale_read_items").is_error
    assert session.call("scale_read_items", plus=["1001"], scales=["NOPE"]).is_error
    assert env.silent("A")


# ---------- earlier changes ----------


def test_get_change_finds_a_change_by_either_id(env3):
    env3.fakes["B"].error_on_ids(1)
    session = Session(env3)
    preview = session.preview(EDIT_1001)
    applied = session.apply(preview["preview_id"]).structured_content
    by_change = session.data("scale_get_change", change_id=applied["change_id"])
    by_preview = session.data("scale_get_change", preview_id=preview["preview_id"])
    for found in (by_change, by_preview):
        assert found["change_id"] == applied["change_id"]
        assert found["scale_statuses"] == {"A": "verified", "B": "rejected", "C": "verified"}
        assert found["success"] is False
        assert found["summary"].startswith("PARTIAL, NOT COMPLETE.")
    assert by_preview["applied"] is True


def test_get_change_says_when_a_preview_never_ran(env):
    session = Session(env)
    preview = session.preview(EDIT_1001)
    data = session.data("scale_get_change", preview_id=preview["preview_id"])
    assert data["applied"] is False and data["success"] is False
    assert "has not been applied" in data["summary"]


def test_get_change_refuses_bad_ids(env):
    session = Session(env)
    assert session.call("scale_get_change").is_error
    assert session.call("scale_get_change", change_id="a", preview_id="b").is_error
    assert session.call("scale_get_change", change_id="nope").structured_content["error"]["code"] == "change_not_found"
    assert session.call("scale_get_change", preview_id="nope").structured_content["error"]["code"] == "preview_not_found"


def test_a_change_with_no_finish_reads_as_not_finished_and_blocks_until_resolved(env):
    session = Session(env)
    env.catalog.append_log(
        "deadchange",
        "started",
        {"preview_id": "p", "requested_by": "mcp:claude", "changes": [{"item_id": 1, "action": "edit", "index": 0, "plu": "1001", "fields": {}, "before": None, "after": None}]},
        "2026-01-01T00:00:00+00:00",
    )
    info = session.data("scale_list_scales")
    assert info["interrupted_change_ids"] == ["deadchange"]

    change = session.data("scale_get_change", change_id="deadchange")
    assert change["outcome"] == "in_progress" and change["success"] is False
    assert change["summary"].startswith("STOPPED WITHOUT FINISHING")
    assert change["running"] is False

    preview = session.preview(EDIT_1001)
    blocked = session.apply(preview["preview_id"])
    assert blocked.is_error and blocked.structured_content["error"]["code"] == "interrupted_change"
    assert not env.fakes["A"].messages_of("GP")

    assert session.call("scale_resolve_interrupted_change", change_id="deadchange", note="   ").is_error
    resolved = session.data("scale_resolve_interrupted_change", change_id="deadchange", note="Compared. A holds the old price.")
    assert resolved["outcome"] == "interrupted" and resolved["success"] is False
    assert resolved["summary"].startswith("INTERRUPTED")
    assert session.data("scale_list_scales")["interrupted_change_ids"] == []

    preview = session.preview(EDIT_1001)
    assert session.apply(preview["preview_id"]).structured_content["success"] is True


# ---------- two processes share the catalog ----------


def test_a_second_service_on_the_same_files_gets_apply_busy_and_sends_nothing(make_env):
    """The web page and this server may run side by side. The library's file lock covers
    them, and a busy scale refuses at once rather than queueing."""
    env = make_env({"A": True}, fake_options={"read_delay": 0.3})
    other_catalog = Catalog(env.settings.catalog_path)
    other = ScaleConnectService(env.config, other_catalog, sleep=lambda s: None)
    session = Session(env)

    preview = session.preview(EDIT_1001, read_scales=False)
    started = threading.Event()
    results = []

    def slow_read():
        started.set()
        results.append(other.compare_scales())

    worker = threading.Thread(target=slow_read)
    worker.start()
    started.wait(5)
    assert env.fakes["A"].wait_for_messages(2, timeout=5), "the other service should be reading"

    for result in (session.apply(preview["preview_id"]), session.call("scale_compare_scales")):
        assert result.is_error
        assert result.structured_content["error"]["code"] == "apply_busy"
        assert "Nothing is queued" in result.structured_content["next_step"]
    assert not env.fakes["A"].messages_of("GP")
    worker.join()
    other.close()

    # The preview is still good, and a change made through one service shows in the other.
    data = session.apply(preview["preview_id"]).structured_content
    assert data["success"] is True


def test_service_calls_run_on_a_fixed_set_of_threads(env, monkeypatch):
    seen = []
    real = env.svc.list_scales

    def spy():
        seen.append(threading.current_thread().name)
        return real()

    monkeypatch.setattr(env.svc, "list_scales", spy)
    session = Session(env)
    for _ in range(3):
        session.data("scale_list_scales")
    assert seen and all(name.startswith("scaleconnect-service") for name in seen)


def test_unexpected_problems_are_reported_as_unknown_on_a_write(env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(env.svc, "apply_change", boom)
    monkeypatch.setattr(env.svc, "search_items", boom)
    session = Session(env)
    result = session.apply("0" * 32)
    assert result.is_error
    assert result.structured_content["summary"].startswith("RESULT UNKNOWN.")
    assert result.structured_content["nothing_sent"] is False
    assert "secret detail" not in result.structured_content["summary"]
    read = session.call("scale_search_items", query="x")
    assert read.is_error and read.structured_content["summary"].startswith("FAILED.")


def test_the_older_client_handshake_works_too(env):
    session = Session(env)
    client_kw = {"mode": "legacy", "client_info": Implementation(name="old-client", version="1")}
    preview = session.call("scale_preview_changes", client_kw=client_kw, changes=EDIT_1001).structured_content
    assert preview["requested_by"] == "mcp:old-client"
    data = session.call("scale_apply_changes", client_kw=client_kw, preview_id=preview["preview_id"]).structured_content
    assert data["success"] is True
