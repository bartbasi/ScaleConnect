# ScaleConnect design

This file sets the boundaries so agents can work at the same time without colliding. The lead owns it. If it is wrong, tell the lead. Do not work around it. This file goes in the repo, so it holds no addresses, prices or real product data.

## Layers

Interfaces call the service. Only `scale.py` opens a socket to a scale.

```
mcp_server.py    web/    cli.py
        \         |       /
          service.py            changes, previews, per scale results, change log
         /          \
   catalog.py      scale.py     one session with one scale
                      |
                  protocol.py   framing, check character, build and parse. No sockets
```

## Who owns what

Edit only your own files. To change someone else's, tell the lead.

| Path | Owner |
|---|---|
| `src/scaleconnect/protocol.py`, `scale.py`, `service.py`, `config.py`, `cli.py`, `errors.py`, `models.py` | driver-dev |
| `docs/LIBRARY.md`, `config.example.toml` | driver-dev |
| `src/scaleconnect/catalog.py`, `seed.py` | catalog-dev |
| `src/scaleconnect/mcp_server.py` | mcp-dev |
| `src/scaleconnect/web/`, `tests/unit_web/`, `deploy/` launcher and autostart files | app-dev |
| `tests/unit_mcp/` | mcp-dev |
| `tests/`, `tests/fake_scale/` | test-engineer |
| `docs/PROTOCOL.md` | protocol-analyst |
| `docs/DESIGN.md` | lead |
| `README.md`, `docs/STAFF.md`, `docs/CLI.md`, `.gitignore`, `LICENSE` | release-manager |

## The item model

`models.py` defines this. Others import it and do not redefine it.

A scale record has 20 fields, in order:

| Position | Field | Notes |
|---|---|---|
| 1 | `id` | Whole number. The scale accepts up to 500. Id 0 got `Ok`, but do not use it |
| 2 | `code` | 4 digits as text |
| 3 | `plu` | 4 digits as text. What staff enter on the scale |
| 4 | `description` | Text |
| 5 | `price` | Text with two decimals, such as `9.99`. Never a float |
| 6 | `field6` | Empty or one space. Meaning unknown. Keep what the item already has |
| 7 to 12 | `fields_7_12` | Six text values, usually `1 1 0 0 0 0`. Position 7 may be by weight or by piece. Keep them |
| 13 | `timestamp` | `YYYY-MM-DD HH:MM:SS`, the time of the last edit |
| 14 to 20 | `fields_14_20` | Seven text values, usually all `0`. Keep them |

An item is immutable. An edit makes a new item with only the requested fields changed.

For a new item, use `field6` empty, `fields_7_12` of `1 1 0 0 0 0` unless told otherwise, and `fields_14_20` all `0`.

## Protocol facts everyone can rely on

See `docs/PROTOCOL.md` once it exists. Until then:

- Framed message: text, TAB, LF, check character, CR.
- Check character: multiply each byte, first through the LF, by weights 2, 3, 4, 5, 6, 7, 8 repeating. Negate the total, mod 11, add 0x30. Value 10 is `:`.
- Sequence: connect, send `RI`, the scale closes. Connect, send `bX01XX`, expect `bXXXOk`. Send each `GP` batch and wait for its reply before the next. Send `GPF0`, expect `GPF0Ok`. Close.
- A `GP` batch is `GP`, a two digit count, and the records. Records end in TAB LF. The final TAB LF of the batch is part of the framing.
- A reply to a `GP` batch starts with the check character of what was received, then CR, then `Ok` or `Error`. `GPF0Ok` proves nothing about items.
- A wrong check character makes the scale close the connection.
- Use batches of 5, like ScaleLink, until tests prove otherwise.

## Timing

A scale can pause before replying. We saw 3.25 seconds, and ScaleLink's 3 second limit made it fail. The default reply wait is 15 seconds and is configurable. Connect retries wait and try again, because a scale can briefly refuse connections. Never send the next message before the previous reply.

## Change results

Every change reports one result per scale. Use these exact statuses:

| Status | Meaning |
|---|---|
| `verified` | Accepted and confirmed by reading back. Only if reads exist |
| `accepted_not_verified` | The scale said `Ok` to every record. Nobody has confirmed the display |
| `rejected` | The scale said `Error` |
| `unknown` | Timeout or lost connection after sending. State unknown |
| `unreachable` | Could not connect. Nothing sent |
| `skipped_not_write_enabled` | The config does not allow writes to this scale |
| `not_available` | Nothing sent. The web page left this scale out because it may be frozen. Each item is `not_sent` |
| `wrong_scale` | The serial at that address did not match the config. Nothing written |
| `mismatch` | Accepted, but reading it back shows something different |
| `drift_refused` | The scale's record differs from the catalog and that was not acknowledged. Nothing written |
| `read_failed` | Could not read the scale first, so nothing was written |

