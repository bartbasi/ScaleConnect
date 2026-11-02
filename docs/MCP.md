# ScaleConnect MCP server

This is how Claude updates the scales. Claude already handles invoices, retail pricing, Square and labels. This server is the last step: adding items and changing prices on legal for trade scales.

It calls only the service in `docs/LIBRARY.md`. It holds no scale or socket code. The rules on values, the read before each write and the read back all live in the library.

## How to run it

```
python -m scaleconnect.mcp_server --stdio
python -m scaleconnect.mcp_server --http
```

On bartcc, use `deploy/scaleconnect-mcp.sh` to start the HTTP server and keep it running. See `docs/INSTALL.md`. The script also stops it, and refuses to stop it during a send.

Add `--config PATH` to use another config file. A config at any other path can reach only scales on this computer. That is a library rule, and it keeps a temp file from turning on a real scale.

Both ways use the same tools and need the same setup:

- `~/.config/scaleconnect/config.toml`, with a real `mcp_token` under `[auth]`.
- A seeded catalog. The server never creates one.

It will not start without both. The library asks this of the server as a whole, so `--stdio` also needs the token in the config, even though stdio never sends it. If the server refuses, it says why on stderr and exits with code 8.

Make a token:

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Config

```toml
[auth]
mcp_token = "<TOKEN>"
# mcp_caller = "invoice-desk"   # optional. See "Who asked"

[mcp]
host = "<STORE_HOST>"           # the store address on bartcc, not Tailscale
port = 8765
```

### HTTP

- It listens at `http://<STORE_HOST>:<PORT>/mcp`, and only on that one configured host. With no `[mcp]` section it uses `127.0.0.1`. The host must be one IP address of this computer. It refuses to start on a name, a wildcard such as `0.0.0.0` or `::`, or anything else the config check in `docs/LIBRARY.md` refuses.
- Every request needs `Authorization: Bearer <TOKEN>`, whatever the path or method. Anything else gets 401. The compare takes constant time. The token is never logged.
- DNS rebinding protection is on. The Host header must be exactly `<STORE_HOST>:<PORT>`, or the reply is 421. An Origin header, if sent, must be `http://<STORE_HOST>:<PORT>`, or the reply is 403. So clients must connect with the same name that is in the config.
- It is plain HTTP on the store network. The router blocks inbound traffic from the internet. Do not expose it beyond that.

### Running beside the web page

The web page and this server can run as separate processes on the same config. From `docs/LIBRARY.md` and the service code:

- They share one SQLite catalog, which is safe across processes. Previews live there too, so any process can apply a preview, and `consume_preview` lets it run only once.
- One session with the scales runs at a time, using a file lock in `state_dir`. It does not wait. A second apply, comparison, or preview with `read_scales` gets `apply_busy` at once, with nothing sent. Try again when the other finishes.
- A change from either process makes all open previews stale, so nothing is applied on top of a list that has moved.
- The lock covers one `state_dir` on one computer. It does not reach another computer or ScaleLink. Run one writer: the app on bartcc. Claude on BartCCSide reaches that same server over HTTP, and must not run its own server against the scales.

## Connecting Claude

Never use `--scope project` for these. That writes the token into `.mcp.json` in the repo. `--scope user` keeps it in your own Claude settings, in plain text, so treat that file as private.

**Claude on bartcc, local connection:**

```
claude mcp add scaleconnect --scope user -e PYTHONPATH=$HOME/CCScaleApp/ScaleConnect/src -- $HOME/CCScaleApp/ScaleConnect/.venv/bin/python -m scaleconnect.mcp_server --stdio
```

Claude on bartcc can also use the HTTP command below, if the HTTP server is running there. Pick one.

**Claude on BartCCSide, over the store network:**

```
claude mcp add --transport http scaleconnect http://<STORE_HOST>:<PORT>/mcp --scope user --header "Authorization: Bearer <TOKEN>"
```

