# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""driver-dev's quick tests. Localhost and the fake scale only. Made up data only."""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fake_scale import FakeScale

from scaleconnect import cli, protocol, scale as scale_module
from scaleconnect.catalog import Catalog
from scaleconnect.config import (
    Config,
    ScaleConfig,
    Settings,
    catalog_path_for,
    load_config,
    restrict_unofficial,
)
from scaleconnect.errors import (
    AcknowledgementRequired,
    BackupFailed,
    CatalogNotSeeded,
    ChangeNotFound,
    InterruptedChange,
    NotReadEnabled,
    NotWriteEnabled,
    PreviewAlreadyApplied,
    PreviewExpired,
    PreviewNotFound,
    PreviewStale,
    ProtocolError,
)
from scaleconnect.models import Change, Item, ScaleStatus
from scaleconnect.scale import ReadResult, ScaleClient
from scaleconnect.seed import seed_catalog
from scaleconnect.service import ScaleConnectService

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
SEED_STAMP = "2026-01-01 00:00:00"


def seed_items():
    return [
        Item(id=1, code="1001", plu="1001", description="Test Alpha", price="10.00", field6=" ", timestamp=SEED_STAMP),
        Item(id=2, code="1002", plu="1002", description="Test Beta", price="5.50", timestamp=SEED_STAMP),
        Item(id=3, code="1004", plu="1004", description="Test Gamma", price="2.00", timestamp=SEED_STAMP),
    ]


def fast_settings(tmp_path, **kw):
    base = dict(
        reply_timeout_seconds=0.4,
        connect_timeout_seconds=0.4,
        connect_attempts=2,
        connect_retry_wait_seconds=0.01,
        read_pace_seconds=0.0,
        read_back_window_seconds=3.0,
        read_back_retry_seconds=0.05,
        state_dir=tmp_path,
        scalelink_db_copy="not_available_here",
    )
    base.update(kw)
    return Settings(**base)


def conf(fake, name="A", enabled=True, **kw):
    return ScaleConfig(name=name, address="127.0.0.1", port=fake.port, write_enabled=enabled, serial=fake.serial, **kw)


def loaded(fake):
    fake.preload(*[tuple(i.to_fields()) for i in seed_items()])
    return fake


@pytest.fixture
def make_service(tmp_path):
    made = []

    def _make(scales, settings=None, **kw):
        settings = settings or fast_settings(tmp_path)
        catalog = Catalog(settings.catalog_path)
        if not catalog.is_seeded():
            catalog.import_items(seed_items())
        service = ScaleConnectService(Config(scales=tuple(scales), settings=settings), catalog, **kw)
        made.append(service)
        return service

    yield _make
    for service in made:
        service.close()


def apply_edit(service, price="10.50", plu="1001", ack=False, drift=()):
    preview = service.preview_changes([{"action": "edit", "target_plu": plu, "price": price}], requested_by="test")
    assert preview.ok, preview.errors
    return service.apply_change(preview.preview_id, ack, acknowledge_drift=drift)


# ---------------------------------------------------------------- only these messages


def test_only_the_seven_message_builders_exist():
    builders = {n for n, _ in inspect.getmembers(protocol, inspect.isfunction) if n.startswith("build_")}
    assert builders == {
        "build_ri",
        "build_open",
        "build_finish",
        "build_item_batch",
        "build_identity",
        "build_read_start",
        "build_read_next",
    }


@pytest.mark.parametrize(
    "text",
    ["EP", "EP01500", "EX", "E", "LG", "LH", "LT", "LC", "L", "LP01", "LP485", "SP", "DP", "CP", "RP",
     "SetHORA,12:00:00", "GPX", "OK", "ok", "", "ZZ", "GPF1", "bX02XX"],
)
def test_no_other_message_can_be_framed(text):
    with pytest.raises(ProtocolError):
        protocol._frame(text)


def test_the_allowed_messages_match_the_protocol_vectors():
    assert protocol.build_ri() == b"RI\t\n4\r"
    assert protocol.build_identity() == b"CX\t\n0\r"
    assert protocol.build_read_start() == b"LP\t\n6\r"
    assert protocol.build_read_next() == b"Ok\t\n7\r"
    assert protocol.build_open() == b"bX01XX\t\n1\r"
    assert protocol.build_finish() == b"GPF0\t\n8\r"
    item = Item(485, "9992", "9992", "TEST ITEM TWO", "1.02", timestamp="2026-11-01 13:31:00")
    assert protocol.build_item_batch([item]) == (
        b"GP01485\t9992\t9992\tTEST ITEM TWO\t1.02\t\t1\t1\t0\t0\t0\t0\t2026-11-01 13:31:00\t0\t0\t0\t0\t0\t0\t0\t\n:\r"
    )


def test_no_source_file_holds_a_forbidden_message():
    pattern = re.compile(r"""["'](EP|LG|LH|LT|SP|DP)[\\"'\d]""")
    for path in (SRC / "scaleconnect").glob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name


def test_no_public_raw_send():
    public = {n for n in dir(ScaleClient) if not n.startswith("_")}
    assert public == {"identify", "read_items", "send_items"}
    assert not hasattr(protocol, "frame")
    assert set(scale_module._READ_MESSAGES) == {"CX", "LP", "Ok"}


def test_delete_is_available_and_has_no_message_of_its_own():
    assert scale_module.DELETE_AVAILABLE is True
    assert not hasattr(ScaleClient, "delete_items")


# ---------------------------------------------------------------- reply parsing


def chained(body: bytes, pairs: int = 2) -> bytes:
    out = body
    for _ in range(pairs):
        out += protocol.check_char(out) + b"\r"
    return out


RECORD = b"496\t9111\t9111\tTEST PRICE WHOLE\t5\t\t1\t1\t0\t0\t0\t0\t2026-11-01 13:38:47\t0\t0\t0\t0\t0\t0\t0\t\n"


def test_read_block_real_shape():
    block = chained(b"LP01" + RECORD)
    parsed, rest = protocol.take_read_block(block)
    assert rest == b"" and not parsed.end
    # An odd value comes through as it is. A read does no value checks.
    assert parsed.records[0].id == 496 and parsed.records[0].price == "5"


def test_read_end_block():
    parsed, rest = protocol.take_read_block(b"LP00End\t\n4\r")
    assert parsed.end and parsed.records == () and rest == b""


def test_read_block_in_pieces_is_not_cut_at_the_first_cr():
    block = chained(b"LP02" + RECORD + RECORD.replace(b"496", b"497"))
    for size in range(len(block)):
        taken = protocol.take_read_block(block[:size])
        if size < len(block) - 2:
            assert taken is None, size
    parsed, _ = protocol.take_read_block(block)
    assert [r.id for r in parsed.records] == [496, 497]


