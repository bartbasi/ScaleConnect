# ScaleConnect library interface

This is the interface the MCP server, web page and command line tool are built on. They call `ScaleConnectService` and nothing below it. driver-dev owns this file. To change it, tell the lead.

Only `scale.py` opens a socket to a scale. Interfaces never import `scale.py` or `protocol.py`.

## What interfaces must do

1. Never show `success` alone. Show `display`, then each scale's status. `accepted_not_verified` is a partial result, never "confirmed".
2. Plan for slow calls. `apply_change` holds the caller until every scale is done, and it reports the change id only at the end. Allow several minutes. See "How long things take". If a client loses the connection, `get_change_for_preview(preview_id)` finds the result.
3. Show a preview before any apply, and pass acknowledgements only after a person, or Claude acting for one, has seen what they name.
4. Read `capabilities()` every time. Do not hard code it.
5. Refuse to run the web page or MCP server when `config.pin` or `config.mcp_token` is `None`. The example value `change-me` loads as `None`.

## Quick start

```python
from scaleconnect.service import ScaleConnectService
from scaleconnect.errors import ScaleConnectError

svc = ScaleConnectService.open()          # reads ~/.config/scaleconnect/config.toml
svc.capabilities().to_dict()
svc.list_scales()
svc.search_items("4001")
preview = svc.preview_changes(
    [{"action": "edit", "target_plu": "4001", "price": "9.99"}],
    requested_by="mcp:claude",
    read_scales=True,                     # optional. Shows drift now, and takes longer
)
if preview.ok:
    record = svc.apply_change(
        preview.preview_id,
        acknowledge_warnings=False,
        acknowledge_drift=[],             # tokens from Drift.ack_token
        requested_by="mcp:claude",
    )
    print(record.display)
    for result in record.scale_results:
        print(result.scale, result.status)
svc.close()
```

Every model has `to_dict()`, which returns plain JSON safe values. Prices are always text. Nothing in a result is a float. The service is safe to share between threads.

## Models (`scaleconnect.models`)

All are frozen dataclasses. Do not redefine them.

### Item

```python
Item(
    id: int,
    code: str,
    plu: str,
    description: str,
    price: str,                    # "9.99". A float raises TypeError
    field6: str = "",              # "" or " " on real items. Keep what the item has
    fields_7_12: tuple[str, ...] = ("1", "1", "0", "0", "0", "0"),
    timestamp: str = "",           # "YYYY-MM-DD HH:MM:SS"
    fields_14_20: tuple[str, ...] = ("0", "0", "0", "0", "0", "0", "0"),
)
Item.to_fields() -> list[str]            # the 20 scale fields, in order
Item.from_fields(fields) -> Item         # from 20 text values
Item.to_dict() -> dict                   # keys as above. Tuples become lists
Item.from_dict(d) -> Item
Item.replace(**changes) -> Item          # a new item
```

Building an `Item` checks only its shape. A record read from a scale can hold odd values, and they are reported as they are. The rules on values live in the service.

### Change

The input to a preview. Pass either `Change` objects or plain dicts with the same keys.

```json
{"action": "add", "plu": "4001", "description": "Example Cheese", "price": "12.50"}
{"action": "add", "plu": "4002", "description": "Example Piece", "price": "3.00", "sold_by": "piece"}
{"action": "add", "plu": "4003", "description": "Example", "price": "3.00", "replace_orphan_id": 490}
{"action": "edit", "target_plu": "4001", "price": "12.75"}
{"action": "edit", "target_id": 7, "description": "New Name", "price": "4.00"}
{"action": "delete", "target_plu": "4002"}
{"action": "delete", "target_id": 7, "expected_plu": "4002"}
```

```python
Change(
    action: str,                   # "add", "edit" or "delete"
    target_plu: str | None = None, # edit and delete: which item, by PLU
    target_id: int | None = None,  # edit and delete: which item, by id
    plu: str | None = None,        # add: required. edit: optional, see "Changing a PLU"
    code: str | None = None,       # optional. Must equal the PLU
    description: str | None = None,# add: required. edit: optional
    price: str | Decimal | None = None,  # add: required. edit: optional. Never a float
    sold_by: str | None = None,    # add only: "weight" (default) or "piece". Sets field 7
    replace_orphan_id: int | None = None,  # add only: reuse an id recorded as an orphan
    expected_plu: str | None = None,  # delete only: refuse with target_changed if the target now holds another PLU
)
Change.from_dict(d) -> Change      # raises ValidationFailed on unknown keys or a float price
Change.to_dict() -> dict
```

Rules:

- An edit gives `target_plu` or `target_id`, not both, and at least one field.
- An edit can change only price, description and PLU. Other fields of the record are never set by a caller.
- Code always follows the PLU. A request where they differ gets `code_plu_mismatch`.
- An add takes the lowest unused id from the catalog. Callers pick an id only through `replace_orphan_id`, which must be a recorded orphan and gives a warning that needs acknowledging.
- A new item takes `sold_by`. All its other fields take the defaults.
- PLUs 9001 to 9500 belong to placeholders. An add or edit that uses one, or an edit or delete that targets one, gets `reserved_plu`.

### Changing a PLU

`{"action": "edit", "target_plu": "4001", "plu": "4005"}` writes the same id with the new PLU and Code, and every other field as that scale holds it. A check at the scale on November 2 showed that this works and that the old PLU stops working.

- **Refused at preview:** a PLU another catalog item holds, or one a scale may still hold from an unfinished PLU change or delete (`duplicate_plu`), a PLU in 9001 to 9500 (`reserved_plu`), a bad PLU (`invalid_plu`), a Code that differs (`code_plu_mismatch`).
- **Refused per scale, with no way to acknowledge it:** the new PLU on another id, or the old PLU on another id (`plu_on_other_id`), and a different product at the id (`different_product`). Field drift on the same product needs its token, as with any write. These checks read the whole table.
- **Verified** means the id reads back as the new record, and neither the old PLU nor the new one is on another id of that scale. Otherwise it is `mismatch`.
- **Landing on one scale and not another.** The catalog moves once any scale took it. Each other scale, including one the change left out or one with writes off, is noted as possibly still on the old record.
- **A lost reply on every scale.** On `unknown` or `mismatch` alone the catalog stays where it was, and each such scale is noted as possibly holding the new PLU. So both PLUs stay taken. A plain edit on that scale, if it holds the new PLU, is refused with `unfinished_plu_change`. Settle with `take="scale"` is refused too. A read of a scale that reached every id it should hold, and shows a noted PLU nowhere, ends that note. So does a verified write. Repeating the same edit finishes it, and an edit asking for the PLU the catalog has puts it back.
- **Retrying.** Repeat the same edit, or name the new PLU, such as `{"action": "edit", "target_plu": "4005", "plu": "4005"}`. The second form also works by `target_id`. It always gives the `plu_change` warning, which names each scale noted as maybe still on an old PLU. With `read_scales`, `plu_change_lagging` also names each scale a read just found on one. A scale may hold the old record or the new PLU with the same description, and neither is a different product. A record with other values there still needs its token. Only those get this, so any other PLU at the id stays a different product, and so does the new PLU under another description. If only this finishes a change, the record goes with the catalog's own time, so a scale it leaves out does not then show timestamp drift.
- **Until it finishes,** a price or description edit is refused on the scale still on the old PLU, with a message saying to repeat the edit. A delete can still reach that scale. The old PLU cannot go to another item. A verified write, delete or reload on that scale ends it.
- **Settling** with `take="catalog"` never changes a PLU on a scale, and refuses with `plu_edit_held`. Only an edit that names the PLU does. With `take="scale"`, on a scale still on the old PLU, it refuses with `plu_change_not_finished`, because that would undo the change while other scales hold the new PLU. In both, the `Drift` has `unfinished_plu_change` true. Use that field to show "Repeat that edit", and do not match its message.

### Delete by placeholder

A delete overwrites the record's id with a placeholder, because replacing an id removes the old PLU. No `E` message exists in this library, and none can be framed. The Scale Manager approved this on November 2.

The placeholder for id N has Code and PLU `9000 + N`, description `UNUSED`, price `0.01`, an empty field 6, the default fields 7 to 12 and 14 to 20, and a new timestamp. `models.placeholder_record` builds it, and `models.is_placeholder` needs every field but the time to match.