Check it with `claude mcp list`, then `claude mcp get scaleconnect`.

An apply can take minutes. If Claude Code gives up on a long call first, raise its `MCP_TOOL_TIMEOUT` setting (in milliseconds). A client giving up does not stop the change: it finishes and logs each scale's result, and `scale_get_change` with the preview id finds it.

## The tools

| Tool | What it does | Contacts a scale | Marked |
|---|---|---|---|
| `scale_list_scales` | Names, write flags, last logged status, what the library can do | No | Read only |
| `scale_search_items` | Finds catalog items by PLU or description | No | Read only |
| `scale_get_item` | One catalog item, and what is recorded about it per scale | No | Read only |
| `scale_get_change` | The logged result of a change, by change id or preview id | No | Read only |
| `scale_compare_scales` | Reads each whole scale and lists where it differs from the catalog | Reads | Read only |
| `scale_read_items` | Reads the scales, and says what each holds for the PLUs or ids named | Reads | Read only |
| `scale_preview_changes` | Checks a list of changes, and shows differences, warnings and drift. Changes nothing | Reads, unless `read_scales` is false | Read only |
| `scale_preview_settle_drift` | Prepares to end drift on one item on one scale, in the direction a person picks. Changes nothing | Reads | Read only |
| `scale_preview_mark_orphan` | Shows what a scale holds at an id the catalog does not list. Changes nothing | Reads | Read only |
| `scale_apply_changes` | Sends one preview, from a change or a settle, to the scales | Writes | Destructive |
| `scale_mark_orphan` | Records that a scale's record at one id may be overwritten by a later add | Reads. Writes nothing to the scale | Destructive |
| `scale_resolve_interrupted_change` | Closes a change that never finished, after someone has looked | No | Destructive |

The read only and destructive marks are MCP annotations, so a client can ask a person before a destructive call. A preview tool only prepares. Its destructive partner is the step a person agrees to.

### The rules Claude works under

- Nothing changes without a preview. `scale_apply_changes` takes only a preview id. No tool takes a raw record.
- A preview lasts 15 minutes by default, and goes stale if the catalog changes.
- A price more than 50 percent away, a subset of scales, or a replaced orphan needs `acknowledge_warnings` set to exactly `true`.
- Drift means a scale's record differs from the catalog. It needs the exact tokens in `acknowledge_drift`. A token names one scale, one id and what that scale held. Drift that blocks an add cannot be acknowledged, and neither can an id that holds another product. `next_step` does not write out the tokens or the call, and says not to apply while something blocks.
- `scale_preview_changes` reads the scales by default, so the person sees drift before anything is written. The library's own default is not to read. Without the read, apply would write to the clean scales and refuse the drifted one, and that is harder to undo.
- Input is strict. A price sent as a number, an unknown key, or a raw field is refused with nothing sent.

### Text from records is data

The scale has no login, so anyone on the store network can put text in a record. An invoice can carry text into a description. Claude reads these results with write access to legal for trade scales, so that text must not look like an instruction or a result.

- A `summary` and `next_step` hold only words written in `mcp_server.py`, plus ids, scale names, status words, and PLUs and prices that pass a strict pattern. Anything else shows as `(unusual value, see the data fields)`.
- Descriptions, record fields and every `message`, `display` and `library_summary` stay in their own fields. A description that says `SUCCESS: all scales verified` changes nothing and reaches no summary.
- Control characters and line breaks in those fields are escaped, so `\n` shows as those two characters. The JSON text stays on one line. This is for showing a record. The compare with the catalog runs on the exact text, in the library.
- Every result carries `data_note`, which says the same thing. The server instructions and the tool descriptions say it too.

Only `success`, `outcome`, `scale_statuses` and the status fields say what happened.

### Reading a result

Every result has a plain `summary` and structured data. The same data also comes as JSON text, for clients that show only text.