def test_read_block_longer_chain_after_a_resend():
    parsed, rest = protocol.take_read_block(chained(b"LP01" + RECORD, pairs=4))
    assert rest == b"" and parsed.records[0].id == 496


def test_leftover_pairs_in_front_of_the_next_block_are_skipped():
    parsed, rest = protocol.take_read_block(b"7\r8\r" + chained(b"LP01" + RECORD))
    assert parsed.records[0].id == 496 and rest == b""
    assert protocol.take_read_block(b"7\r8\rLP00End\t\n4\r")[0].end


def test_read_block_keeps_latin1_bytes():
    record = RECORD.replace(b"TEST PRICE WHOLE", b"CAF\xc9")
    parsed, _ = protocol.take_read_block(chained(b"LP01" + record))
    assert parsed.records[0].description == "CAF\u00c9"


@pytest.mark.parametrize(
    "data",
    [
        b"LG01secret\t\n1\r",
        b"LH15text\t\n1\r",
        b"XX",
        chained(b"LP01" + RECORD)[:-2] + bytes([0x30 + ((chained(b"LP01" + RECORD)[-2] - 0x30 + 1) % 11)]) + b"\r",
        chained(b"LP01" + RECORD.replace(b"\t0\t\n", b"\t\n")),
        chained(b"LP01x" + RECORD),
        b"LP00Nope\t\n" + protocol.check_char(b"LP00Nope\t\n") + b"\r",
    ],
)
def test_bad_read_blocks_raise_and_hold_no_reply_text(data):
    with pytest.raises(protocol.ReadReplyError) as caught:
        protocol.take_read_block(data)
    assert "secret" not in str(caught.value)


def test_batch_reply_rules():
    sent = protocol.build_item_batch(seed_items()[:1])
    echo = sent[-2:-1]

    def reply(word, echo_char=echo):
        return chained(echo_char + b"\r" + word + b"\t\n", 1)

    assert protocol.parse_batch_reply(reply(b"Ok"), sent).result is protocol.BatchResult.OK
    assert protocol.parse_batch_reply(reply(b"Error"), sent).result is protocol.BatchResult.ERROR
    wrong = bytes([0x30 + ((echo[0] - 0x30 + 1) % 11)])
    assert protocol.parse_batch_reply(reply(b"Ok", wrong), sent).result is protocol.BatchResult.BAD
    assert protocol.parse_batch_reply(reply(b"GPF0Ok"), sent).result is protocol.BatchResult.BAD
    assert protocol.parse_batch_reply(b"7\r" + reply(b"Ok"), sent).result is protocol.BatchResult.OK


def test_identity_parse():
    reply = chained(b"CX00\tSER-1\t40.00 lb\t0.010 lb\tEnglish\t\tlb\tW-LABEL\t2\t\n", 1)
    assert protocol.parse_identity(reply) == "SER-1"
    with pytest.raises(protocol.ReadReplyError):
        protocol.parse_identity(chained(b"LG01\tSER-1\t\n", 1))


@pytest.mark.parametrize("data", [chained(b"LG01\tpw\t\n", 1), b"junk", chained(b"CX00\tSER-1\t\n", 1)])
def test_only_known_replies_can_be_described(data):
    text = protocol.describe_reply(data)
    assert text == f"<unrecognized reply, {len(data)} bytes>"


# ---------------------------------------------------------------- guards in scale.py


def test_scale_with_no_flags_is_never_contacted(tmp_path, fake_scale):
    client = ScaleClient(conf(fake_scale, enabled=False), fast_settings(tmp_path))
    for call in (lambda: client.send_items(seed_items()[:1]), client._connect):
        with pytest.raises(NotWriteEnabled):
            call()
    for call in (client.identify, client.read_items, client._connect_read):
        with pytest.raises(NotReadEnabled):
            call()
    assert fake_scale.nothing_received


def test_read_enabled_scale_can_read_but_never_write(tmp_path, make_fake):
    fake = loaded(make_fake())
    client = ScaleClient(conf(fake, enabled=False, read_enabled=True), fast_settings(tmp_path))
    assert client.identify().ok
    assert client.read_items().ok
    with pytest.raises(NotWriteEnabled):
        client.send_items(seed_items()[:1])
    assert set(fake.kinds()) <= {"CX", "LP", "Ok"}


def test_truthy_flags_do_not_count(tmp_path, fake_scale):
    cfg = ScaleConfig("A", "127.0.0.1", fake_scale.port, write_enabled="true", serial=fake_scale.serial, read_enabled=1)
    client = ScaleClient(cfg, fast_settings(tmp_path))
    with pytest.raises(NotWriteEnabled):
        client.send_items(seed_items()[:1])
    with pytest.raises(NotReadEnabled):
        client.identify()
    assert fake_scale.nothing_received


def test_a_write_checks_identity_itself(tmp_path, make_fake):
    fake = make_fake(serial="OTHER")
    cfg = ScaleConfig("A", "127.0.0.1", fake.port, True, serial="EXPECTED")
    report = ScaleClient(cfg, fast_settings(tmp_path)).send_items(seed_items()[:1])
    assert report.status is ScaleStatus.WRONG_SCALE
    assert fake.kinds() == ["CX"]


def test_no_serial_in_config_sends_nothing(tmp_path, fake_scale):
    cfg = ScaleConfig("A", "127.0.0.1", fake_scale.port, True)
    assert ScaleClient(cfg, fast_settings(tmp_path)).send_items(seed_items()[:1]).status is ScaleStatus.WRONG_SCALE
    assert fake_scale.nothing_received


# ---------------------------------------------------------------- reads


def test_read_stops_early(tmp_path, make_fake):
    fake = make_fake()
    fake.preload(*[tuple(Item(i, f"{2000+i}", f"{2000+i}", f"T {i}", "1.00", timestamp=SEED_STAMP).to_fields()) for i in range(1, 21)])
    result = ScaleClient(conf(fake), fast_settings(tmp_path)).read_items(7)
    assert result.ok and not result.complete and max(result.items) == 10
    assert fake.kinds() == ["CX", "LP", "Ok"]


def test_full_read_reaches_the_end(tmp_path, make_fake):
    fake = loaded(make_fake())
    result = ScaleClient(conf(fake), fast_settings(tmp_path)).read_items()
    assert result.ok and result.complete and sorted(result.items) == [1, 2, 3]


def test_a_stall_stops_the_read_with_no_retry(tmp_path, make_fake):
    fake = loaded(make_fake())
    fake.stall_read(times=None)
    result = ScaleClient(conf(fake), fast_settings(tmp_path)).read_items()
    assert not result.ok and result.status is ScaleStatus.READ_FAILED
    assert fake.kinds().count("LP") == 1