- **The steps are those of any write.** Identity, a read of the whole table, compare, write, wait, read the whole table back. Nothing is resent automatically.
- **Compare.** A different product at the id is `different_product`, which nothing can acknowledge. The old PLU on another id, or the placeholder's PLU on another id, is `plu_on_other_id`. The same product with other values is `differs`, and needs its token, like any write. That includes a timestamp alone.
- **Verified** means the id holds the placeholder and the old PLU is on no other id of that scale. Otherwise it is `mismatch`.
- **A placeholder is an empty slot.** `compare_scales`, `read_scale_items`, `mark_orphans_from_read` and every write treat it as nothing being there. `items_read` still counts it, and the message says how many. It is not drift and not an orphan. It is never in the catalog, so never in search or the list. An add may write over one. An edit over one goes as a new item there.
- **The catalog.** The item leaves the list once any scale took the placeholder. On `unknown` or `mismatch` alone it stays listed.
- **Naming what you expect.** An id can go to a new item once a delete is finished. So a repeat built from an old result must send `expected_plu`, and it gets `target_changed` if that id now holds another PLU.
- **The id and the PLU.** A deleted item keeps its id and its PLU until every configured scale has confirmed it, including one the change left out. For a scale since taken out of the config, the PLU comes free, and the id stays held. An add or edit using that PLU gets `duplicate_plu`, naming the scales. A scale is confirmed by a `verified` delete, a verified reload, a later read that shows a placeholder at that id, or a read that reached every id the scale should hold and shows nothing there. A read showing the PLU under another id confirms nothing. Then the id and PLU are free again.
- **Trying again.** While a scale may still hold a deleted item, a delete can target it by PLU or id even though it has left the list. Use `scales` to name that scale. `list_unfinished` gives the change to use.
- **A lost reply.** If a delete ended `unknown` and landed, the catalog still lists the item. Any write except a repeat of the delete is then refused with `delete_may_have_landed`, because it would bring the item back. Its token allows that.

### Never leave a gap

A gap in the ids may be what freezes a read. It is not proven, so a write treats it as dangerous. Before writing an id above a scale's last filled id plus one, the service fills the ids between with placeholders in the same send, in id order. Filling relies on where a read ended, so it has these limits. All refuse the whole scale with `drift_refused`, nothing sent and no token that helps:

- `read_ended_early`: a whole read ended below an id the scale should hold. Nothing about that scale's end can be trusted, so nothing goes to it, whatever the write. Reload it, or have a person look. An id counts unless the catalog has positive evidence it never went to this scale, which is one of these: the belief is `absent`, from an add that was not delivered there or that left it out. Or the last change there was an add that was not accepted, with no orphan noted there. Or there is no row for this scale, and the item came through an add and not the seed, so the scale joined the config later. A seeded item, or anything a scale was sent or shown holding, always counts, as do deleted items it may still hold.
- `fill_blocked`: the fill would go over an id the catalog lists, or a deleted item a scale may hold. Settling a missed add to that scale, in id order, clears it.
- `fill_not_previewed`: a fill nobody saw. A preview with no read shows none, so apply will not fill after one.

A real fill gives the warning `gap_filled`, which needs acknowledging. So does a settle to the catalog that would fill. After the send, the scale's `message` names the ids, and `fillers_landed` lists the ones it took. They are read back with the rest. If one does not read back as sent, the scale's status is `mismatch`, with an entry in `mismatches`. A change where only fillers landed has the outcome `partial`, not `failed`. A gap already inside a table is left alone and named in the `message`. `reload_scale` closes it.

### A scale that may be frozen

`may_be_frozen` is true on a result where a scale gave no reply, or answered a read badly in any way and then did not close when we hung up. A failed connect sets it too, and so does a reload whose read back got no reply or did not close. A reload read back that answered badly and then closed does not. It is on `ScaleResult`, `ScaleRead`, `ScaleComparison`, `OrphanMarks`, `ReloadResult`, `PreviewWarning` (`scale_read_failed`) and `PreviewError` (`settle_read_failed`). Do not read that scale again until a person has checked it and rebooted it if needed, because a table that froze it can freeze it again. Use the field, and do not match the words.

`scales_may_be_frozen()` names each scale whose newest no reply has no read since that answered. A no reply on a logged change or reload is in its scale result. One on any other read, which is a preview, a compare, a read of items, a settle preview or an orphan read, is kept as a `read_no_reply` event in the change log, with the read's status. A read that answers, even a short one or one that found an empty table, keeps the newest log id at that moment in `meta`, beside a per scale time. A no reply at or before that log id is over. The place in the log decides, not the clock, so a damaged time, a time with no offset or a time in the future cannot keep a scale flagged once a read answers. Only for a scale with no log id kept yet, from before this was added, does the clock decide, and then a time that cannot be compared with now, or is after now, counts for nothing. If the log cannot be read, every configured scale is named. If the log cannot take a note, the service holds it in a small file in `state_dir/held_no_replies`, one file per scale, named for that scale, written whole, flushed to disk and then renamed. A file that cannot be read still counts against the scale in its name, and is logged once per process. A file whose note went to the log, or whose scale answered since, no longer counts, even if it cannot be removed. A removal that fails is logged with the file's path. Every process reads those files when it builds the list, so they all name that scale at once, and the first to look once the log works writes the note there and removes the file. A read of that scale that answers, in any process, removes it. Only if the file cannot be written either does the note stay in that process's memory, and the service logs an error saying so.

`avoid_frozen=True`, which the web page passes, makes `preview_changes` leave out each scale that may be frozen: a `scale_left_out` warning that needs acknowledging, the names in `left_out`, and in the saved preview. If no scale with writes on is left, it refuses with `scale_may_be_frozen`. On apply, each scale left out gets a result of `not_available` with every item `not_sent`, and is not contacted, so `list_unfinished` lists it as missing that change. `apply_change`, `read_scale_items` and `preview_settle_drift` check the list again under the apply lock. Apply then raises `ScaleMayBeFrozen` before anything is sent if a scale it would contact may be frozen: one with writes on, or with reads on for a settle that takes the scale's record. A scale with reads and writes both off is not contacted, gets `skipped_not_write_enabled`, and does not stop the others. A read leaves a flagged scale out as `not_contacted` with `may_be_frozen` true, unless its reads and writes are both off, when it gets the usual words for that. A settle preview refuses with `scale_may_be_frozen`. `mark_orphans_from_read` with `avoid_frozen=True` returns `not_contacted` with `may_be_frozen` true.

`leave_out_frozen=True`, which the MCP server and the command line pass, works the same way in `preview_changes`, whether `scales` names that scale or not. On `apply_change` it checks the list under the apply lock too, but leaves out a scale flagged since the preview, with the same `not_available` result, and raises `ScaleMayBeFrozen` only if no scale it would contact is left. On `compare_scales` it leaves a flagged scale out as `not_contacted` with `may_be_frozen` true. Both front ends pass it to `compare_scales` unless exactly one scale is named, see `compare_leaves_out_frozen`, so a compare that names that scale alone, or a reload, is the only read that can clear its flag. A flagged scale left out of a compare that named it with others says to name it alone. They pass `avoid_frozen=True` to `read_scale_items`, `preview_settle_drift` and `mark_orphans_from_read`.

### When a read goes wrong

**The one repeat.** It is off by default. The `read_repeat_enabled` setting turns it on. Turn it on if a scale fails reads with blocks out of order. On the test scale, a stopped read followed by a whole read 5 seconds later, the default `read_repeat_wait_seconds`, passed 20 of 20. That test stood in for the repeat. The library's own repeat has not run on a real scale yet, so check the first one in the read problem log. With it off, nothing is read twice. The message still gives the ids, and the problem is still logged.

With it on, a read starts once more from id 1, after `read_repeat_wait_seconds`, 5 by default, only if all of these hold:

- A block failed the order check, with at least one good block before it.
- The ids in that block are from 1 to 500.
- It came on the first `LP` of this read call. An `LP` that was dropped and tried again rules out a repeat.
- The scale closed its side when we hung up. It may send more bytes first, and those are thrown away. It has up to 8 seconds, or the reply wait if that is shorter.

At most one repeat per read call. It never skips or accepts a bad block.