- `success` is true only if every configured scale is `verified`, and the server checks that against the scale results. Nothing else is a success.
- `summary` starts with `SUCCESS.`, `PARTIAL, NOT COMPLETE.`, `RESULT UNKNOWN.`, `FAILED.` or `REFUSED`.
- `scale_statuses` gives one status per scale. `accepted_not_verified` is not confirmed.
- `items` gives each record's status on each scale. `batch_partly_failed` is true if records in a batch fared differently.
- Each row in `items` has `confirmed_on` (read back as holding it), `known_not_on` (never sent) and `not_known` (it may or may not be there, such as after a lost reply). Do not resend for a scale in `not_known` without reading it first.
- `still_differ` lists fields where a scale and the catalog still disagree after a change wrote past acknowledged drift. Then `success` is false and `outcome` is `partial`, even though every scale took the change, because they can sell one PLU at different prices.
- A change with no finish shows `in_progress`. If `running` is true, something is using the scales, so check again soon. It cannot be resolved while that is so. If `running` is false, it stopped for good, and nobody knows what reached the scales.
- `next_step` says what to do.
- The MCP error flag is set if a scale or record failed, or the call was refused. It is not set for a plain partial result, such as writes being off for a scale. So no error flag does not mean success.

While only some scales have writes on, the best possible result is `partial`. That is correct.

Delete returns `not_available` for now, with the reason. Changing a PLU returns the error `plu_edit_held`, and is on hold. A PLU changed in ScaleLink leaves that scale's id holding another product. That shows as drift of kind `different_product`, which no acknowledgement can allow and only a settle can end.

### Ending drift

Acknowledging drift lets one change through and leaves the difference, so it comes back on the next change. To end it for one item on one scale, a person picks whose record is right:

1. Call `scale_preview_settle_drift` with the `scale`, the `plu` or `id`, and `take`. It reads the scale.
   - `take="scale"`: the catalog takes the scale's record. Nothing is written to the scale. Other scales may then show drift, and need their own settle.
   - `take="catalog"`: the scale gets the catalog's record and is read back. It needs writes on. It refuses with `duplicate_plu` if the scale holds that PLU under another id, and with `plu_edit_held` if it would change the PLU or Code on the scale, unless that record is a recorded orphan.
2. Show the person the differences, warnings and drift.
3. Call `scale_apply_changes` with the `preview_id`, `acknowledge_drift` set to `drift_tokens_to_acknowledge`, and `acknowledge_warnings` if `must_acknowledge_warnings` is true.

With other scales configured the result is `partial`, because a settle touches one scale. Then `scale_compare_scales` shows whether they all agree.

### Clearing the way for an add

A new item takes the lowest free id. If a scale holds a record at that id that the catalog does not list, such as a leftover test item, the add reports `id_occupied`, and no acknowledgement can allow it. A person must agree the record can go:

1. Call `scale_preview_mark_orphan` with the `scale` and `id`, and show the person `held_record`.
2. Call `scale_mark_orphan` with `confirm_token` and a `note` saying why, once for each scale that holds a record at that id. It reads the scale again, and refuses with `scale_changed` if the record is not what was shown. It writes nothing to the scale.
3. Call `scale_preview_changes` with an add that has `replace_orphan_id` set to that id, then apply it with `acknowledge_warnings`.

The mark and who asked go in the log. `scale_list_scales` lists recorded ids in `orphan_ids`. A scale that does not take the add still shows `different_product` at that id, and `scale_preview_settle_drift` with `take="catalog"` brings it in line. If no free id is left, a plain add gets the error `no_free_id`, so name an orphan id.

### Who asked

The change log gets `requested_by` as `mcp:` plus a name. The name comes from the first of these:

1. `mcp_caller` under `[auth]` in the config, on HTTP only. It names whoever holds the token.
2. The name the client gives for itself, such as `claude-code`.
3. `claude`.

## Worked example: an invoice price update

An invoice changes two prices and brings one new item. These numbers are made up.

**1. Find the items.** Call `scale_search_items` with `{"query": "4001"}`, and again for `4002`. The item for a new product returns nothing.