def test_pace_is_used_between_read_messages(tmp_path, make_fake):
    fake = make_fake()
    fake.preload(*[tuple(Item(i, f"{2000+i}", f"{2000+i}", f"T {i}", "1.00", timestamp=SEED_STAMP).to_fields()) for i in range(1, 12)])
    waits = []
    settings = fast_settings(tmp_path, read_pace_seconds=0.2)
    assert ScaleClient(conf(fake), settings, sleep=waits.append).read_items().ok
    assert waits.count(0.2) == fake.kinds().count("Ok") == 3


# ---------------------------------------------------------------- writes in scale.py


def test_write_sequence_after_identity(tmp_path, fake_scale):
    items = [Item(i, f"{2000+i}", f"{2000+i}", f"T {i}", "1.00", timestamp=SEED_STAMP) for i in range(1, 8)]
    report = ScaleClient(conf(fake_scale), fast_settings(tmp_path)).send_items(items)
    assert report.status is ScaleStatus.ACCEPTED_NOT_VERIFIED
    assert fake_scale.kinds() == ["CX", "RI", "bX01XX", "GP", "GP", "GPF0"]
    assert [len(m.records) for m in fake_scale.messages_of("GP")] == [5, 2]


def test_stall_is_unknown_and_never_resent(tmp_path, fake_scale):
    fake_scale.stall("GP", times=None)
    report = ScaleClient(conf(fake_scale), fast_settings(tmp_path)).send_items(seed_items()[:1])
    assert report.status is ScaleStatus.UNKNOWN and report.attempts == 1
    assert len(fake_scale.messages_of("GP")) == 1


def test_error_on_a_mixed_batch_is_unknown_and_on_one_record_is_rejected(tmp_path, fake_scale):
    fake_scale.force_error("GP", times=None)
    settings = fast_settings(tmp_path)
    mixed = ScaleClient(conf(fake_scale), settings).send_items(seed_items()[:2])
    assert {o.status for o in mixed.items} == {"unknown"} and all(o.said_error for o in mixed.items)
    one = ScaleClient(conf(fake_scale), settings).send_items(seed_items()[:1])
    assert one.status is ScaleStatus.REJECTED


def test_stall_on_the_opening_does_not_repeat_the_opening(tmp_path, fake_scale):
    fake_scale.stall("bX01XX", times=None)
    report = ScaleClient(conf(fake_scale), fast_settings(tmp_path, connect_attempts=4)).send_items(seed_items()[:1])
    assert report.status is ScaleStatus.UNREACHABLE and report.attempts == 0
    assert fake_scale.kinds().count("bX01XX") == 1


def test_connect_waits_grow(tmp_path):
    fake = FakeScale(post_write_refusal_seconds=0.0).start()
    port = fake.port
    fake.stop()
    waits = []
    settings = fast_settings(tmp_path, connect_attempts=4, connect_retry_wait_seconds=5.0, connect_timeout_seconds=0.2)
    client = ScaleClient(ScaleConfig("A", "127.0.0.1", port, True, serial="X"), settings, sleep=waits.append)
    assert client.identify().status is ScaleStatus.UNREACHABLE
    assert waits == [5.0, 10.0, 20.0]


@pytest.mark.parametrize("step", ["GP", "bX01XX", "GPF0"])
def test_a_surprise_settings_reply_is_never_kept(tmp_path, fake_scale, step):
    surprise = chained(b"LG01\tpw-like-value\t\n", 1)
    fake_scale.raw_reply(surprise, step, times=None)
    report = ScaleClient(conf(fake_scale), fast_settings(tmp_path)).send_items(seed_items()[:1])
    assert report.status in (ScaleStatus.UNKNOWN, ScaleStatus.UNREACHABLE)
    assert "pw-like-value" not in repr(report)


def test_a_surprise_settings_reply_to_a_read_goes_nowhere(make_service, make_fake, tmp_path):
    fake = loaded(make_fake())
    fake.raw_reply(chained(b"LG01\tpw-like-value\t\n", 1), "LP", times=None)
    service = make_service([conf(fake)])
    record = apply_edit(service)
    assert record.scale_results[0].status == "read_failed"
    text = json.dumps(record.to_dict()) + json.dumps(service._catalog.read_log(record.change_id))
    assert "pw-like-value" not in text
    assert service.compare_scales()[0].status == "read_failed"
    state = tmp_path / "catalog.sqlite3"
    assert b"pw-like-value" not in state.read_bytes()


# ---------------------------------------------------------------- the seven steps


def test_a_clean_edit_is_verified_and_keeps_every_other_field(make_service, make_fake):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    record = apply_edit(service)
    assert record.scale_results[0].status == "verified"
    assert record.success and record.verified and record.outcome == "success"
    assert fake.kinds()[:3] == ["CX", "LP", "Ok"] or fake.kinds()[:2] == ["CX", "LP"]
    assert fake.item(1)[5] == " " and fake.item(1)[4] == "10.50"
    view = service.get_item(plu="1001")
    assert view.item.price == "10.50"
    assert view.scales["A"].belief == "present"
    assert view.scales["A"].last_read.price == "10.50"
    assert view.scales["A"].last_read_at


def test_read_back_waits_out_the_busy_gap(make_service, make_fake):
    fake = loaded(make_fake(post_write_refusal_seconds=1.0))
    assert apply_edit(make_service([conf(fake)])).scale_results[0].status == "verified"


def test_read_back_through_a_silent_gap(make_service, make_fake, tmp_path):
    fake = loaded(make_fake(post_write_refusal_seconds=0.6, post_write_mode="silent"))
    settings = fast_settings(tmp_path, read_back_window_seconds=5.0)
    assert apply_edit(make_service([conf(fake)], settings)).scale_results[0].status == "verified"


class _ReadBackReturns(ScaleClient):
    """A scale whose read back gives something chosen by the test."""

    replacement = None

    def read_items(self, up_to_id=None, *, patient=False):
        result = super().read_items(up_to_id, patient=patient)
        if patient:
            return type(self).replacement(result)
        return result


def _factory(replacement):
    return type("Client", (_ReadBackReturns,), {"replacement": staticmethod(replacement)})


def test_read_back_that_differs_is_mismatch_and_says_what_the_scale_holds(make_service, make_fake):
    def changed(result):
        items = dict(result.items)
        items[1] = items[1].replace(price="99.99")
        return ReadResult(True, None, "read", items)

    fake = loaded(make_fake())
    service = make_service([conf(fake)], scale_factory=_factory(changed))
    record = apply_edit(service)
    result = record.scale_results[0]
    assert result.status == "mismatch" and not record.success
    assert result.mismatches[0].fields["price"].after == "99.99"
    assert result.mismatches[0].fields["price"].before == "10.50"
    assert record.outcome == "unknown"