- **No repeat, a plain failure.** A record not in the expected form, a reply that is no read block, a bad check character, a block too long, an id above 500, or a first block out of order, on a scale that closed when we hung up. Blank records came right before a real freeze, so these never send a second `LP`. See `docs/PROTOCOL.md` section 3.
- **No repeat, and it may be frozen.** Silence, a dropped connection, a failed connect, or any bad answer above, or blocks out of order, on a scale that did not close when we hung up. In that last case `may_be_frozen` is true with the setting on or off, and the message adds `The scale did not close the connection after we hung up.` and the frozen note. After a bad answer the scale has up to 8 seconds to close, or the reply wait if that is shorter. After a clean stop it has 2.
- **With the setting off, a read back gets no second try either.** Before round 6, a read back that got a bad answer kept trying through its window. Now a scale that answers badly once after a write ends `accepted_not_verified`, and is read once.
- **In the repeat, one try at `LP`.** If it is dropped or gets no reply, the read stops with `may_be_frozen`. Only a refused connect is tried again, because no message went out and the scale answered. A connect that gets no answer stops it too.
- If the repeat works, the read succeeds. `ReadResult.repeated` is true and `first_problem` says why. Both stay unset if no repeat was tried.
- If it fails too, the message names both reasons, and `may_be_frozen` comes from the second read. `repeated` is true here even if every connect was refused and no second `LP` went out.
- A read back keeps to its window. The repeat starts only if its wait ends inside `read_back_window_seconds`. If not, the first problem stands. While it runs, a refused connect is tried until the window ends, and silence on the read stops at once.
- A failed read can take about twice as long, plus the wait.
- A write is never repeated. `CX` is not repeated either.

**The words.** The message gives parsed ids and never a word from a record:

`FRONT: the read failed (the scale sent blocks out of order: last id seen 170, then a block from id 166 to id 170, exactly the same as the block before).`

The ending can also be `different from the block before` or `with no block before it`, and can add `, with ids inside it out of order or repeated`. An id above 99999 shows as `over 99999`. If both reads fail, the message is `<name>: the read failed (<first reason>). It was read once more, and that failed too. <second message>`.

**The log.** Every read problem adds one line to `~/.local/state/scaleconnect/read-problems.log`, or the folder `XDG_STATE_HOME` names, the same state folder `deploy/*.sh` uses. The `read_problem_log` setting or the `SCALECONNECT_READ_LOG` variable moves it. The web page, MCP server and command line share the file. It is private to the user. Past about 1 MB it drops the older half. A failure to write it never touches the read.

```
2026-11-02T16:55:12-06:00 scale=FRONT read=plain last_id=170 blocks=35 seconds=20.4 silent=no repeat=will_try what=the read failed (...).
```

`read` is `plain`, `patient`, which means a read back, or `identity`, which means a `CX` that failed. `last_id` is the last id that was in order, and it never shows above 99999. `blocks` counts blocks received, including the bad one. `seconds` runs from the start of the read call. `silent` is yes if the result was `may_be_frozen`. `repeat` is `no`, `no_time` (a read back had no window left), `will_try`, then one more line with `worked` or `failed`. It holds no product text. A read back logs when it ends, not at each refused try during the busy gap.

A `CX` that fails adds one `read=identity` line, whether the service sent it on its own or `read_items` did. When `read_items` stops there, that is the only line for that call. A `CX` that works adds no line. Neither does a failed `CX` inside a read back, which logs at its end.

### What is unfinished

`list_unfinished()` gives one `UnfinishedWork` for each scale that has not confirmed a delete, a PLU change, or an add or edit it missed. It contacts no scale, so it says what may be so.

```python
UnfinishedWork(scale: str, kind: str, item_id: int, plu: str, old_plus: tuple[str, ...], repeat: dict, message: str, status: str = "", change_id: str = "", catalog_took_it: bool = True, may_be_frozen: bool = False, differs_in: tuple[str, ...] = (), at: str = "", from_last_read: bool = False)
```

`kind` is `delete`, `plu_change` or `missed_change`. `plu` is the PLU the catalog has, or for a delete the one it had. `old_plus` is what that scale may still sell. For a delete or a PLU change, `repeat` is the change to pass to `preview_changes` to finish it. For a missed change, `repeat` names the settle to preview, with `tool`, `scale`, `take` and `plu`, and `status` and `change_id` say what happened on that scale. Show `message`, then offer that. `from_last_read` true means a settle that took another scale's record moved the catalog, nothing was sent to this scale, and its last read shows a record that differs in `differs_in`. Then `change_id` and `at` are that settle's.

### Reloading one scale

`reload_scale` is for a person, through `scaleconnect reload`. The web page and MCP server do not call it. See `docs/CLI.md` for when to use it.

First it compares the catalog with this scale's last known read, with no scale contact, on every field but the time. If they differ, or no read has reached an id, or that read is more than 15 minutes old (`RELOAD_READ_MAX_AGE`), it raises `ReloadDifferences` listing them and giving the time of the last read, with nothing sent, unless `accept_differences=True`. A change made in ScaleLink inside those 15 minutes cannot be seen. A catalog can be out of date, and this puts its prices on the scale. Show the list to a person before passing that. Then it checks identity with `CX`, sends ids 1 to the highest id in the catalog (counting deleted items), then reads once, only if every record was accepted. No read comes first, so it works on a scale whose read would freeze. A live item goes exactly as the catalog holds it, and every other id gets a placeholder. It changes no catalog item, and it logs like a change, with the action `reload`. Rule 5: the item list is backed up as for any first write. What the scale holds is not, because that needs the read this exists to avoid, and `started` in the log says `scale_dump: not_taken_no_read_before_a_reload`. An ordinary first write to that scale later still saves the dump.

```python
ReloadResult(scale: str, status: str, ok: bool, message: str, change_id: str | None, top_id: int, accepted: int,
             placeholders: tuple[int, ...], not_accepted: tuple[int, ...], differs_on_read: tuple[int, ...],
             left_above: tuple[int, ...], duplicate_plus: tuple[str, ...], may_be_frozen: bool,
             differences: tuple[ReloadDifference, ...], not_known: tuple[int, ...], last_read_at: str | None)
ReloadDifference(item_id: int, plu: str | None, fields: dict[str, FieldDiff], read_at: str, message: str)
```

`status` is `verified`, `mismatch` (a record reads back differently, or a PLU is left on two ids), `accepted_not_verified` (the read failed), `unknown` (the send stopped part way, and nothing was read), or one saying nothing went out: `unreachable`, `read_failed`, `wrong_scale`, `rejected` or `skipped_not_write_enabled`. `ok` is true only for `verified`. `message` gives the time of the last read, names each price that changed from what the scale showed then, and says if it could not know. `not_known` holds the ids with no read, or a read too old to trust. In `fields`, `before` is what the scale last showed and `after` is what was sent.

### Statuses

```python
class ScaleStatus(StrEnum):
    VERIFIED = "verified"
    ACCEPTED_NOT_VERIFIED = "accepted_not_verified"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    UNREACHABLE = "unreachable"
    SKIPPED_NOT_WRITE_ENABLED = "skipped_not_write_enabled"
    NOT_AVAILABLE = "not_available"
    WRONG_SCALE = "wrong_scale"
    MISMATCH = "mismatch"
    DRIFT_REFUSED = "drift_refused"
    READ_FAILED = "read_failed"
```

| Status | Show staff | Could it have changed the scale? |
|---|---|---|
| `verified` | The scale said Ok, and reading it back matched every field | Yes, as shown |
| `accepted_not_verified` | The scale said Ok, but it could not be read back. Check the scale | Probably |
| `rejected` | The scale said Error, and reading it back shows it did not change | No |
| `unknown` | The connection failed, the reply could not be trusted, or the scale said Error and either kept the record or could not be read back. Check the scale, or run a comparison | Maybe, or yes |
| `mismatch` | The scale said Ok, but holds something different. See `mismatches` | Yes |
| `unreachable` | Could not connect, or the opening steps failed. No item was sent | No |
| `read_failed` | Could not read the scale first. Nothing was written | No |
| `wrong_scale` | The serial at that address does not match the config. Nothing was written | No |
| `drift_refused` | The scale holds a different product, differs and nobody acknowledged it, or holds something in the way. Nothing was written. See `drift` | No |
| `skipped_not_write_enabled` | Writes are off for this scale. Nothing was sent | No |
| `not_available` | Nothing was sent. The scale was left out because it may be frozen, with `avoid_frozen`, `leave_out_frozen` or by `scales`. Each item is `not_sent` | No |

A scale's status is the worst of its items, in this order: `wrong_scale`, `unknown`, `mismatch`, `rejected`, `drift_refused`, `read_failed`, `unreachable`, `not_available`, `skipped_not_write_enabled`, `accepted_not_verified`, `verified`.