A change is a full success only if every configured scale is `verified`. `accepted_not_verified` is a partial result and must be shown as such. Never call it confirmed.

A full success means the scales only. Bart's rule is that a change is not finished until Square and the labels match too. No code here reaches them. Claude does it with its own connector and skill, as `docs/MCP.md` says, and the web page tells staff to tell the manager. A client cuts server instructions and tool descriptions at 4,096 characters, so the store's rules for this are in the result of a tool that contacts nothing, `scale_square_rules`, and the rule that matters most at each step is in that result's `next_step`.

The list of unfinished work takes no scale's word for what `verified` means. `verified` says a scale holds what was sent, and an edit sends that scale's own record with only the asked for fields changed. So the list also shows any row whose last record sent differs from the catalog in anything but the time, whatever its status. A miss the catalog did not take has no settle offered, because a settle puts the old value back.

## Validation

`service.py` validates before anything reaches a scale, because the scale does not. Price is more than 0, at most 999.99, with two decimals. PLU is exactly 4 digits. Code always equals PLU. PLUs 9001 to 9500 are held back for placeholders, so no add or edit may use one. No duplicate PLU. Id within 1 to 500. Description is not empty, has no TAB, CR or LF, and has a maximum length set in config. Use 35 until the lab finds the real limit. Characters allowed are printable ASCII until the lab shows more is safe.

## Each scale is the truth about itself

Lead's decision, November 1, after the stage D review (C1, C2, C3) and the lab finding the `LP` read. `docs/PROTOCOL.md` is the reference for messages.

The catalog is our master list of what we want. It is not proof of what any scale holds. Every write to a scale follows this order, per scale:

1. **Check identity.** Send `CX` and compare the serial with the config. On a mismatch send nothing else, and report `wrong_scale`. A write enabled scale must have a serial in its config. Two entries may not share an address.
2. **Read first.** Read the whole table with `LP`, once, because a write must find its PLU under any other id, and know where the table ends. If the read fails, write nothing to that scale and report `read_failed` with a clear reason.
3. **Compare.** If that scale's record differs from the catalog's record for that id, the scale has drifted. Do not overwrite it silently. The preview reports the drift per scale, naming the fields and values, and apply needs an acknowledgement that names it. Previews do not contact scales unless asked to, so apply must repeat this check, refuse on any drift not acknowledged, and report what it found.
4. **Build from what the scale holds.** The record sent to a scale is that scale's own record with only the requested fields changed, plus a new timestamp. So one scale's unrelated difference never reaches another. An add takes no existing record. Refuse an add if the id holds anything on that scale, unless it is a recorded orphan we chose to replace. Refuse an add if the PLU is on that scale under another id.
5. **Write,** following the sequence in `PROTOCOL.md`. Check each batch's reply. Use the correct count.
6. **Wait and read back.** After `GPF0` the scale ignores connections for about 10 to 11 seconds. Retry patiently. Read to the id and compare every field as exact text. Match gives `verified`. No match gives `mismatch`, listing what the scale holds. If it can't be read back, give `accepted_not_verified`.
7. **Never resend automatically after sending has begun.** The next attempt starts again from step 1, because the read shows whether the record landed.

Reads and writes to one scale happen one session at a time. Allow for a read being slow: the lab's pace was one send per second, and reply speed is untested on a real scale. Make the pace configurable, defaulting to 0.2 seconds between read messages, and stop on any stall. Never send `LG`, and never store its reply, because it holds password like values. The only reads the library may use are `LP` and `CX`.

The catalog records, per scale, the last record read, and when. A change counts as a success only if every configured scale is `verified`. A subset of scales is allowed only with an explicit acknowledgement, and the outcome says which scales were not touched. It never says "every scale". There is a service call that compares the catalog with each scale, and lists differences.

The service must never create a catalog. It refuses to run unless the catalog has a seeded marker, and service and seed share one path rule.

Fields 7 to 12 and 14 to 20 are never set by callers as raw values. A new item takes one named choice, sold by weight or by piece, which sets field 7. All other fields take the defaults. Edits can change only price, description, PLU and Code. Editing a PLU sets Code to match, and a request where they differ is refused. Bart's check at FRONT on November 2 showed that a record at the same id with a new Code and PLU works, and that the old PLU stops working. Id decides which item is replaced, per the lab.