def test_no_read_back_is_accepted_not_verified_and_only_partial(make_service, make_fake):
    fake = loaded(make_fake())
    gone = lambda result: ReadResult(False, ScaleStatus.READ_FAILED, "no read", {})  # noqa: E731
    service = make_service([conf(fake)], scale_factory=_factory(gone))
    record = apply_edit(service)
    assert record.scale_results[0].status == "accepted_not_verified"
    assert record.outcome == "partial" and not record.success and not record.verified
    assert "confirmed" not in record.display.lower().replace("not confirmed", "")
    assert service.get_item(plu="1001").scales["A"].belief == "unknown"


def test_wrong_serial_writes_nothing(make_service, make_fake):
    fake = loaded(make_fake(serial="OTHER"))
    cfg = ScaleConfig("A", "127.0.0.1", fake.port, True, serial="EXPECTED")
    record = apply_edit(make_service([cfg]))
    assert record.scale_results[0].status == "wrong_scale"
    assert fake.kinds() == ["CX"]


def test_read_failure_writes_nothing(make_service, make_fake):
    fake = loaded(make_fake())
    fake.stall_read(times=None)
    record = apply_edit(make_service([conf(fake)]))
    assert record.scale_results[0].status == "read_failed"
    assert not fake.messages_of("GP") and "RI" not in fake.kinds()
    assert record.outcome == "failed"


def test_drift_is_refused_named_and_then_stays_on_that_scale(make_service, make_fake):
    good, drifted = loaded(make_fake()), loaded(make_fake())
    other = seed_items()[0].replace(description="Scale Own Name")
    drifted.preload(tuple(other.to_fields()))
    service = make_service([conf(good, "GOOD"), conf(drifted, "DRIFT")])

    record = apply_edit(service, "10.50")
    by_scale = {r.scale: r for r in record.scale_results}
    assert by_scale["GOOD"].status == "verified"
    assert by_scale["DRIFT"].status == "drift_refused"
    assert not drifted.messages_of("GP")
    finding = by_scale["DRIFT"].drift[0]
    assert finding.kind == "differs" and finding.fields["description"].after == "Scale Own Name"
    assert "Scale Own Name" in finding.message
    assert record.outcome == "partial"

    # Asked for fresh, the preview names it too. A wrong token does not pass.
    wanted = [{"action": "edit", "target_plu": "1001", "price": "10.60"}]
    preview = service.preview_changes(wanted, scales=["DRIFT"], read_scales=True)
    assert preview.scales_read == ("DRIFT",)
    # The catalog moved to 10.50 too, so this scale differs in price as well.
    token = next(d for d in preview.drift if d.scale == "DRIFT").ack_token
    assert service.apply_change(preview.preview_id, True, acknowledge_drift=["DRIFT:1:nope"]).scale_results[0].status == "drift_refused"
    assert not drifted.messages_of("GP")

    preview = service.preview_changes(wanted, scales=["DRIFT"])
    record = service.apply_change(preview.preview_id, True, acknowledge_drift=[token])
    assert record.scale_results[0].status == "verified"
    # The scale's own name stayed. Only the price moved.
    assert drifted.item(1)[3] == "Scale Own Name" and drifted.item(1)[4] == "10.60"
    assert good.item(1)[3] == "Test Alpha"
    assert record.outcome == "partial" and record.not_touched == ("GOOD",)
    assert not record.success and "GOOD" in record.summary


def test_changed_scale_record_voids_an_old_token(make_service, make_fake):
    fake = loaded(make_fake())
    fake.preload(tuple(seed_items()[0].replace(price="11.00").to_fields()))
    service = make_service([conf(fake)])
    token = service.preview_changes([{"action": "edit", "target_plu": "1001", "description": "New"}], read_scales=True).drift[0].ack_token
    fake.preload(tuple(seed_items()[0].replace(price="12.00").to_fields()))
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "description": "New"}])
    record = service.apply_change(preview.preview_id, acknowledge_drift=[token])
    assert record.scale_results[0].status == "drift_refused"
    assert fake.item(1)[4] == "12.00"


def test_a_record_the_scale_holds_that_cannot_be_sent_is_refused(make_service, make_fake):
    fake = loaded(make_fake())
    fake.preload(tuple(seed_items()[0].replace(price="1000.00").to_fields()))
    service = make_service([conf(fake)])
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "description": "New"}], read_scales=True)
    record = service.apply_change(preview.preview_id, acknowledge_drift=[preview.drift[0].ack_token])
    assert record.scale_results[0].status == "drift_refused"
    assert not fake.messages_of("GP")


def test_add_is_refused_if_the_id_or_plu_is_taken_on_that_scale(make_service, make_fake):
    fake = loaded(make_fake())
    stranger = Item(4, "7777", "7777", "Stranger", "3.33", timestamp=SEED_STAMP)
    fake.preload(tuple(stranger.to_fields()))
    service = make_service([conf(fake)])
    wanted = [{"action": "add", "plu": "4321", "description": "New", "price": "1.00"}]
    preview = service.preview_changes(wanted)
    record = service.apply_change(preview.preview_id, acknowledge_drift=["anything"])
    result = record.scale_results[0]
    assert result.status == "drift_refused" and result.drift[0].kind == "id_occupied"
    assert result.drift[0].ack_token is None
    assert fake.item(4)[1] == "7777" and not fake.messages_of("GP")

    fake2 = loaded(make_fake())
    fake2.preload(tuple(Item(9, "4321", "4321", "Elsewhere", "3.33", timestamp=SEED_STAMP).to_fields()))
    record = apply_add(make_service([conf(fake2, "B")], fast_settings(service._settings.state_dir, catalog_path=service._settings.catalog_path)), "4321")
    assert record.scale_results[0].drift[0].kind == "plu_on_other_id"
    assert not fake2.messages_of("GP")


def apply_add(service, plu, **kw):
    preview = service.preview_changes([{"action": "add", "plu": plu, "description": "New", "price": "1.00", **kw}])
    assert preview.ok, preview.errors
    return service.apply_change(preview.preview_id, True)


def test_add_may_replace_a_recorded_orphan_only_when_chosen(make_service, make_fake):
    fake = loaded(make_fake())
    fake.preload(tuple(Item(9, "9991", "9991", "Old Test", "1.00", timestamp=SEED_STAMP).to_fields()))
    service = make_service([conf(fake)])
    service.mark_orphan(9, "A", "test item")
    preview = service.preview_changes([{"action": "add", "plu": "4321", "description": "New", "price": "1.00", "replace_orphan_id": 9}])
    assert "orphan_replaced" in {w.code for w in preview.warnings}
    with pytest.raises(AcknowledgementRequired):
        service.apply_change(preview.preview_id)
    record = service.apply_change(preview.preview_id, True)
    assert record.scale_results[0].status == "verified"
    assert fake.item(9)[2] == "4321" and service.get_item(plu="4321").item.id == 9
    assert 9 not in service._catalog.orphan_ids()
    bad = service.preview_changes([{"action": "add", "plu": "4322", "description": "New", "price": "1.00", "replace_orphan_id": 2}])
    assert "invalid_id" in {e.code for e in bad.errors}