`ItemResult.status` is one of these or `not_sent`, which means an earlier problem on that scale stopped this record. A record never sent is never a success: a scale with `not_sent` beside `verified` items reports `unknown`, and the change cannot be `success`. `ItemView.scales[...].status` can also be `not_sent` or `seed`.

`verified` needs both an `Ok` and a matching read. When a batch gets `Error`, each record is settled by the read back. If it shows the old record, that record is `rejected`. If it shows the new record, or the read back fails, that record is `unknown`, because the scale may have kept it. So `rejected`, and the outcome `failed`, always rest on proof.

After a stall or lost connection the status stays `unknown`, and the scale is not read right away, because it may still be working. The next attempt, or `compare_scales`, shows what landed.

### Capabilities

```python
Capabilities(add: bool, edit: bool, delete: bool, read: bool, verify: bool, notes: dict[str, str])
```

```json
{"add": true, "edit": true, "delete": true, "read": true, "verify": true, "notes": {}}
```

Show `notes[...]` next to any control you hide or disable.

### ScaleInfo

```python
ScaleInfo(name: str, write_enabled: bool, last_status: str | None, last_change_id: str | None,
          last_seen_at: str | None, last_message: str | None,
          read_enabled: bool, interrupted_change_ids: tuple[str, ...])
```

`last_*` come from the change log. `list_scales` never contacts a scale. The address and serial are not included, and no message holds a serial. `read_enabled` is true if the library may contact this scale at all, which also holds for any write enabled scale. If `interrupted_change_ids` is not empty, show it. See "Interrupted changes".

### ItemView

```python
ItemView(item: Item, scales: dict[str, ScaleItemState], source: str, interrupted_change_ids: tuple[str, ...])
ScaleItemState(scale: str, status: str | None, belief: str | None, change_id: str | None, at: str | None,
               last_read: Item | None, last_read_at: str | None,
               last_read_matches: bool | None, last_read_found_nothing: bool)
```

`source` is `"catalog"`. The catalog is what we want. It is not proof of what a scale holds. `last_read` is the last record read from that scale for this id, and `last_read_at` is when. If `last_read` is `None` and `last_read_at` is set, that read found nothing at this id. If both are `None`, no read has reached this id. `last_read_found_nothing` says the first of these directly. `last_read_matches` compares all 20 fields with the catalog's record, and is `None` if there is no record to compare. Use it, and do not compare a few fields yourself: the same name and price can be another product. `belief` is `present` only after a read showed it (a later read showing exactly what the catalog lists also makes it so), `unknown` after a bare `Ok`, a failure or anything unsettled, and `absent` if we know it never arrived. A scale with no entry has nothing recorded.

### Drift

What one scale holds that differs from what was expected.

```python
Drift(
    scale: str,
    kind: str,
    item_id: int | None,
    plu: str | None,
    fields: dict[str, FieldDiff],
    held: Item | None,             # what the scale holds
    message: str,                  # plain words, with the fields and values
    ack_token: str | None,         # None means no acknowledgement can allow it
)
FieldDiff(before: str | None, after: str | None)
```

| `kind` | Meaning | `before` and `after` | Can be acknowledged |
|---|---|---|---|
| `differs` | The same product, by PLU and Code, with other fields different from the catalog | catalog, scale | Yes |
| `different_product` | The PLU or Code at that id differs from the catalog, so it is another product. `unfinished_plu_change` true means this scale is still on the old PLU of an unfinished change | catalog, scale | No. Settling resolves it. If `unfinished_plu_change` is true, repeating that edit does, and settling refuses |
| `read_ended_early`, `fill_blocked`, `fill_not_previewed` | See "Never leave a gap". `item_id` is `None` | none | No |
| `delete_may_have_landed` | A placeholder at the id, from a delete with no confirmed result. This write would bring the item back | none | Yes |
| `plu_on_other_id` | The PLU to be written is on this scale under another id. That refuses any write, a plain price edit too | none | No |
| `id_occupied` | An add's id holds something on this scale, and it is not a recorded orphan | none | No |
| `missing` | From `compare_scales` and `preview_settle_drift`. The scale holds nothing at an id the catalog lists | none | Not needed |
| `not_in_catalog` | From `compare_scales` and `read_scale_items`. The scale holds an id the catalog does not list | none | Not applicable |
| `mismatch` | After a write, the scale holds something other than what was sent | sent, scale | Not applicable |
| `still_differs` | In `ChangeRecord.still_differ`. The change landed, and this scale kept other values than the catalog | catalog, scale | Not applicable |

A price must never land on a different product, so a write refuses `different_product` whatever is acknowledged. Show that it needs settling, and offer no checkbox: `ack_token` is `None` from a compare, a read or a write.

An edit where the scale holds nothing at the id, inside its table, goes as an add there. It needs no acknowledgement, and the preview warns with `missing_on_scale`.

An `ack_token` names one scale, one id and exactly what that scale held. If the scale's record changes again, the old token no longer matches. Acknowledging drift never copies anything between scales. The record sent is that scale's own record with only the requested fields changed, plus a new timestamp, so the difference stays on that scale, and the result says so in `still_differ`. For `missing` the record comes from the catalog.

### Preview

```python
Preview(
    ok: bool,                      # False if any error. preview_id is then None
    preview_id: str | None,
    created_at: str,               # ISO 8601 with offset
    expires_at: str | None,
    requested_by: str,
    scales: tuple[str, ...],       # targeted scale names, in send order
    differences: tuple[Difference, ...],
    warnings: tuple[PreviewWarning, ...],
    errors: tuple[PreviewError, ...],
    drift: tuple[Drift, ...],      # only if read_scales was True
    scales_read: tuple[str, ...],  # which scales were read for it. Empty means drift was not checked
)
Difference(index: int, action: str, item_id: int | None, plu: str | None,
           fields: dict[str, FieldDiff], before: Item | None, after: Item | None)
PreviewWarning(code: str, message: str, requires_ack: bool, index: int | None)
PreviewError(code: str, message: str, index: int | None, field: str | None)
```

`Difference` compares against the catalog. `index` is the position in the list passed in, and `None` means the whole preview. `fields` holds only what changes and leaves out the timestamp. `after` for a delete is `None`, and `before` for an add is `None`. An empty `drift` with an empty `scales_read` means nobody has looked yet. Do not present that as "no drift".

Warning codes:

| Code | `requires_ack` | Meaning |
|---|---|---|
| `price_change_large` | true | New price is more than 50 percent away from the current one |
| `scale_not_targeted` | true | The preview names fewer than all configured scales |
| `orphan_replaced` | true | This add replaces what a scale holds at a recorded orphan id |
| `no_change` | false | The catalog already has these values. Applying still reads each scale and sends the record. Not for finishing a PLU change, which gives `plu_change` |
| `delete_placeholder` | true | This delete overwrites the id with a placeholder, so the old PLU stops working |
| `plu_change` | true | This edit changes a PLU, or finishes a change a scale has not confirmed. The old PLU stops working. Code follows it. When finishing, it names the scales noted as behind |
| `plu_change_lagging` | false | With `read_scales`, this scale was read just now and is still on the old PLU |
| `scale_not_write_enabled` | false | This scale will report `skipped_not_write_enabled` |
| `missing_on_scale` | false | With `read_scales`, the scale holds nothing at this id, so the record goes there as a new item |
| `gap_filled` | true | With `read_scales`, this write would leave a gap on the scale named, so the ids named get placeholders in the same send. Without a read there is no fill: apply refuses that scale |
| `interrupted_change` | false | An earlier change on these items never finished. Apply will refuse until it is resolved |
| `scale_read_failed` | false | With `read_scales`, this scale could not be read for the preview |

Error codes: `invalid_change`, `invalid_action`, `item_not_found`, `duplicate_target`, `invalid_price`, `invalid_plu`, `invalid_code`, `code_plu_mismatch`, `reserved_plu`, `duplicate_plu`, `invalid_id`, `no_free_id`, `invalid_description`, `invalid_field`, `unknown_scale`, `no_changes`, `no_scales`, `target_changed`, `config_changed`. `delete_not_available` appears only if `DELETE_AVAILABLE` is turned off.

`preview_settle_drift` adds the warning `other_scales_may_differ` (no acknowledgement), and the errors `no_drift`, `settle_read_failed`, `settle_not_possible`, `plu_edit_held`, `plu_change_not_finished`, `read_ended_early`, `fill_blocked` and `scale_not_write_enabled`.