A PLU change is verified only if the id reads back as the new record and neither the old PLU nor the new one is on another id of that scale. It can land on one scale and not another. The catalog moves once any scale took it, and it notes each other scale as possibly still on the old record. A retry is the same edit. It needs an acknowledgement again, and it names the scales still behind. It accepts a scale holding either the old record or the new PLU with the same description, and treats anything else as a different product. Until it finishes, the old PLU is not free for another item, and settling toward that scale is refused, since it would undo the change. If every reply was lost, the catalog stays where it was and a scale may hold the new PLU, so both stay taken until a read or a repeat settles it. Only an edit that names the PLU may write a new one over what a scale holds. A price edit does not. Settling with `take="catalog"` does not either, and refuses with `plu_edit_held`, because it could overwrite what a scale holds with nobody having asked for a PLU change.

## Deletes

A delete overwrites the id with a placeholder record. Bart approved this, and checked it at FRONT on November 2. `EP` is unproven and may have taken FRONT off the network, so the library never sends any `E` message, and it has no way to frame one.

The placeholder for id N has Code and PLU `9000 + N`, description `UNUSED`, price `0.01`, field 6 empty, the default fields 7 to 12 and 14 to 20, and a new timestamp. It is a normal `GP` record. A delete backs up first, reads first and reads back, like any write. It is verified only if the id holds the placeholder and the old PLU is on no other id of that scale. A different product at the id cannot be acknowledged. Field drift on the same product needs its token, including a timestamp alone.

A placeholder on a scale is an empty slot. It is not drift, not an orphan, and never in the catalog. A deleted item keeps its id until every configured scale has confirmed the placeholder. Then an add may write over it.

## Gaps and reloads

A read can freeze a scale. See `docs/PROTOCOL.md` section 3. A gap in the ids is suspected, not proven, so no write may leave one. Before writing an id above a scale's last filled id plus one, the ids between get placeholders in the same send, in id order, and the result says so.

Filling relies on where a read ended, and nobody has ruled out a read ending early. So a read that ends below an id the scale should hold refuses that scale, with nothing sent. An id counts unless the catalog has positive evidence it never went to this scale, such as an add that was not delivered there, so a scale that merely missed an add is not stuck. A fill never goes over an id the catalog lists, or a deleted item a scale may hold. A real fill must have been shown in a preview that read that scale, and acknowledged. A short read frees no deleted id. A scale is confirmed free of one by a placeholder read at that id, or by a read it can trust showing nothing there, and not if the PLU is under another id. Until then the deleted item's PLU is not free either.

`scaleconnect reload` is for a person at the command line, not the page or the MCP server. A catalog can be out of date, so first it compares itself with that scale's last known read, with no scale contact. It compares every field but the time, and a read over 15 minutes old counts as nothing known. If they differ, or nothing is known, it refuses unless a person accepts that with a second flag. It checks identity with `CX` alone, sends ids 1 to the highest catalog id with placeholders for the rest, and reads once only if every record was accepted. The report names the prices that changed. It is the repair for a frozen scale, a scale fresh from a product reset, and a scale whose ids differ from the catalog.

## Catalog

ScaleConnect keeps its own item list in SQLite, because we may not be able to read a scale. It also holds an append only change log.

It is seeded from a private JSON file built from ScaleLink's real full send, so every field is exactly what ScaleLink sends. The seed lives outside the repo. Tests use made up data.

Ids are limited to 500, and the real catalog is close to that. New items take the lowest unused id. Track which ids are known to hold orphan items on a scale.

## Config

Path: `~/.config/scaleconnect/config.toml`. It holds scale names, addresses, write enabled flags, the PIN and the MCP token. Only FRONT is write enabled until Bart says otherwise. A running service refuses writes if the file has since gained anything it has not loaded, and says to restart both services. A flag turned off takes effect at once. The default for any scale with no flag is not write enabled. Code must run on Windows and Linux.

## Stack

Python 3.13. Standard library only in `protocol.py` and `scale.py`. FastAPI and uvicorn for the page. The official `mcp` SDK. pytest. The virtual environment is `ScaleConnect/.venv`. Run tests with `.venv\Scripts\python.exe -m pytest` on this PC.

## Testing

Automated tests only use the fake scale on localhost. They never touch a real scale.