def test_add_sets_sold_by_and_defaults(make_service, make_fake):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    assert apply_add(service, "3000", sold_by="piece").success
    stored = fake.item(4)
    assert stored[1:6] == ("3000", "3000", "New", "1.00", "")
    assert stored[6:12] == ("0", "1", "0", "0", "0", "0") and stored[13:] == ("0",) * 7
    assert apply_add(service, "3001").success
    assert fake.item(5)[6] == "1"
    assert "invalid_field" in {e.code for e in service.preview_changes([{"action": "add", "plu": "3002", "description": "N", "price": "1.00", "sold_by": "gram"}]).errors}


def test_full_read_on_add_and_first_write(make_service, make_fake, tmp_path):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    apply_edit(service, "10.50")
    dumps = list((tmp_path / "backups").glob("*scale-dump.json"))
    assert len(dumps) == 1
    saved = json.loads(dumps[0].read_text(encoding="utf-8"))
    assert [i["plu"] for i in saved] == ["1001", "1002", "1004"]
    apply_edit(service, "10.60")
    assert len(list((tmp_path / "backups").glob("*scale-dump.json"))) == 1


# ---------------------------------------------------------------- outcomes and flags


def test_subset_never_says_success_and_names_what_was_left(make_service, make_fake):
    a, b = loaded(make_fake()), loaded(make_fake())
    service = make_service([conf(a, "A"), conf(b, "B")])
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "10.50"}], scales=["A"])
    with pytest.raises(AcknowledgementRequired):
        service.apply_change(preview.preview_id)
    record = service.apply_change(preview.preview_id, True)
    assert b.nothing_received
    assert record.verified and not record.success and record.not_touched == ("B",)
    assert "every scale" not in record.summary.lower() and "Not touched: B" in record.summary


def test_a_scale_off_keeps_it_partial(make_service, make_fake):
    on, off = loaded(make_fake()), loaded(make_fake())
    service = make_service([conf(on, "ON"), conf(off, "OFF", enabled=False)])
    record = apply_edit(service)
    assert [r.status for r in record.scale_results] == ["verified", "skipped_not_write_enabled"]
    assert record.outcome == "partial" and off.nothing_received


def test_continues_after_failure_in_config_order(make_service, make_fake):
    first, second, third = loaded(make_fake()), make_fake(), loaded(make_fake())
    first.force_error("GP")
    dead = conf(second, "TWO")
    second.stop()
    record = apply_edit(make_service([conf(first, "ONE"), dead, conf(third, "THREE")]))
    assert [(r.scale, r.status) for r in record.scale_results] == [
        ("ONE", "rejected"),
        ("TWO", "unreachable"),
        ("THREE", "verified"),
    ]
    assert third.item(1)[4] == "10.50"


def test_a_preview_goes_stale_if_a_flag_changed(make_service, make_fake, tmp_path):
    fake = loaded(make_fake())
    path = tmp_path / "live.toml"

    def write(flag):
        path.write_text(
            f'[settings]\nstate_dir = "{tmp_path.as_posix()}"\nscalelink_db_copy = "not_available_here"\n'
            f'read_pace_seconds = 0\n\n[[scales]]\nname = "A"\naddress = "127.0.0.1"\nport = {fake.port}\n'
            f'serial = "{fake.serial}"\nwrite_enabled = {flag}\n',
            encoding="utf-8",
        )

    write("true")
    catalog = Catalog(load_config(path).settings.catalog_path)
    catalog.import_items(seed_items())
    catalog.close()
    service = ScaleConnectService.open(path)
    try:
        preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "10.50"}])
        write("false")
        with pytest.raises(PreviewStale):
            service.apply_change(preview.preview_id)
        assert service.list_scales()[0].write_enabled is False
        assert not fake.messages_of("GP") and fake.nothing_received
    finally:
        service.close()


def test_preview_rules(make_service, make_fake):
    fake = loaded(make_fake())
    now = [datetime(2026, 1, 1, 12, 0, 0).astimezone()]
    service = make_service([conf(fake)], clock=lambda: now[0])
    with pytest.raises(PreviewNotFound):
        service.apply_change("nope")
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "10.50"}])
    now[0] += timedelta(seconds=901)
    with pytest.raises(PreviewExpired):
        service.apply_change(preview.preview_id)
    assert fake.nothing_received

    now[0] = datetime(2026, 1, 1, 12, 0, 0).astimezone()
    stale = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "10.60"}])
    fresh = service.preview_changes([{"action": "edit", "target_plu": "1002", "price": "5.60"}])
    assert service.apply_change(fresh.preview_id).success
    with pytest.raises(PreviewAlreadyApplied):
        service.apply_change(fresh.preview_id)
    before = len(fake.messages_of("GP"))
    with pytest.raises(PreviewStale):
        service.apply_change(stale.preview_id)
    assert len(fake.messages_of("GP")) == before


@pytest.mark.parametrize("price,warned", [("15.00", False), ("15.01", True), ("5.00", False), ("4.99", True)])
def test_large_price_warning(make_service, make_fake, price, warned):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": price}])
    assert any(w.code == "price_change_large" and w.requires_ack for w in preview.warnings) is warned
    if warned:
        for value in (False, "yes", 1):
            with pytest.raises(AcknowledgementRequired):
                service.apply_change(preview.preview_id, value)
        assert fake.nothing_received
    assert service.apply_change(preview.preview_id, warned).success


def test_delete_writes_a_placeholder_and_reads_it_back(make_service, make_fake):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    assert service.capabilities().delete is True and service.capabilities().read is True
    preview = service.preview_changes([{"action": "delete", "target_plu": "1001"}])
    assert "delete_placeholder" in {w.code for w in preview.warnings}
    record = service.apply_change(preview.preview_id, True)
    assert record.scale_results[0].status == "verified" and record.success
    assert fake.item(1)[1:5] == ("9001", "9001", "UNUSED", "0.01")
    with pytest.raises(Exception) as caught:
        service.get_item(plu="1001")
    assert caught.value.code == "item_not_found"