Validation problems come back in `errors` and do not raise, so you can show them all at once.

### ChangeRecord

The result of an apply, and what `get_change` returns.

```python
ChangeRecord(
    change_id: str,
    preview_id: str,
    requested_by: str,
    acknowledged_warnings: bool,
    started_at: str,
    finished_at: str | None,
    outcome: str,
    success: bool,                 # True only if outcome is "success"
    changes: tuple[Difference, ...],
    scale_results: tuple[ScaleResult, ...],   # one per targeted scale, in send order
    summary: str,                  # display, then every scale's status, and scales not touched
    verified: bool,                # every targeted scale is verified. True is possible on a subset
    display: str,                  # one plain line. Interfaces must show it
    not_touched: tuple[str, ...],  # configured scales this change left out
    still_differ: tuple[Drift, ...],  # scales that took the change and hold other values than the catalog
    running: bool,                 # no finish is logged, and something holds the lock now
)
ScaleResult(
    scale: str,
    status: str,
    message: str,
    attempts: int,                 # write sessions opened: 0 or 1. Never more
    items: tuple[ItemResult, ...],
    replies: tuple[str, ...],      # write replies in known forms. Anything else shows only its length
    drift: tuple[Drift, ...],      # what the read before the write found
    mismatches: tuple[Drift, ...], # what the read back found
)
ItemResult(item_id: int | None, plu: str | None, action: str, status: str, message: str)
```

| `outcome` | Meaning |
|---|---|
| `success` | Every record on every configured scale is `verified`, the catalog was updated, and no scale still differs from it |
| `partial` | At least one record is on at least one scale, and the change is not complete everywhere. Also used when every scale verified it but `still_differ` is not empty: the scales still sell it differently, and `display` names the fields and values |
| `unknown` | Nothing is known to have landed, but something may have. Check the scale |
| `failed` | Nothing landed anywhere, and that is known: no record went out, or a read back showed no change |
| `catalog_out_of_sync` | The scales were contacted, but the catalog or the log could not be fully updated. Read `summary`, and run `compare_scales` |
| `in_progress` | No finish was logged. If `running` is true it may still be going. If false it stopped for good, and `display` says so. Treat the result as unknown either way |
| `interrupted` | It never finished, and someone has resolved it with `resolve_interrupted_change` |

A subset of scales never gives `success`, even when each targeted scale is verified. While only some scales are write enabled, the best result is `partial`. That is correct and is not an error.

The catalog changes after an apply as follows. An edit is saved if at least one scale took it. Otherwise the catalog keeps the old values. An add is saved if at least one scale took it, or if a scale is `unknown` or `mismatch`, because the record may be on that scale and must keep its id. So a scale can differ from the catalog after a change, and the next write there reports drift. To bring it in line, make a new preview of an edit with the values you want, and acknowledge the drift it names.

A scale's `message` is written after each record is settled, so it says the same thing as its final `status`.

### ScaleRead

What a scale holds right now, from `read_scale_items`.

```python
ScaleRead(scale: str, status: str, message: str, items_read: int, read_at: str | None,
          complete: bool, records: tuple[HeldRecord, ...])
HeldRecord(item: Item, catalog: Item | None, matches_catalog: bool, difference: Drift | None)
```

`status` is `read`, `not_contacted` (no flag allows contact), `wrong_scale`, `unreachable` or `read_failed`. Only `read` means anything is known. `complete` means the read reached the end of the table. `item` is what the scale holds, and can hold odd values. `catalog` is the catalog's record for that id, or `None` if it lists none. `difference.kind` is `differs` or `not_in_catalog`.

### ScaleComparison

```python
ScaleComparison(scale: str, status: str, message: str, items_read: int, read_at: str | None,
                differences: tuple[Drift, ...])
```

`status` is `compared`, `not_contacted` (no flag allows contact), `wrong_scale`, `unreachable` or `read_failed`.

## Service (`scaleconnect.service`)

```python
class ScaleConnectService:
    def __init__(self, config: Config, catalog: Catalog, *, backup_hook=None,
                 scale_factory=ScaleClient, clock=None, sleep=time.sleep)
    # Raises CatalogNotSeeded for a catalog that was never seeded

    @classmethod
    def open(cls, config_path: str | Path | None = None) -> "ScaleConnectService"
    # Path order: argument, SCALECONNECT_CONFIG variable, ~/.config/scaleconnect/config.toml
    # Never creates a catalog. See "Which config can reach a scale".

    def close(self) -> None
    def capabilities(self) -> Capabilities
    def list_scales(self) -> list[ScaleInfo]
    def search_items(self, query: str, limit: int = 50) -> list[Item]
    def get_item(self, *, plu: str | None = None, id: int | None = None) -> ItemView
    def preview_changes(self, changes: Sequence[Change | dict], *, requested_by: str = "unknown",
                        scales: Sequence[str] | None = None, read_scales: bool = False,
                        avoid_frozen: bool = False, leave_out_frozen: bool = False) -> Preview
    def apply_change(self, preview_id: str, acknowledge_warnings: bool = False, *,
                     acknowledge_drift: Sequence[str] = (), requested_by: str | None = None,
                     avoid_frozen: bool = False, leave_out_frozen: bool = False) -> ChangeRecord
    def compare_scales(self, scales: Sequence[str] | None = None, *, leave_out_frozen: bool = False) -> list[ScaleComparison]
    def read_scale_items(self, *, plus: Sequence[str] | None = None, ids: Sequence[int] | None = None,
                         scales: Sequence[str] | None = None, avoid_frozen: bool = False) -> list[ScaleRead]
    def preview_settle_drift(self, *, scale: str, take: str, plu: str | None = None, id: int | None = None,
                             requested_by: str = "unknown", avoid_frozen: bool = False) -> Preview
    def get_change(self, change_id: str) -> ChangeRecord
    def get_change_for_preview(self, preview_id: str) -> ChangeRecord | None
    def list_changes(self, limit: int = 50) -> list[ChangeRecord]
    def resolve_interrupted_change(self, change_id: str, note: str, *, requested_by: str = "unknown") -> ChangeRecord
    def mark_orphans_from_read(self, scale: str, *, ids: Sequence[int] | None = None, note: str = "",
                               requested_by: str = "unknown", avoid_frozen: bool = False) -> OrphanMarks
    def list_orphans(self) -> dict[int, list[str]]
    def mark_orphan(self, item_id: int, scale: str, note: str = "") -> None
    def list_unfinished(self) -> list[UnfinishedWork]
    def scales_may_be_frozen(self, unfinished: Sequence[UnfinishedWork] | None = None) -> list[str]
    def frozen_how(self) -> dict[str, str]
    def config_changed(self) -> str | None
    def reload_scale(self, scale: str, *, accept_differences: bool = False, requested_by: str = "unknown") -> ReloadResult
    def apply_running(self) -> bool
```

| Function | Contacts a scale | Raises |
|---|---|---|
| `capabilities` | No | Nothing |
| `list_scales` | No | Nothing |
| `search_items` | No | Nothing. No match gives `[]` |
| `get_item` | No | `ItemNotFound`, `ValidationFailed` if not exactly one of plu and id |
| `preview_changes` | Only with `read_scales=True`: `CX` and `LP` on enabled scales | `ApplyBusy` with `read_scales`. Nothing for bad input, which is in `Preview.errors` |
| `apply_change` | Yes | See below |
| `compare_scales` | Yes: `CX` and `LP` on enabled scales. Never writes | `ApplyBusy`. `ValidationFailed` for an unknown name or an empty list |
| `read_scale_items` | Yes: `CX` and `LP` on enabled scales. Never writes | `ApplyBusy`. `ValidationFailed` for an unknown name, an empty list of scales, or a bad PLU or id type |
| `preview_settle_drift` | Yes: `CX` and `LP` on that scale. Never writes | `ApplyBusy`. Nothing for bad input, which is in `Preview.errors` |
| `get_change` | No | `ChangeNotFound` |
| `get_change_for_preview` | No | `PreviewNotFound`. Returns `None` if not applied yet |
| `list_changes` | No | Nothing |
| `resolve_interrupted_change` | No | `ChangeNotFound`. `ApplyBusy` while anything holds the lock |
| `mark_orphans_from_read` | Yes: `CX` and `LP` on that scale. Never writes | `ApplyBusy`. `ValidationFailed` for an unknown scale or a bad id type |
| `list_orphans` | No | Nothing |
| `mark_orphan` | No | `CatalogError` for a bad id |
| `list_unfinished` | No | Nothing |
| `config_changed` | No | Nothing. Plain words if the config file has something this service has not loaded, or `None` |
| `reload_scale` | Yes: `CX`, the write messages, then `LP` once. Command line only | `ReloadDifferences` with no scale contact. `ValidationFailed` for an unknown or not write enabled scale, or a catalog record that cannot be sent. `InterruptedChange`, `BackupFailed`, `LogFailed`, `ApplyBusy`, all with nothing sent |
| `apply_running` | No | Nothing |