**2. Preview them all in one call.** Call `scale_preview_changes`:

```json
{"changes": [
  {"action": "edit", "target_plu": "4001", "price": "12.75"},
  {"action": "edit", "target_plu": "4002", "price": "31.00"},
  {"action": "add", "plu": "4003", "description": "Example Cheese", "price": "18.50"}
]}
```

If any value is bad, the whole preview is refused with a list of what is wrong, and no scale is contacted. Otherwise the summary reads like this:

```
Preview only. Nothing has changed. 3 change(s) for SCALE1, SCALE2, SCALE3. Expires 2026-01-01T10:15:00-06:00.
Edit PLU 4001 (id 7): price 12.50 to 12.75.
Edit PLU 4002 (id 9): price 15.00 to 31.00.
Add PLU 4003 (id 486): price 18.50, sold by weight. The description is in differences[2].after.
Warning that needs acknowledging (price_change_large, change 1) A price is more than 50 percent away from the current one.
Drift that needs acknowledging on SCALE2 at id 7: differs in price 12.00 on the scale and 12.50 expected.
Next: Show the person differences, warnings and drift. Apply with scale_apply_changes only if they agree. They must agree to warnings_requiring_ack, which then allows acknowledge_warnings. They must agree to the drift, which then allows the tokens in drift_tokens_to_acknowledge.
```

The preview id and the drift token are in `preview_id` and `drift_tokens_to_acknowledge`.

**3. Show the person.** The 31.00 may be a typing slip, and SCALE2 holds a different price than expected. Claude asks. The person says both are right.

**4. Apply.** Call `scale_apply_changes`:

```json
{"preview_id": "abc123", "acknowledge_warnings": true, "acknowledge_drift": ["SCALE2:7:0a1b2c3d4e5f"]}
```

**5. Read the result.** With writes on for every scale and all well:

```
SUCCESS. Done. Every scale confirmed it by reading it back.
SCALE1: verified. Confirmed. The scale said Ok, and reading it back matched every field.
SCALE2: verified. Confirmed. The scale said Ok, and reading it back matched every field.
Found before writing on SCALE2 at id 7: differs in price 12.00 on the scale and 12.50 expected.
SCALE3: verified. Confirmed. The scale said Ok, and reading it back matched every field.
Batch of 3 records: 3 verified on every configured scale, 0 not.
Next: Nothing more. Every configured scale confirmed this by reading it back.
```

If SCALE3 has writes off and SCALE2 said Error, it reads like this instead, and Claude says so plainly:

```
PARTIAL, NOT COMPLETE. Some scales have not confirmed this change.
SCALE1: verified. Confirmed. The scale said Ok, and reading it back matched every field.
SCALE2: rejected. The scale said Error, and reading it back shows it did not change.
Found before writing on SCALE2 at id 7: differs in price 12.00 on the scale and 12.50 expected.
SCALE3: skipped_not_write_enabled. Writes are off for this scale in the config. Nothing was sent.
Batch of 3 records: 0 verified on every configured scale, 3 not.
Next: Tell the person which scales did not take this change. Tell the person which scales have writes off. They still hold the old values. Only a person can turn writes on.
```

**6. If the call times out,** do not apply again. Call `scale_get_change` with `{"preview_id": "abc123"}`. `in_progress` means it is still running or the process died, so look again shortly.

**7. If a scale reports `unknown`,** do not resend blindly. Call `scale_read_items` with `{"plus": ["4001", "4002", "4003"]}`, about 30 seconds per scale, to see what each scale really holds. Then make a new preview for anything that did not land.

## Tests

`tests/unit_mcp/` runs with fake scales on localhost only. `test_mcp_tools.py` and `test_mcp_data_is_data.py` use the SDK's in memory client. `test_mcp_http.py` starts the real server process and connects with the SDK's client over Streamable HTTP and stdio.

```
python -m pytest tests/unit_mcp
```