def test_mixed_edit_and_delete_reports_both(make_service, make_fake):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    preview = service.preview_changes(
        [{"action": "edit", "target_plu": "1001", "price": "10.50"}, {"action": "delete", "target_plu": "1002"}]
    )
    record = service.apply_change(preview.preview_id, True)
    result = record.scale_results[0]
    assert result.status == "verified"
    assert [i.status for i in result.items] == ["verified", "verified"]
    assert record.outcome == "success"


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "price",
    ["0.00", "-1.00", "1000.00", "9.9", "9", "9.999", "09.99", " 9.99", "9.99\n", "abc", "NaN", "9,99", "", 9.99, 10, True],
)
def test_bad_prices_refused(make_service, fake_scale, price):
    service = make_service([conf(fake_scale)])
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": price}])
    assert not preview.ok and preview.preview_id is None
    assert fake_scale.nothing_received


@pytest.mark.parametrize("price", ["0.01", "999.99", Decimal("12.50")])
def test_good_prices_allowed(make_service, fake_scale, price):
    service = make_service([conf(fake_scale)])
    assert service.preview_changes([Change(action="add", plu="3000", description="New", price=price)]).ok


def test_float_price_never_builds_an_item():
    with pytest.raises(TypeError):
        Item(id=1, code="1000", plu="1000", description="X", price=9.99)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"action": "add", "plu": "123", "description": "X", "price": "1.00"}, "invalid_plu"),
        ({"action": "add", "plu": "12345", "description": "X", "price": "1.00"}, "invalid_plu"),
        ({"action": "add", "plu": "4001\n", "description": "X", "price": "1.00"}, "invalid_plu"),
        ({"action": "add", "plu": "3000", "code": "3001", "description": "X", "price": "1.00"}, "code_plu_mismatch"),
        ({"action": "add", "plu": "1001", "description": "X", "price": "1.00"}, "duplicate_plu"),
        ({"action": "add", "plu": "3000", "description": "   ", "price": "1.00"}, "invalid_description"),
        ({"action": "add", "plu": "3000", "description": "A\tB", "price": "1.00"}, "invalid_description"),
        ({"action": "add", "plu": "3000", "description": "Caf\u00e9", "price": "1.00"}, "invalid_description"),
        ({"action": "add", "plu": "3000", "description": "N" * 36, "price": "1.00"}, "invalid_description"),
        ({"action": "add", "plu": "3000", "description": "Trailing ", "price": "1.00"}, "invalid_description"),
        ({"action": "add", "plu": "3000", "description": "X", "price": "1.00", "fields_7_12": ["1"] * 6}, "invalid_change"),
        ({"action": "edit", "target_plu": "9999", "price": "1.00"}, "item_not_found"),
        ({"action": "edit", "target_plu": "1001"}, "invalid_change"),
        ({"action": "edit", "target_plu": "1001", "plu": "199"}, "invalid_plu"),
        ({"action": "edit", "target_plu": "1001", "code": "1998", "plu": "1999"}, "code_plu_mismatch"),
        ({"action": "edit", "target_plu": "1001", "plu": "1002"}, "duplicate_plu"),
        ({"action": "edit", "target_plu": "1001", "plu": "9500"}, "reserved_plu"),
        ({"action": "edit", "target_plu": "1001", "code": "1009"}, "code_plu_mismatch"),
        ({"action": "edit", "target_plu": "1001", "sold_by": "piece"}, "invalid_change"),
        ({"action": "move", "target_plu": "1001"}, "invalid_action"),
        ({"action": "edit", "target_plu": "1001", "bogus": "1"}, "invalid_change"),
        ({"action": "edit", 7: "1"}, "invalid_change"),
    ],
)
def test_validation_codes(make_service, fake_scale, change, code):
    service = make_service([conf(fake_scale)])
    preview = service.preview_changes([change])
    assert not preview.ok
    assert code in {e.code for e in preview.errors}
    assert fake_scale.nothing_received


@pytest.mark.parametrize("target", [True, 1.0, "1"])
def test_odd_target_id_is_refused(make_service, fake_scale, target):
    service = make_service([conf(fake_scale)])
    assert not service.preview_changes([Change(action="edit", target_id=target, price="11.00")]).ok


def test_duplicates_within_one_preview(make_service, fake_scale):
    service = make_service([conf(fake_scale)])
    both = [
        {"action": "add", "plu": "3000", "description": "One", "price": "1.00"},
        {"action": "add", "plu": "3000", "description": "Two", "price": "1.00"},
    ]
    assert "duplicate_plu" in {e.code for e in service.preview_changes(both).errors}
    twice = [{"action": "edit", "target_plu": "1001", "price": "10.10"}, {"action": "edit", "target_id": 1, "price": "10.20"}]
    assert "duplicate_target" in {e.code for e in service.preview_changes(twice).errors}


# ---------------------------------------------------------------- backup


def test_backup_only_once_and_redone_if_the_folder_is_gone(make_service, make_fake, tmp_path):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    apply_edit(service, "10.50")
    apply_edit(service, "10.60")
    folders = [p for p in (tmp_path / "backups").iterdir() if p.is_dir()]
    assert len(folders) == 1
    assert json.loads((folders[0] / "items.json").read_text(encoding="utf-8"))[0]["price"] == "10.00"
    import shutil

    shutil.rmtree(folders[0])
    apply_edit(service, "10.70")
    assert len([p for p in (tmp_path / "backups").iterdir() if p.is_dir()]) == 1


def test_old_marker_does_not_count(make_service, make_fake, tmp_path):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "first-write-items-A.marker").write_text("2026-01-01 somewhere\n", encoding="utf-8")
    apply_edit(service)
    assert [p for p in (tmp_path / "backups").iterdir() if p.is_dir()]


def test_backup_failure_stops_everything_before_contact(make_service, make_fake):
    fake = loaded(make_fake())

    def hook(scale, items, backup_dir):
        raise OSError("disk full")

    service = make_service([conf(fake)], backup_hook=hook)
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "10.50"}])
    with pytest.raises(BackupFailed):
        service.apply_change(preview.preview_id)
    assert fake.nothing_received
    assert service.list_changes()[0].outcome == "failed"


def test_no_scalelink_plan_stops_the_first_write(make_service, make_fake, tmp_path):
    fake = loaded(make_fake())
    service = make_service([conf(fake)], fast_settings(tmp_path, scalelink_db_copy=None))
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "10.50"}])
    with pytest.raises(BackupFailed):
        service.apply_change(preview.preview_id)
    assert fake.nothing_received


def test_no_backup_for_a_scale_with_writes_off(make_service, fake_scale):
    calls = []
    service = make_service([conf(fake_scale, enabled=False)], backup_hook=lambda *a: calls.append(a))
    apply_edit(service)
    assert calls == [] and fake_scale.nothing_received


# ---------------------------------------------------------------- crash and log