`search_items` puts an exact PLU first, then PLUs that start with the query, then descriptions that hold the query, ignoring case. An empty query lists items up to the limit.

`read_scale_items` returns the records named by `plus` or `ids`, on one `ScaleRead` per scale. Give neither for every record. A PLU can be under any id, so `plus` or neither reads the whole table. `ids` alone stops once the highest id is reached.

### Acknowledgements

| Argument | What it allows |
|---|---|
| `acknowledge_warnings=True` | Every preview warning with `requires_ack`: a large price change, a subset of scales, a replaced orphan, a delete, a PLU change. Must be exactly `True` |
| `acknowledge_drift=[token, ...]` | Writing to a scale whose fields differ on the same product, for these exact tokens. Tokens come from `Preview.drift` or `ScaleResult.drift`. It cannot allow a different product or a duplicate PLU |

Previews do not read scales unless asked, so apply always checks. If apply finds drift nobody acknowledged, that scale reports `drift_refused` with the tokens, and nothing is written to it. The preview is then used up. Make a new preview, and pass the tokens to apply.

### Settling drift on one item

Once drift is acknowledged, the catalog and the scale each keep their own value, so the next change shows the drift again. To end it for one item on one scale, a person picks which record wins. Settling is the only way to resolve `different_product`, so `preview_settle_drift` gives a token for that kind too.

| `take` | What happens | Writes to the scale |
|---|---|---|
| `"scale"` | The catalog takes the scale's record exactly, including its timestamp | No |
| `"catalog"` | The scale gets the catalog's record through the normal seven steps, and is read back | Yes |

```python
preview = svc.preview_settle_drift(scale="SCALE1", take="scale", plu="4001", requested_by="web:staff")
if preview.ok:
    # Show preview.drift[0].message and preview.differences[0], then:
    record = svc.apply_change(
        preview.preview_id,
        acknowledge_warnings=True,            # if any warning has requires_ack
        acknowledge_drift=[preview.drift[0].ack_token],
        requested_by="web:staff",
    )
```

- The preview reads the scale now, so it takes as long as a read. Apply reads it again.
- `Difference.action` is `settle_accept` or `settle_push`. `fields` shows what will change: `before` and `after` are catalog then scale for `settle_accept`, and scale then catalog for `settle_push`. Both include the timestamp.
- It logs like any other change, and `get_change` returns it. A `settle_accept` that goes through shows the scale as `verified`, because it was read just then.
- If the scale's record changed after the preview, the scale reports `drift_refused` and nothing changes.
- `take="catalog"` sends the catalog's own timestamp, not a new one. A new one would make this scale differ from the catalog and every other scale by the timestamp alone.
- `take="catalog"` needs writes on. `take="scale"` needs only a flag that allows contact, and takes no backup, because nothing is written to the scale.
- `take="scale"` moves the catalog, so other scales that matched the old record may then show drift. The preview warns of this.

It refuses with `no_drift` if they already match, and `settle_read_failed` if the scale cannot be read. `take="scale"` refuses a record that breaks the validation rules, a PLU another catalog item holds or that is not free from an unfinished delete or PLU change, a scale still on the old PLU (`plu_change_not_finished`), or an id where the scale holds nothing (`settle_not_possible`). `take="catalog"` refuses with `duplicate_plu` if the scale holds that PLU under another id, and as any write does if its read ended early or a fill would be blocked. It refuses with `plu_edit_held` if it would change the PLU or Code on the scale, unless the scale's record at that id is a recorded orphan, which it then replaces, with the `orphan_replaced` warning. Where the scale holds nothing at the id, the token is not needed. Settling covers items the catalog lists. For an id only the scale holds, see "When an add has nowhere to land".

### When an add has nowhere to land

A real table can be full: every id holds something, some of it old test items the catalog does not list. An add to such an id reports `id_occupied`. To go on:

```python
marks = svc.mark_orphans_from_read("SCALE1", note="old test items", requested_by="web:staff")
# OrphanMarks(scale, status, message, marked: tuple[int, ...], not_marked: tuple[int, ...], held: tuple[Item, ...])
preview = svc.preview_changes(
    [{"action": "add", "plu": "4001", "description": "Example", "price": "3.00", "replace_orphan_id": marks.marked[0]}]
)
svc.apply_change(preview.preview_id, acknowledge_warnings=True)   # after a person saw orphan_replaced
```

`mark_orphans_from_read` reads one scale and records each id it holds that the catalog does not list, for that scale only. Run it for each scale that holds one. Show `held` first, because replacing an orphan removes that record's PLU from the scale. It writes nothing to the scale, and it is logged. `status` is `read`, `not_contacted`, `wrong_scale`, `unreachable` or `read_failed`, and only `read` marks anything. Give `ids` to mark only those. Any of them it cannot mark, because the scale holds nothing there or the catalog lists it, comes back in `not_marked`.

An add never takes an orphan id unless it names one in `replace_orphan_id`. If no free id is left, the preview error `no_free_id` says so. After the add, a note is cleared only for each scale that took the record. A scale that did not still shows `different_product`, and `preview_settle_drift(take="catalog")` brings it in line.

### What apply does

The preview is used up and the start logged in one transaction. So `get_change_for_preview` gives either `None`, meaning nothing has been sent, or a change. If the log cannot be written, the preview stays unused. `apply_change` raises, with nothing sent, for `PreviewNotFound`, `PreviewExpired`, `PreviewStale`, `PreviewAlreadyApplied`, `AcknowledgementRequired`, `ValidationFailed`, `InterruptedChange`, `BackupFailed`, `LogFailed` and `ApplyBusy`. After it starts sending it does not raise for scale or log problems. They appear in the record, and it continues to the next scale.

1. Takes the lock. Checks the preview exists, has not been used, has not expired, the catalog has not changed, the write flags are the same as at preview, warnings are acknowledged, every record is still valid, every add's id is still free, and no interrupted change touches these items.
2. Saves the item list backup before the first write to each scale.
3. Marks the preview used, and logs that the change started.
4. For each targeted scale, one at a time, in config order. It logs each result as it comes, and a failure moves on to the next scale:
   1. **Identity.** Sends `CX`. A wrong serial sends nothing else.
   2. **Read.** Sends `LP` and reads the whole table, once. It reads twice only with `read_repeat_enabled` on, and only after blocks out of order on a scale that then closed. See "When a read goes wrong". Any write must find its PLU under any other id, and know where the table ends.
   3. **Compare.** A different product, a duplicate PLU, an occupied id, a read that ended early, a fill nobody saw, or field drift nobody acknowledged refuses this whole scale.
   4. **Build.** The scale's own record with only the requested fields, plus one new timestamp. A record that fails validation is refused here.
   5. **Write.** `RI`, `bX01XX`, `GP` batches, `GPF0`. Each waits for the last reply, and each batch's reply is checked.
   6. **Read back.** Waits out the scale's busy gap of about 10 to 11 seconds. Compares all 20 fields as exact text.
   7. **No resend.** Nothing goes out twice on its own.
5. Updates the catalog, and logs the finish.

A read or `CX` that gets no reply says the scale may have frozen and may need a reboot. A read has frozen real scales. See `docs/PROTOCOL.md` section 3. Nothing keeps sending to a scale that stopped replying: a stall on an ordinary read ends it at once, and the read back stops at its window, or at once if blocks had started to come or it was the repeat. Blocks out of order on a scale that then does not close say the same.

How retries work: a connect that fails, at once or by timing out, is tried again up to `connect_attempts` times, and the wait doubles from `connect_retry_wait_seconds` up to 30 seconds. So is `CX` or the start of a read that gets dropped with no reply, which is another way a busy scale says no. With the defaults that is at least 65 seconds of real time, even if each refusal comes back at once. The read back keeps trying for `read_back_window_seconds`, 45 by default, also in real time. `load_config` warns if either covers under 15 seconds. Nothing is retried once a scale has stalled on an opening message or has answered, apart from the one repeat of a read in "When a read goes wrong", which is off by default.

