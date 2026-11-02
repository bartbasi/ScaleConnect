"""The real server process, over real Streamable HTTP and real stdio, on localhost only.

It starts `python -m scaleconnect.mcp_server` with a config in a temp folder that names
only fake scales on 127.0.0.1, and connects with the SDK's own client.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx
import httpx2
import pytest
from mcp import Client, MCPError, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp_types import Implementation

from fake_scale import FakeScale
from scaleconnect.mcp_server import main
from scaleconnect.service import ScaleConnectService
from service_env import SEED, assert_wire_clean, seeded_catalog_file

TOKEN = "made-up-test-token-not-a-real-one-7f3a"
SRC = str(Path(__file__).resolve().parents[2] / "src")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_config(folder: Path, fakes: dict[str, FakeScale | None], *, token: str | None = TOKEN, host="127.0.0.1", port=None,
                 extra_auth: str = "") -> Path:
    state = folder / "state"
    lines = [
        "[settings]",
        f'state_dir = "{state.as_posix()}"',
        f'catalog_path = "{(folder / "catalog.sqlite3").as_posix()}"',
        f'backup_dir = "{(folder / "backups").as_posix()}"',
        'scalelink_db_copy = "not_available_here"',
        "read_pace_seconds = 0",
        "reply_timeout_seconds = 5",
        "connect_attempts = 1",
        "connect_retry_wait_seconds = 0",
        "read_back_retry_seconds = 0.1",
        "",
        "[auth]",
    ]
    if token is not None:
        lines.append(f'mcp_token = "{token}"')
    if extra_auth:
        lines.append(extra_auth)
    lines += ["", "[mcp]", f'host = "{host}"', f"port = {port or free_port()}", ""]
    for name, fake in fakes.items():
        if fake is None:
            lines += ["[[scales]]", f'name = "{name}"', 'address = "127.0.0.1"', "port = 1", "write_enabled = false", ""]
        else:
            lines += [
                "[[scales]]",
                f'name = "{name}"',
                'address = "127.0.0.1"',
                f"port = {fake.port}",
                f'serial = "{fake.serial}"',
                "write_enabled = true",
                "",
            ]
    path = folder / "config.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env.pop("SCALECONNECT_CONFIG", None)
    return env


@pytest.fixture(scope="module")
def live(tmp_path_factory, template_root):
    """One running server process and two fake scales, shared by the HTTP tests."""
    folder = tmp_path_factory.mktemp("mcp-http")
    fakes = {name: FakeScale(post_write_refusal_seconds=0.0, serial=f"TEST-{name}").start() for name in ("A", "B")}
    for fake in fakes.values():
        fake.preload(*[tuple(i.to_fields()) for i in SEED])
    shutil.copy(seeded_catalog_file(template_root, SEED), folder / "catalog.sqlite3")
    port = free_port()
    config = write_config(folder, fakes, port=port, extra_auth="")
    log_path = folder / "server.log"
    log_file = open(log_path, "wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "scaleconnect.mcp_server", "--http", "--config", str(config)],
        env=child_env(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert process.poll() is None, log_path.read_text(errors="replace")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("the server did not start listening")
        yield type("Live", (), {"url": f"http://127.0.0.1:{port}/mcp", "port": port, "config": config, "fakes": fakes, "log": log_path, "process": process})
    finally:
        process.terminate()
        process.wait(timeout=30)
        log_file.close()
        for fake in fakes.values():
            fake.stop()
            fake.assert_clean()
            assert_wire_clean(fake)


def connect(url: str, token: str | None = TOKEN, **client_kw) -> tuple[Client, object]:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else None
    http = create_mcp_http_client(headers=headers)
    return Client(streamable_http_client(url, http_client=http), **client_kw), http


async def with_client(url, work, token=TOKEN, **client_kw):
    client, http = connect(url, token, **client_kw)
    async with http:
        async with client:
            return await work(client)


def post_initialize(url, headers):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    base = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    return httpx.post(url, json=body, headers={**base, **headers}, timeout=10)


GOOD = {"Authorization": f"Bearer {TOKEN}"}


# ---------- the end to end check ----------


@pytest.mark.parametrize("mode, price", [("legacy", "11.00"), ("auto", "12.00")])
def test_end_to_end_over_real_http(live, mode, price):
    """Connect, list tools, preview, apply. The fake scale has to show the new price."""

    async def work(client):
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["scale_apply_changes"].annotations.destructive_hint is True
        assert tools["scale_preview_changes"].annotations.read_only_hint is True
        scales = await client.call_tool("scale_list_scales", {})
        assert [s["name"] for s in scales.structured_content["scales"]] == ["A", "B"]
        preview = await client.call_tool(
            "scale_preview_changes", {"changes": [{"action": "edit", "target_plu": "1001", "price": price}]}
        )
        assert not preview.is_error, preview.content
        assert preview.structured_content["scales_read"] == ["A", "B"]
        applied = await client.call_tool("scale_apply_changes", {"preview_id": preview.structured_content["preview_id"]})
        return preview.structured_content, applied

    preview, applied = anyio.run(
        lambda: with_client(live.url, work, mode=mode, client_info=Implementation(name="e2e-client", version="1"))
    )
    data = applied.structured_content
    assert not applied.is_error
    assert data["success"] is True and data["outcome"] == "success"
    assert data["scale_statuses"] == {"A": "verified", "B": "verified"}
    assert data["requested_by"] == "mcp:e2e-client"
    assert [content.type for content in applied.content] == ["text", "text"]
    assert data["summary"] == applied.content[0].text
    assert json.loads(applied.content[1].text)["change_id"] == data["change_id"]
    for fake in live.fakes.values():
        assert fake.item(1)[4] == price


def test_an_add_over_http_reaches_the_scale(live):
    async def work(client):
        preview = await client.call_tool(
            "scale_preview_changes",
            {"changes": [{"action": "add", "plu": "4400", "description": "Http Item", "price": "2.25", "sold_by": "piece"}]},
        )
        return await client.call_tool("scale_apply_changes", {"preview_id": preview.structured_content["preview_id"]})

    applied = anyio.run(lambda: with_client(live.url, work))
    data = applied.structured_content
    assert data["success"] is True, data["summary"]
    item_id = data["changes"][0]["item_id"]
    for fake in live.fakes.values():
        record = fake.item(item_id)
        assert record[1:5] == ("4400", "4400", "Http Item", "2.25")
        assert record[6] == "0", "sold by the piece"


def test_the_catalog_is_shared_with_another_process(live):
    """Like the web page: a separate process on the same files. Its change shows here,
    and a preview made there can be applied here, because previews live in the catalog."""
    other = ScaleConnectService.open(live.config)
    try:
        preview = other.preview_changes(
            [{"action": "edit", "target_plu": "1002", "price": "22.00"}], requested_by="web:test"
        )
        assert preview.ok

        async def work(client):
            found = await client.call_tool("scale_get_change", {"preview_id": preview.preview_id})
            assert found.structured_content["applied"] is False
            return await client.call_tool("scale_apply_changes", {"preview_id": preview.preview_id})

        applied = anyio.run(lambda: with_client(live.url, work))
        assert applied.structured_content["success"] is True
        assert other.get_change_for_preview(preview.preview_id).change_id == applied.structured_content["change_id"]
        assert other.get_item(plu="1002").item.price == "22.00"
    finally:
        other.close()


@pytest.mark.parametrize("mode, price", [("legacy", "5.60"), ("auto", "5.70")])
def test_a_client_that_gives_up_does_not_stop_the_change_and_can_find_the_result(live, mode, price):
    """An apply can take minutes, and clients time out. It must finish and log, and the
    preview id must find it, as the tool description promises."""
    for fake in live.fakes.values():
        fake.delay_reply(1.5, kind="GP", times=1)

    async def give_up(client):
        preview = await client.call_tool(
            "scale_preview_changes", {"changes": [{"action": "edit", "target_plu": "1003", "price": price}]}
        )
        preview_id = preview.structured_content["preview_id"]
        with anyio.move_on_after(0.5) as scope:
            await client.call_tool("scale_apply_changes", {"preview_id": preview_id})
        assert scope.cancelled_caught, "the call should still have been running"
        return preview_id

    preview_id = anyio.run(lambda: with_client(live.url, give_up, mode=mode))

    async def look(client):
        return await client.call_tool("scale_get_change", {"preview_id": preview_id})

    deadline = time.monotonic() + 30
    data = None
    while time.monotonic() < deadline:
        data = anyio.run(lambda: with_client(live.url, look, mode=mode)).structured_content
        if data.get("outcome") not in (None, "in_progress"):
            break
        assert data.get("success") is False, "a change still running is never a success"
        time.sleep(0.2)
    assert data["applied"] is True
    assert data["success"] is True, data["summary"]
    assert data["scale_statuses"] == {"A": "verified", "B": "verified"}
    for fake in live.fakes.values():
        assert fake.item(3)[4] == price


def test_the_token_never_reaches_the_server_log(live):
    anyio.run(lambda: with_client(live.url, lambda client: client.call_tool("scale_list_scales", {})))
    post_initialize(live.url, {"Authorization": "Bearer wrong-token-value"})
    text = live.log.read_text(errors="replace")
    assert TOKEN not in text
    assert "wrong-token-value" not in text
    assert "Streamable HTTP at http://127.0.0.1" in text


# ---------- the token ----------


def test_no_token_is_refused(live):
    response = post_initialize(live.url, {})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in response.text


@pytest.mark.parametrize(
    "value",
    [
        "Bearer wrong",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        "Bearer",
        TOKEN,
        f"Basic {TOKEN}",
        "Bearer caf\xe9",
    ],
)
def test_a_wrong_token_is_refused(live, value):
    response = post_initialize(live.url, {"Authorization": value.encode("latin-1")})
    assert response.status_code == 401


def test_the_right_token_is_accepted_and_case_of_the_scheme_does_not_matter(live):
    assert post_initialize(live.url, GOOD).status_code == 200
    assert post_initialize(live.url, {"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_every_method_and_path_needs_the_token(live):
    for method in ("GET", "POST", "DELETE", "PUT", "OPTIONS"):
        assert httpx.request(method, live.url, timeout=10).status_code == 401, method
    assert httpx.get(f"http://127.0.0.1:{live.port}/", timeout=10).status_code == 401
    assert httpx.get(f"http://127.0.0.1:{live.port}/anything/else", timeout=10).status_code == 401


def test_the_sdk_client_with_a_wrong_token_gets_nothing(live):
    async def work(client):
        return await client.list_tools()

    tools_seen = []

    async def attempt():
        try:
            tools_seen.append(await with_client(live.url, work, token="wrong", mode="legacy"))
        except Exception as exc:
            return exc

    failure = anyio.run(attempt)
    assert tools_seen == []
    assert isinstance(failure, (MCPError, ExceptionGroup, httpx2.HTTPError)), repr(failure)


# ---------- what it listens to ----------


def test_it_is_bound_only_to_the_configured_address(live):
    """127.0.0.2 is this computer too, and a server on every address would answer it."""
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.2", live.port), timeout=2).close()


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "localhost:1", "127.0.0.1:1"])
def test_dns_rebinding_protection_refuses_another_host_header(live, host):
    response = post_initialize(live.url, {**GOOD, "Host": host})
    assert response.status_code == 421


def test_dns_rebinding_protection_refuses_another_origin(live):
    assert post_initialize(live.url, {**GOOD, "Origin": "http://evil.example"}).status_code == 403
    allowed = {**GOOD, "Origin": f"http://127.0.0.1:{live.port}"}
    assert post_initialize(live.url, allowed).status_code == 200


# ---------- refusing to start ----------


def seeded_folder(tmp_path, template_root) -> Path:
    shutil.copy(seeded_catalog_file(template_root, SEED), tmp_path / "catalog.sqlite3")
    return tmp_path


@pytest.mark.parametrize("flag", ["--http", "--stdio"])
@pytest.mark.parametrize("token", [None, "", "   ", "change-me"])
def test_no_token_refuses_to_start(tmp_path, template_root, capsys, flag, token):
    folder = seeded_folder(tmp_path, template_root)
    port = free_port()
    config = write_config(folder, {"A": None}, token=token, port=port)
    assert main([flag, "--config", str(config)]) == 8
    err = capsys.readouterr().err
    assert "No MCP token" in err
    with socket.socket() as sock:
        sock.settimeout(0.5)
        assert sock.connect_ex(("127.0.0.1", port)) != 0, "nothing should be listening"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "*"])
def test_listening_on_every_address_refuses_to_start(tmp_path, template_root, capsys, host):
    config = write_config(seeded_folder(tmp_path, template_root), {"A": None}, host=host)
    assert main(["--http", "--config", str(config)]) == 8
    assert "host must be one" in capsys.readouterr().err


def test_a_bad_port_refuses_to_start(tmp_path, template_root, capsys):
    config = write_config(seeded_folder(tmp_path, template_root), {"A": None}, port=70000)
    assert main(["--http", "--config", str(config)]) == 8
    assert "port" in capsys.readouterr().err


def test_a_catalog_nobody_seeded_refuses_to_start(tmp_path, capsys):
    config = write_config(tmp_path, {"A": None})
    assert main(["--http", "--config", str(config)]) == 8
    assert not (tmp_path / "catalog.sqlite3").exists(), "the server must never create a catalog"
    assert capsys.readouterr().err.startswith("ScaleConnect MCP server did not start")


def test_a_missing_config_refuses_to_start(tmp_path, capsys):
    assert main(["--http", "--config", str(tmp_path / "nothing.toml")]) == 8
    assert "No config file" in capsys.readouterr().err


def test_a_way_to_connect_must_be_chosen():
    for args in ([], ["--http", "--stdio"]):
        with pytest.raises(SystemExit) as caught:
            main(args)
        assert caught.value.code == 2


def test_a_bad_caller_name_refuses_to_start(tmp_path, template_root, capsys):
    config = write_config(seeded_folder(tmp_path, template_root), {"A": None}, extra_auth='mcp_caller = "bad\\nname"')
    assert main(["--http", "--config", str(config)]) == 8
    assert "mcp_caller" in capsys.readouterr().err


def test_the_refusal_never_prints_the_token(tmp_path, capsys):
    config = write_config(tmp_path, {"A": None})
    main(["--http", "--config", str(config)])
    captured = capsys.readouterr()
    assert TOKEN not in captured.err and TOKEN not in captured.out


def test_a_name_set_in_the_config_goes_in_the_log(tmp_path, template_root):
    fake = FakeScale(post_write_refusal_seconds=0.0, serial="TEST-A").start()
    try:
        fake.preload(*[tuple(i.to_fields()) for i in SEED])
        folder = seeded_folder(tmp_path, template_root)
        port = free_port()
        config = write_config(folder, {"A": fake}, port=port, extra_auth='mcp_caller = "invoice-desk"')
        process = subprocess.Popen(
            [sys.executable, "-m", "scaleconnect.mcp_server", "--http", "--config", str(config)],
            env=child_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                    break
                except OSError:
                    time.sleep(0.1)

            async def work(client):
                return await client.call_tool(
                    "scale_preview_changes",
                    {"changes": [{"action": "edit", "target_plu": "1001", "price": "11.00"}], "read_scales": False},
                )

            url = f"http://127.0.0.1:{port}/mcp"
            preview = anyio.run(lambda: with_client(url, work, client_info=Implementation(name="someone-else", version="1")))
            assert preview.structured_content["requested_by"] == "mcp:invoice-desk"
        finally:
            process.terminate()
            process.wait(timeout=30)
    finally:
        fake.stop()


# ---------- stdio ----------


def test_end_to_end_over_real_stdio(tmp_path, template_root):
    fake = FakeScale(post_write_refusal_seconds=0.0, serial="TEST-A").start()
    try:
        fake.preload(*[tuple(i.to_fields()) for i in SEED])
        config = write_config(seeded_folder(tmp_path, template_root), {"A": fake})
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "scaleconnect.mcp_server", "--stdio", "--config", str(config)],
            env=child_env(),
        )

        async def go():
            async with Client(params, client_info=Implementation(name="claude-code", version="1")) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                preview = await client.call_tool(
                    "scale_preview_changes", {"changes": [{"action": "edit", "target_plu": "1001", "price": "11.00"}]}
                )
                applied = await client.call_tool("scale_apply_changes", {"preview_id": preview.structured_content["preview_id"]})
                return names, applied

        names, applied = anyio.run(go)
        assert "scale_apply_changes" in names
        data = applied.structured_content
        assert data["success"] is True and data["scale_statuses"] == {"A": "verified"}
        assert data["requested_by"] == "mcp:claude-code"
        assert fake.item(1)[4] == "11.00"
    finally:
        fake.stop()
        fake.assert_clean()


def test_a_delete_and_a_plu_change_end_to_end_over_real_stdio(tmp_path, template_root):
    """The real process and the SDK's own client: preview, a refusal without the acknowledgement,
    then apply. The fake scale has to show the result, and no E message may reach it."""
    fake = FakeScale(post_write_refusal_seconds=0.0, serial="TEST-A").start()
    try:
        fake.preload(*[tuple(i.to_fields()) for i in SEED])
        config = write_config(seeded_folder(tmp_path, template_root), {"A": fake})
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "scaleconnect.mcp_server", "--stdio", "--config", str(config)],
            env=child_env(),
        )

        async def preview_and_apply(client, change):
            preview = await client.call_tool("scale_preview_changes", {"changes": [change]})
            assert not preview.is_error, preview.content
            data = preview.structured_content
            refused = await client.call_tool("scale_apply_changes", {"preview_id": data["preview_id"]})
            assert refused.is_error and refused.structured_content["error"]["code"] == "acknowledgement_required"
            applied = await client.call_tool("scale_apply_changes", {"preview_id": data["preview_id"], "acknowledge_warnings": True})
            assert not applied.is_error, applied.content
            return data, applied.structured_content

        async def go():
            async with Client(params) as client:
                deleted = await preview_and_apply(client, {"action": "delete", "target_plu": "1002"})
                moved = await preview_and_apply(client, {"action": "edit", "target_plu": "1001", "plu": "1999"})
                found_old = await client.call_tool("scale_read_items", {"plus": ["1002", "1001"]})
                return deleted, moved, found_old.structured_content

        (delete_preview, delete_result), (move_preview, move_result), read = anyio.run(go)

        assert [w["code"] for w in delete_preview["warnings_requiring_ack"]] == ["delete_placeholder"]
        assert delete_result["success"] is True and delete_result["scale_statuses"] == {"A": "verified"}
        assert fake.item(2)[1:5] == ("9002", "9002", "UNUSED", "0.01")

        assert [w["code"] for w in move_preview["warnings_requiring_ack"]] == ["plu_change"]
        assert move_result["success"] is True and move_result["scale_statuses"] == {"A": "verified"}
        assert fake.item(1)[1:3] == ("1999", "1999")
        assert fake.item(1)[3:5] == ("Test Cheddar", "10.00"), "the other fields stay"

        assert [row["found"] for row in read["scales"][0]["results"]] == [False, False], "neither old PLU is on the scale"
        assert not any(m.raw.startswith(b"E") for m in fake.messages)
        assert_wire_clean(fake)
    finally:
        fake.stop()
        fake.assert_clean()