def test_interrupted_change_blocks_those_items_until_resolved(make_service, make_fake, monkeypatch):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "10.50"}])

    def die(item):
        raise KeyboardInterrupt

    monkeypatch.setattr(service._catalog, "upsert_item", die)
    with pytest.raises(KeyboardInterrupt):
        service.apply_change(preview.preview_id)
    monkeypatch.undo()

    change_id = service._catalog.open_change_ids()[0]
    assert service.get_change(change_id).outcome == "in_progress"
    assert service.list_scales()[0].interrupted_change_ids == (change_id,)
    assert service.get_item(plu="1001").interrupted_change_ids == (change_id,)
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "description": "New"}])
    assert "interrupted_change" in {w.code for w in preview.warnings}
    with pytest.raises(InterruptedChange):
        service.apply_change(preview.preview_id)
    # Another item is not held up.
    assert apply_edit(service, "5.60", plu="1002").scale_results[0].status == "verified"

    assert service.resolve_interrupted_change(change_id, "Checked.", requested_by="test").outcome == "interrupted"
    assert service.list_scales()[0].interrupted_change_ids == ()
    with pytest.raises(ChangeNotFound):
        service.resolve_interrupted_change(change_id, "Again.")
    # The scale kept 10.50 and the catalog says 10.00, so the next write names that.
    record = apply_edit(service, "10.80")
    assert record.scale_results[0].status == "drift_refused"
    assert fake.item(1)[4] == "10.50"


def test_catalog_failure_is_not_success(make_service, make_fake, monkeypatch):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "10.50"}])

    def boom(item):
        raise RuntimeError("locked")

    monkeypatch.setattr(service._catalog, "upsert_item", boom)
    record = service.apply_change(preview.preview_id)
    assert record.outcome == "catalog_out_of_sync" and not record.success


def test_get_change_matches_the_result_and_finds_by_preview(make_service, make_fake):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "10.50"}])
    assert service.get_change_for_preview(preview.preview_id) is None
    record = service.apply_change(preview.preview_id)
    assert service.get_change(record.change_id).to_dict() == json.loads(json.dumps(record.to_dict()))
    assert service.get_change_for_preview(preview.preview_id).change_id == record.change_id
    assert service.list_changes()[0].change_id == record.change_id
    with pytest.raises(ChangeNotFound):
        service.get_change("nope")
    info = service.list_scales()[0]
    assert info.last_status == "verified" and info.last_change_id == record.change_id


# ---------------------------------------------------------------- compare


def test_compare_lists_differences_and_writes_nothing(make_service, make_fake):
    fake = loaded(make_fake())
    fake.preload(
        tuple(seed_items()[0].replace(price="11.00").to_fields()),
        tuple(Item(9, "9991", "9991", "Old Test", "1.00", timestamp=SEED_STAMP).to_fields()),
    )
    off = make_fake()
    service = make_service([conf(fake), conf(off, "OFF", enabled=False)])
    found, skipped = service.compare_scales()
    assert found.status == "compared" and found.items_read == 4
    assert {(d.kind, d.item_id) for d in found.differences} == {("differs", 1), ("not_in_catalog", 9)}
    assert next(d for d in found.differences if d.kind == "differs").fields["price"].after == "11.00"
    assert skipped.status == "not_contacted" and off.nothing_received
    assert set(fake.kinds()) <= {"CX", "LP", "Ok"}
    assert service.get_item(plu="1001").scales["A"].last_read.price == "11.00"


def test_compare_on_wrong_serial_reads_nothing(make_service, make_fake):
    fake = loaded(make_fake(serial="OTHER"))
    result = make_service([ScaleConfig("A", "127.0.0.1", fake.port, True, serial="EXPECTED")]).compare_scales()[0]
    assert result.status == "wrong_scale" and fake.kinds() == ["CX"]


# ---------------------------------------------------------------- seeding


def test_service_refuses_an_unseeded_catalog_and_never_creates_one(tmp_path):
    settings = fast_settings(tmp_path)
    catalog = Catalog(settings.catalog_path)
    with pytest.raises(CatalogNotSeeded):
        ScaleConnectService(Config(settings=settings), catalog)
    catalog.close()
    path = tmp_path / "other" / "config.toml"
    path.parent.mkdir()
    db = tmp_path / "other" / "catalog.sqlite3"
    path.write_text(f'[settings]\ncatalog_path = "{db.as_posix()}"\n', encoding="utf-8")
    with pytest.raises(CatalogNotSeeded):
        ScaleConnectService.open(path)
    assert not db.exists()


def test_seed_and_service_share_one_path_rule(tmp_path):
    db = tmp_path / "chosen" / "catalog.sqlite3"
    path = tmp_path / "config.toml"
    path.write_text(f'[settings]\ncatalog_path = "{db.as_posix()}"\nstate_dir = "{tmp_path.as_posix()}"\n', encoding="utf-8")
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"items": [i.to_dict() for i in seed_items()]}), encoding="utf-8")
    assert catalog_path_for(path) == db
    assert seed_catalog(None, seed, config_path=path)["path"] == str(db)
    service = ScaleConnectService.open(path)
    assert service.search_items("1001")[0].id == 1
    assert service.list_changes() == []  # the seeding note is not a change
    service.close()


# ---------------------------------------------------------------- config and CLI


def write_config(tmp_path, fake, enabled="true", extra=""):
    state = (tmp_path / "state").as_posix()
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[settings]
state_dir = "{state}"
reply_timeout_seconds = 1
connect_timeout_seconds = 1
connect_attempts = 1
connect_retry_wait_seconds = 0
read_pace_seconds = 0
scalelink_db_copy = "not_available_here"
{extra}
[auth]
pin = "secret-pin-value"
mcp_token = "secret-token-value"