Turning a flag off in the config file takes effect at once, even in a running server. It is checked at each scale, and again right before writing. So does taking a scale out or moving its address, which counts as off. Any other change needs a restart: a scale added, a flag turned on, a serial, a setting, the PIN or token. Until then `config_changed()` gives plain words, `apply_change` and `reload_scale` raise `ConfigChanged`, and a preview or settle gives the error `config_changed`, all with nothing sent. Otherwise a price edit would report success for the old scales while the new one got nothing.

A preview lasts `preview_ttl_seconds`, 900 by default. Any change to the catalog's items makes all open previews stale.

### Interrupted changes

If the process dies mid change, the log holds a start with no finish. Apply then refuses to touch those items, and raises `InterruptedChange`. `list_scales` and `get_item` show the change id, and a preview warns. To clear it, run `compare_scales` to see what the scales hold, then call `resolve_interrupted_change`.

The lock tells a running change from a dead one, in any process. While anything holds it, `apply_running()` is true, `interrupted_change_ids` stays empty, a change with no finish has `running` true, and `resolve_interrupted_change` raises `ApplyBusy`. Once it is free, a change with no finish has stopped for good, so show its `display`, and do not say it is working.

### Which config can reach a scale

`open` keeps a scale enabled only if the config is the local file at `~/.config/scaleconnect/config.toml`, or the scale's address is this PC. So a temp file passed with `--config` or `SCALECONNECT_CONFIG` cannot turn on a real scale. Any config with a write enabled scale must say how rule 5 is met: set `scalelink_db_path`, or `scalelink_db_copy = "not_available_here"`. The real config fails at `open` without it. Any other config gets `BackupFailed` at the first write, unless a custom `backup_hook` is passed.

The lock covers one `state_dir` on one PC. It does not reach another PC or ScaleLink. Run one writer for the scales: the app on StoreServer. Do not write from the command line on another PC at the same time.

### How long things take

Measured on real scales on November 2, 2026, through the MCP server, at the default pace of 0.2 seconds between read messages. The unreachable scale row is still an estimate.

| Step | Time |
|---|---|
| A whole table read of one scale | About 29 to 34 seconds |
| A compare, or a preview with `read_scales`, on three scales | About 95 to 102 seconds |
| Any write | A whole table read on each scale, then the write. A preview with `read_scales` does one more |
| Apply one low id edit on three scales | About 149 seconds |
| Apply an add, delete or PLU change at a high id on one scale | About 69 seconds |
| The read back | The busy gap of 10 to 11 seconds, then a read up to the id. A whole table for a delete or a PLU change |
| One unreachable scale | About 2 minutes, with the default retries |

So one edit on three scales takes about 4 minutes from preview to done. Reads and writes hold the lock.

## Validation

Done by the service on every add and edit result. It also runs on the record built from a scale's own values, and a failure there gives `drift_refused`.

| Check | Rule | Error code |
|---|---|---|
| Price | Text or Decimal, never a float. More than 0, at most 999.99, exactly two decimals, no stray leading zeros | `invalid_price` |
| PLU | Exactly 4 ASCII digits, not starting with 0 | `invalid_plu` |
| Code | Exactly 4 ASCII digits, not starting with 0, and equal to the PLU | `invalid_code`, `code_plu_mismatch` |
| PLU change | Code follows it. Not in 9001 to 9500. See "Changing a PLU" | `code_plu_mismatch`, `reserved_plu` |
| Duplicate | A new PLU must be free in the catalog. Each scale is checked at apply too | `duplicate_plu`, or `plu_on_other_id` drift |
| Id | 1 to 500 | `invalid_id` |
| Description | Not empty or blank. Printable ASCII only, so no TAB, CR or LF. At most `description_max_length`, 35 by default. A new value cannot start or end with a space | `invalid_description` |
| Sold by | `weight` or `piece` | `invalid_field` |
| Other fields | Printable ASCII, at most 20 characters each. Callers cannot set them | `invalid_field` |
| Same item twice | One preview may not target an item twice | `duplicate_target` |

A PLU with a leading zero is refused because none of the 484 real items has one (they run from 1001 to 7262), and nobody knows what a scale does with one. An existing catalog item whose Code differs from its PLU can be edited only by an edit that gives a Code equal to the PLU.

## Exceptions (`scaleconnect.errors`)

All derive from `ScaleConnectError`. Each has `.code` (stable text), `.message` (plain words for a person) and `.to_dict()`, which gives `{"code": ..., "message": ...}`.

| Class | `code` | CLI exit |
|---|---|---|
| `ConfigError` | `config_error` | 8 |
| `CatalogNotSeeded` | `catalog_not_seeded` | 8 |
| `ValidationFailed` (has `.errors`, a list of `PreviewError`) | `validation_failed` | 3 |
| `ItemNotFound` | `item_not_found` | 4 |
| `ChangeNotFound` | `change_not_found` | 4 |
| `PreviewNotFound` | `preview_not_found` | 4 |
| `PreviewExpired` | `preview_expired` | 5 |
| `PreviewStale` | `preview_stale` | 5 |
| `PreviewAlreadyApplied` | `preview_already_applied` | 5 |
| `InterruptedChange` | `interrupted_change` | 5 |
| `ScaleMayBeFrozen` (has `.scales`, only with `avoid_frozen`) | `scale_may_be_frozen` | 5 |
| `AcknowledgementRequired` | `acknowledgement_required` | 6 |
| `ReloadDifferences` (has `.differences`, `.not_known` and `.last_read_at`) | `reload_differences` | 6 |
| `ConfigChanged` | `config_changed` | 8 |
| `BackupFailed` | `backup_failed` | 9 |
| `ApplyBusy` | `apply_busy` | 10 |
| `LogFailed` | `log_failed` | 1 |
| `NotWriteEnabled`, `NotReadEnabled` (raised only by `scale.py`, the service turns them into a status) | `not_write_enabled`, `not_read_enabled` | 1 |
| `OperationNotAvailable` | `not_available` | 1 |
| `ProtocolError` (a record or message that cannot be built safely) | `protocol_error` | 1 |

## Config (`scaleconnect.config`)

```python
load_config(path: str | Path | None = None) -> Config
Config(
    scales: tuple[ScaleConfig, ...],   # in file order, which is send order
    settings: Settings,
    pin: str | None,                   # for app-dev. None if unset or the example value. Never log it
    mcp_token: str | None,             # for mcp-dev. Same. Never log it
    mcp_caller: str | None,            # the name of whoever holds the MCP token, for the log. Optional
    web: ListenConfig,                 # .host, .port
    mcp: ListenConfig,
    path: Path | None,
    warnings: tuple[str, ...],         # show these at startup
)
ScaleConfig(name: str, address: str, port: int = 50036, write_enabled: bool = False,
            serial: str | None = None, read_enabled: bool = False)
catalog_path_for(config_path=None) -> Path   # the one catalog path rule. The service and seed share it
```

An enabled scale must have a serial in the file, and two entries may not share an address and port, or a serial.

On Linux, `load_config` refuses a file that group or other can use in any way. Run `chmod 600` on it. Windows has no such modes, so this check is skipped there.

`[web]` and `[mcp]` `host` must be one real IPv4 address. `0`, `::0`, `0.0.0.0`, names such as `localhost`, and wildcards raise `ConfigError`. `config.listen_problem(host)` gives the reason, or `None`. An address in Tailscale's `100.64.0.0/10` loads with a warning in `Config.warnings`.
 `repr(Config)` hides the PIN and token. See `config.example.toml` for every setting. `unknown_retries` and `unknown_retry_wait_seconds` are gone, because a record is never resent automatically. A config that still has them loads, with a warning.

A scale with neither `write_enabled` nor `read_enabled` is never contacted. `write_enabled` alone allows reads too, because a write needs them.

## Catalog (`scaleconnect.catalog`)

The service uses it. Interfaces make no changes through it. It uses `Item`, keeps everything in SQLite, is safe to use from several threads and processes, and never contacts a scale.