[[scales]]
name = "A"
address = "127.0.0.1"
port = {fake.port if fake else 50036}
serial = "{fake.serial if fake else 'S'}"
write_enabled = {enabled}
""",
        encoding="utf-8",
    )
    return path


def test_config_flags_and_secrets(tmp_path):
    config = load_config(write_config(tmp_path, None, enabled='"true"'))
    assert config.scales[0].write_enabled is False and config.warnings
    assert "secret-pin-value" not in repr(config) and "secret-token-value" not in repr(config)
    assert config.pin == "secret-pin-value"
    path = tmp_path / "noflag.toml"
    path.write_text('[[scales]]\nname = "A"\naddress = "192.0.2.10"\n', encoding="utf-8")
    scale = load_config(path).scales[0]
    assert scale.write_enabled is False and scale.read_enabled is False


@pytest.mark.parametrize("flag", ["write_enabled", "read_enabled"])
def test_an_enabled_scale_needs_a_serial(tmp_path, flag):
    path = tmp_path / "c.toml"
    path.write_text(f'[[scales]]\nname = "A"\naddress = "192.0.2.10"\n{flag} = true\n', encoding="utf-8")
    with pytest.raises(Exception) as caught:
        load_config(path)
    assert caught.value.code == "config_error"


def test_two_entries_cannot_share_an_address(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[[scales]]\nname = "A"\naddress = "192.0.2.10"\n\n[[scales]]\nname = "B"\naddress = "192.0.2.10"\n',
        encoding="utf-8",
    )
    with pytest.raises(Exception) as caught:
        load_config(path)
    assert caught.value.code == "config_error"


@pytest.mark.parametrize("bad", ["nan", "inf"])
def test_odd_numbers_refused(tmp_path, bad):
    path = tmp_path / "c.toml"
    path.write_text(f"[settings]\nreply_timeout_seconds = {bad}\n", encoding="utf-8")
    with pytest.raises(Exception) as caught:
        load_config(path)
    assert caught.value.code == "config_error"


def test_short_reply_wait_warns_and_big_batches_are_refused(tmp_path):
    assert any("reply wait" in w for w in load_config(write_config(tmp_path, None)).warnings)
    with pytest.raises(Exception):
        Settings(batch_size=6)


def test_example_config_loads_with_nothing_on(tmp_path):
    copy = tmp_path / "config.toml"
    copy.write_text((ROOT / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(copy)
    assert not any(s.write_enabled or s.read_enabled for s in config.scales)
    assert all(s.address.startswith("192.0.2.") for s in config.scales)
    assert config.pin is None and config.mcp_token is None


def test_other_config_cannot_reach_a_real_address(tmp_path):
    path = tmp_path / "temp.toml"
    path.write_text(
        '[[scales]]\nname = "REAL"\naddress = "192.0.2.10"\nserial = "S1"\nwrite_enabled = true\n\n'
        '[[scales]]\nname = "LOCAL"\naddress = "127.0.0.1"\nserial = "S2"\nwrite_enabled = true\n',
        encoding="utf-8",
    )
    real, local = restrict_unofficial(load_config(path)).scales
    assert real.write_enabled is False and local.write_enabled is True


def run_cli(capsys, *args):
    code = cli.main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_cli_flow_and_exit_codes(tmp_path, make_fake, capsys):
    fake = loaded(make_fake())
    path = str(write_config(tmp_path, fake))
    with Catalog(load_config(path).settings.catalog_path) as catalog:
        catalog.import_items(seed_items())

    code, out = run_cli(capsys, "--config", path, "capabilities")
    assert (code, out["delete"], out["read"]) == (0, True, True)
    code, out = run_cli(capsys, "--config", path, "scales")
    assert code == 0 and out[0]["name"] == "A" and "address" not in out[0] and "serial" not in out[0]
    code, out = run_cli(capsys, "--config", path, "search", "1001")
    assert code == 0 and out[0]["plu"] == "1001"
    code, out = run_cli(capsys, "--config", path, "get", "--plu", "1001")
    assert code == 0 and out["item"]["id"] == 1
    code, out = run_cli(capsys, "--config", path, "get", "--plu", "9999")
    assert code == 4 and out["error"]["code"] == "item_not_found"
    code, out = run_cli(capsys, "--config", path, "compare")
    assert code == 0 and out[0]["status"] == "compared"

    code, out = run_cli(capsys, "--config", path, "preview", "--json", '[{"action":"edit","target_plu":"1001","price":9.99}]')
    assert code == 3 and not out["ok"]
    code, out = run_cli(capsys, "--config", path, "preview", "--json", "not json")
    assert code == 3

    code, out = run_cli(capsys, "--config", path, "preview", "--read", "--json", '[{"action":"edit","target_plu":"1001","price":"20.00"}]')
    assert code == 0 and out["ok"] and out["scales_read"] == ["A"] and out["drift"] == []
    preview_id = out["preview_id"]
    code, out = run_cli(capsys, "--config", path, "apply", preview_id)
    assert code == 6 and out["error"]["code"] == "acknowledgement_required"
    code, out = run_cli(capsys, "--config", path, "apply", preview_id, "--acknowledge-warnings", "--who", "tester")
    assert code == 0 and out["success"] and out["verified"] and out["requested_by"] == "tester"
    change_id = out["change_id"]
    code, out = run_cli(capsys, "--config", path, "apply", preview_id)
    assert code == 5 and out["error"]["code"] == "preview_already_applied"
    code, out = run_cli(capsys, "--config", path, "change", change_id)
    assert code == 0 and out["outcome"] == "success"
    code, out = run_cli(capsys, "--config", path, "changes")
    assert code == 0 and len(out) == 1

    fake.force_error("GP")
    code, out = run_cli(capsys, "--config", path, "preview", "--json", '[{"action":"edit","target_plu":"1002","price":"5.60"}]')
    code, out = run_cli(capsys, "--config", path, "apply", out["preview_id"])
    assert code == 7 and out["scale_results"][0]["status"] == "rejected"

    fake.preload(tuple(seed_items()[2].replace(price="9.00").to_fields()))
    code, out = run_cli(capsys, "--config", path, "compare")
    assert code == 7
    code, out = run_cli(capsys, "--config", path, "preview", "--read", "--json", '[{"action":"edit","target_plu":"1004","description":"New"}]')
    token = out["drift"][0]["ack_token"]
    code, out = run_cli(capsys, "--config", path, "apply", out["preview_id"], "--acknowledge-drift", token)
    # Stage F M4: confirmed, and the scale still holds its own price, so it is not a full success.
    assert code == 7 and out["scale_results"][0]["status"] == "verified" and out["still_differ"][0]["fields"]["price"]["after"] == "9.00"
    assert fake.item(3)[4] == "9.00" and fake.item(3)[3] == "New"


def test_cli_config_error_and_usage(tmp_path, capsys):
    code, out = run_cli(capsys, "--config", str(tmp_path / "missing.toml"), "scales")
    assert code == 8 and out["error"]["code"] == "config_error"
    with pytest.raises(SystemExit) as caught:
        cli.main(["nonsense"])
    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_cli_unseeded_exit_code(tmp_path, capsys):
    code, out = run_cli(capsys, "--config", str(write_config(tmp_path, None)), "scales")
    assert code == 8 and out["error"]["code"] == "catalog_not_seeded"


def test_cli_runs_as_a_module(tmp_path):
    path = write_config(tmp_path, None, enabled="false")
    with Catalog(load_config(path).settings.catalog_path) as catalog:
        catalog.import_items(seed_items())
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    env.pop("SCALECONNECT_CONFIG", None)
    done = subprocess.run(
        [sys.executable, "-m", "scaleconnect.cli", "--config", str(path), "capabilities"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["read"] is True
    assert "secret-pin-value" not in done.stdout + done.stderr