```python
class Catalog:
    def __init__(self, path: str | Path)      # creates the file and tables if missing. Only seed code should
    @classmethod
    def open_existing(cls, path) -> "Catalog" # raises CatalogNotSeeded if there is no file
    def close(self) -> None
    def is_seeded(self) -> bool
    def mark_seeded(self, source: str = "") -> None   # import_items does this itself
    def epoch(self) -> str                    # differs for each catalog file

    # Items
    def version(self) -> str                  # changes whenever any item changes, and only then
    def list_items(self, *, include_deleted=False) -> list[Item]    # by id
    def get_by_id(self, item_id: int, *, include_deleted=False) -> Item | None
    def get_by_plu(self, plu: str) -> Item | None
    def deleted_item_still_held(self, *, plu: str | None = None, item_id: int | None = None) -> Item | None
    def search(self, query: str, limit: int = 50) -> list[Item]
    def free_ids(self, count: int, max_id: int = 500) -> list[int]
    # Lowest unused ids, ascending, skipping live, reserved and orphan ids, and deleted items a scale may hold.
    # May return fewer than count.
    def upsert_item(self, item: Item, *, replacing_orphan: bool = False) -> None
    def remove_item(self, item_id: int) -> None
    def import_items(self, items: Iterable[Item]) -> int            # seeding an empty catalog only

    # Ids known or suspected to hold an item on a scale that the catalog does not list
    def mark_orphan_id(self, item_id: int, scale: str, note: str = "") -> None
    def clear_orphan_id(self, item_id: int, scale: str) -> bool
    def orphan_ids(self) -> dict[int, list[str]]   # id to scale names

    # What we last recorded and read for an item on each scale
    def record_scale_state(self, item_id: int, scale: str, status: str, change_id: str, at: str, *,
                           belief: str | None = None, record: Item | None = None) -> None
    def record_scale_states(self, scale: str, rows: Iterable[tuple[int, str, str | None, Item | None]],
                            change_id: str, at: str) -> None   # many at once, in one transaction

    # PLU changes a scale has not confirmed. The table is added when an older catalog file is opened
    def note_plu_move(self, scale: str, old: Item) -> None
    def plu_moves(self, item_id: int, scale: str) -> dict[str, Item]   # noted PLU to its record
    def settle_plu_moves_from_read(self, scale: str, plus_held: Iterable[str]) -> None
    def clear_plu_moves(self, item_id: int, scale: str) -> None
    def old_plus_in_use(self) -> dict[str, int]                        # old PLU to its id

    def scale_states(self, item_id: int) -> dict[str, dict]
    # scale name to {"status", "change_id", "at", "belief", "record"}
    def record_scale_reads(self, scale: str, items: Iterable[Item], at: str, *, covered_up_to: int | None = None) -> None
    # An id at or below covered_up_to that is not in items is recorded as holding nothing.
    # If that id is a deleted item's, this scale's belief for it becomes absent, unless items holds its PLU.
    # covered_up_to must not go past what the read can vouch for
    def scale_state_rows(self, scale: str) -> dict[int, dict]
    def ids_added_since_seed(self) -> set[int]
    def last_reads_for_scale(self, scale: str) -> dict[int, dict]
    def deleted_still_held(self) -> list[dict]                # {"plu", "item_id", "scale"}
    def all_plu_moves(self) -> list[dict]                     # {"scale", "item_id", "old_plu"}
    def last_reads(self, item_id: int) -> dict[str, dict]   # scale name to {"record": Item or None, "at"}

    # Previews. Payload is a JSON safe dict the catalog does not interpret
    def save_preview(self, preview_id: str, payload: dict, created_at: str, expires_at: str) -> None
    def load_preview(self, preview_id: str) -> dict | None
    # {"preview_id", "payload", "created_at", "expires_at", "consumed_by"}. None if missing
    def consume_preview(self, preview_id: str, change_id: str) -> bool
    # Atomic. True the first time only. False if already consumed or missing

    # Change log. Append only. No update or delete, ever
    def append_log(self, change_id: str, event: str, payload: dict, at: str) -> None
    def read_log(self, change_id: str) -> list[dict]
    # Oldest first. Each is {"change_id", "event", "payload", "at"}
    def list_change_ids(self, limit: int = 50) -> list[str]   # newest first
    def open_change_ids(self) -> list[str]    # started, with no finish
    def last_scale_event(self, scale: str) -> dict | None
    # Payload of the newest "scale_result" row for that scale, plus "change_id" and "at". None if there is none
```

Events the service writes: `started`, `scale_result`, `finished` and `backup_failed`. `scale_result` holds a `ScaleResult.to_dict()`. `started` holds who asked, the acknowledgements, the config path and the backup notes. A `change_id` is a uuid in hex. `backup_failed` has its own `change_id`, and that record shows `outcome` `failed` with no `scale_results`. Read blocks never go in the log. Only the record read for an id goes in `scale_read`, or in a `Drift` for that id.

### Seeding

`python -m scaleconnect.seed SEED.json [--config PATH] [--db PATH] [--overwrite] [--believed-on NAME]`. Without `--db` the file goes where the config says, by the same rule the service uses. The service refuses any catalog not made this way. `--believed-on` records what a scale should hold after a full send. Its belief stays `unknown` until a read shows it, and no write relies on it. `--overwrite` takes the same lock as a change, so it raises `ApplyBusy` while one runs. If it fails, the old catalog and its side files go back. Stop the web page and MCP server before an `--overwrite`. On Linux a running one keeps using the old file on threads it already has.

### Backups

`backup_hook(scale: str, items: list[Item], backup_dir: Path) -> Path` runs once for each write enabled scale, before anything is sent. A delete counts as a write here. The default hook writes the item list as JSON, and copies the ScaleLink database too if `scalelink_db_path` is set. If a hook fails, `apply_change` raises `BackupFailed`, and the preview can still be used.

The first write to a scale also saves everything `LP` returns to a dump file in `backup_dir`. If that fails, the scale reports `read_failed`, and nothing is written to it. `LG`, `LH` and `LT` are not saved. Markers in `backup_dir` record that both ran. A marker counts only if it is the current version, is for this catalog file, and what it names still exists. The log says which ScaleLink copy happened: `scalelink_db_copied`, `not_available_here` or `custom_hook`.

## Command line (`scaleconnect.cli`)

`python -m scaleconnect.cli [--config PATH] COMMAND`. Installed as `scaleconnect`. Output on stdout is always one JSON document.

```
scaleconnect capabilities
scaleconnect scales
scaleconnect search QUERY [--limit N]
scaleconnect get (--plu PLU | --id ID)
scaleconnect preview (--json TEXT | --file PATH | --stdin) [--scale NAME ...] [--read] [--who NAME]
scaleconnect apply PREVIEW_ID [--acknowledge-warnings] [--acknowledge-drift TOKEN ...] [--who NAME]
scaleconnect compare [--scale NAME ...]
scaleconnect read [--plu PLU ...] [--id ID ...] [--scale NAME ...]
scaleconnect settle (--plu PLU | --id ID) --scale NAME --take (scale | catalog) [--who NAME]
scaleconnect change CHANGE_ID
scaleconnect changes [--limit N]
scaleconnect mark-orphans --scale NAME [--id ID ...] [--note TEXT] [--who NAME]
scaleconnect orphans
scaleconnect resolve CHANGE_ID --note TEXT [--who NAME]
scaleconnect reload --scale NAME --yes [--who NAME]
```

`reload` overwrites ids 1 to N on that scale and prints a `ReloadResult`. It exits 2 without `--yes`, and 7 if `ok` is false.

`settle` prints a `Preview`. Apply it with `apply`, as with any other. `preview` takes a JSON list of changes. On failure the output is `{"ok": false, "error": {"code": ..., "message": ...}}`. `preview` with errors prints the `Preview` and exits 3. `apply` prints the `ChangeRecord`.

| Exit | Meaning |
|---|---|
| 0 | Done. For `apply`, the outcome is `success`. For `compare`, every contacted scale matches. For `read`, every contacted scale was read |
| 1 | Unexpected error |
| 2 | Bad command line |
| 3 | Validation failed, including a preview or `settle` with errors, or an unknown scale name |
| 4 | Not found |
| 5 | Preview expired, stale or already applied, or an interrupted change is in the way |
| 6 | Warnings must be acknowledged |
| 7 | `apply` ran, but the outcome is not `success`. Or `compare` found differences or could not read, or `read` could not read a scale. Read the output |
| 8 | Config problem, or no seeded catalog |
| 9 | Backup failed. Nothing sent |
| 10 | Another apply is running. Nothing sent |
