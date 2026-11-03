# Command line

The command line tool calls the same library as the web page and the MCP server. It holds no scale code of its own.

Run it as `scaleconnect` once the package is installed, or as `python -m scaleconnect.cli`. This page was checked against `src/scaleconnect/cli.py` and its `--help` output.

```
scaleconnect [--config PATH] COMMAND [options]
```

`--config PATH` is the path to a `config.toml`. Without it the library uses the `SCALECONNECT_CONFIG` variable, then `~/.config/scaleconnect/config.toml`. A config at any other path can reach only scales on the same computer. That is a library rule, so a temp file cannot turn on a real scale.

Words in capitals are values you supply. An option marked "can repeat" can be given more than once.

## Commands

| Command | What it does | Contacts a scale |
|---|---|---|
| `capabilities` | What this version can do | No |
| `scales` | The scales, whether writes are on, and the last known status | No |
| `frozen` | The scales that may be frozen, and how each one stopped answering | No |
| `search QUERY [--limit N]` | Find items in the catalog by PLU or description. `--limit` defaults to 50 | No |
| `get --plu PLU` or `get --id ID` | One item from the catalog, with what is recorded for each scale. Give one of the two | No |
| `preview` | Check a list of changes. Changes nothing | Only with `--read` |
| `apply PREVIEW_ID` | Apply one preview | Yes, writes |
| `change CHANGE_ID` | The result of an earlier change | No |
| `changes [--limit N]` | Recent changes. `--limit` defaults to 50 | No |
| `compare` | Read the enabled scales and list where they differ from the catalog. Writes nothing | Yes, reads |
| `read` | Show what the enabled scales hold now. Writes nothing | Yes, reads |
| `settle` | Preview settling one item that differs on one scale | Yes, reads |
| `resolve CHANGE_ID --note TEXT` | Close a change that never finished, after someone has looked | No |
| `reload --scale NAME --yes [--accept-differences]` | Overwrite ids 1 to N on one scale with the catalog. No read first. Refuses if the catalog differs from the last read, or nothing is known, unless you accept that | Yes, writes, then one read. Not if it refuses |

### preview

Give the changes in exactly one of these ways:

- `--json TEXT`: the JSON list on the command line.
- `--file PATH`: a file holding the JSON list.
- `--stdin`: read the JSON list from standard input.

Other options:

- `--scale NAME`: only this scale. Can repeat. A preview that names fewer than all scales needs a warning acknowledged at apply.
- `--read`: also read the scales now and report drift. This takes longer.
- `--who NAME`: who is asking, for the change log. The default is `cli:` plus the user name.

Each change is an object. Prices are text such as `"12.50"`, never a number.

```
{"action": "add", "plu": "4001", "description": "Example Cheese", "price": "12.50"}
{"action": "add", "plu": "4002", "description": "Example Piece", "price": "3.00", "sold_by": "piece"}
{"action": "edit", "target_plu": "4001", "price": "12.75"}
{"action": "edit", "target_id": 7, "description": "New Name", "price": "4.00"}
{"action": "edit", "target_plu": "4001", "plu": "4005"}
{"action": "delete", "target_plu": "4002"}
```

A delete overwrites the item's id on each scale with a placeholder record named `UNUSED`, so the old PLU stops working. With `target_id` you can add `"expected_plu"`, and it is refused with `target_changed` if that id now holds another PLU. A preview with a delete gives a warning that must be acknowledged at apply. The old PLU stays taken until every scale that may hold it has confirmed, and an add or edit using it gets `duplicate_plu`. PLUs 9001 to 9500 belong to placeholders, so an add or edit that uses one gets the error `reserved_plu`. An edit can change a PLU with `"plu"`, and Code always follows it. A preview with a PLU change also gives a warning that must be acknowledged at apply, because the old PLU stops working. If it lands on one scale and not another, repeat the same edit, which needs the same acknowledgement. Settling cannot do this: `--take catalog` refuses with `plu_edit_held`, and `--take scale` on a scale still on the old PLU refuses with `plu_change_not_finished`. Any write reads the whole table on each scale first, and is refused if its PLU is on another id there, or if the read ended below an id the catalog expects. If the write would have to fill a gap, it needs a preview with `--read`, and that warning acknowledged. The full list of keys, warnings and error codes is in `docs/LIBRARY.md`.

### apply

- `PREVIEW_ID`: the `preview_id` from a preview.
- `--acknowledge-warnings`: allow every warning that needs it, such as a price more than 50 percent away, a subset of scales, a delete or a PLU change. Only use it after a person has seen the warning.
- `--acknowledge-drift TOKEN`: allow writing to a scale whose record differs from the catalog, for this exact token. Tokens come from the `drift` list in a preview or a result. Can repeat.
- `--who NAME`: who is asking.

Apply can take minutes. One edit on three scales can take over a minute, and an add over two minutes. If the connection is lost, use `change` or `changes` to find the result.

### settle

Previews settling one item that differs on one scale. Apply the preview with `apply`, as with any other.

- `--plu PLU` or `--id ID`: the item. Give one.
- `--scale NAME`: required. The scale.
- `--take scale` or `--take catalog`: required. Whose record wins. `scale` makes the catalog take the scale's record and writes nothing to the scale. `catalog` writes the catalog's record to the scale.
- `--who NAME`: who is asking.

### compare and read

- `compare [--scale NAME]`: can repeat.
- `read [--plu PLU] [--id ID] [--scale NAME]`: each can repeat. With neither `--plu` nor `--id` the whole table is read.

A whole table read takes about 30 seconds at the default pace.

`preview --read` and `apply` also do one each, on each scale.

If a read gets no reply, the message says the scale may have frozen and may need a reboot. Check the scale before trying again. A read has frozen scales with damaged or old records. A gap in the ids is suspected too, but not proven.

While a scale may be frozen, the web page sends nothing to it. That is after a logged change, a reload or any read, here or on the page, got no reply from it, and no read of it has answered since. The page says "Could not connect. Tell the Scale Manager." if the last try could not connect, which can be the network or a frozen scale. It says "Did not answer. Tell the Scale Manager." if the scale connected and then stopped answering. Both count the same way. A page review leaves that scale out, and the change goes to the other scales. The scale is then listed as missing that change. `preview` and `apply` here leave out a scale that may be frozen the same way, with or without `--scale`, and so do `read`, `settle` and `mark-orphans`. `apply` checks again just before it sends. A `compare` leaves it out too, unless it is the only `--scale` given: `not_contacted` with `may_be_frozen` true, and exit 7. So `compare` with no `--scale`, or with several, does not read it. Only `compare --scale NAME` with that scale alone, or a reload, reads it.

`frozen` shows which scales are flagged, and contacts no scale. It includes a note a running server held in the state folder because the log could not take it. In `how`, `unreachable` means could not connect, `no_reply` means it connected and then gave no reply, and `unknown` means an older row that did not keep it.

Once you have looked at the scale, `compare --scale NAME` reads it once. Its `status` says what happened:

- `compared`: it answered. That clears it, even if the table is empty after a factory reset. The page uses it again at once. Then `settle` finishes anything still listed, including each change it was left out of.
- `unreachable`: it could not connect. The flag stays. It may still be off the network, or frozen.
- `read_failed` with `may_be_frozen` true: it connected and then stopped answering. The flag stays. Have someone reboot it before you try again.

Do not repeat the compare over and over. A reload clears it too.

Only a read that answers clears it. Each such read keeps its place in the change log, and a no reply logged before that place is over. The clock does not decide, so a wrong clock or a damaged time in the log cannot keep a scale flagged for good.

If you cannot clear it now, there is a way out. Turn that scale's reads and writes both off in the config, then restart the web page and the MCP server together. The page stops counting it, and staff can send changes to the other scales. That scale gets no changes at all until you turn it back on. To turn it back on, set reads and writes back on, restart both, then run `compare --scale NAME` once. The flag is still there until a read answers.

### When Bart is away

The page cannot clear a flag. That is on purpose. The Claude app on a phone cannot do it either, because the MCP server listens only on the store network, and the phone app cannot reach it. Use one of these:

- Claude Code on a computer on the store network, with the ScaleConnect MCP server.
- This command line on bartcc, or on a computer on the store network.
- From a phone: SSH to bartcc over Tailscale, then this command line, or Claude Code, there.

Then:

1. Run `frozen` to see which scale is flagged, and how.
2. Ask someone at the store to look at that scale. Is it on? Does the display work? If it seems frozen, they turn it off and on.
3. Run `compare --scale NAME` once, and read its `status` as above.
4. If it is still flagged, leave it, or use the config way out above. Until then the page leaves that scale out of every check and change, and each change it misses goes on the "Not finished" list.

### reload

**This overwrites ids 1 to N on that scale.** N is the highest id in the catalog, counting deleted items. Whatever the scale holds at those ids is replaced, whether or not it matches the catalog. It is for a person at the command line, and it is not on the web page or the MCP server.

- `--scale NAME`: required. One scale. It must be write enabled.
- `--yes`: required. Without it nothing is sent, and the command exits 2.
- `--accept-differences`: send it although the catalog differs from this scale's last known read, or nothing recent is known about it. Only use it after a person has read the list.
- `--who NAME`: who is asking.

**The catalog can be out of date, and a reload puts its prices on the scale.** So before anything goes out, it compares the catalog with this scale's last known read, on every field but the time, such as price, name, sold by and tax. It does not contact the scale for this. A read more than 15 minutes old counts as nothing known, because prices change in ScaleLink with no word to us. If there is any difference, or nothing is known about an id, it lists them, gives the time of the last read, sends nothing, and exits 6. Read that list. To find out what the scale holds now, run `compare`, if a read is safe on it, and run the reload within 15 minutes. A change made in ScaleLink after that read still cannot be seen. Then run the reload again with `--accept-differences` if you still want it. A scale fresh from a reset, or one whose read freezes, will need the flag, because nothing can be known about it.

Use it for these:

- A scale whose read freezes. Reboot it first. A full send has cured this.
- A scale fresh from a product reset. It has nothing safe to read until it is reloaded.
- A scale whose ids differ from the catalog, so a one item send would land on the wrong product. A reload puts it back in step.

What it does, in order:

1. Backs up the item list, as for any first write. It does not save what the scale holds, because that needs a read. The change log says so.
2. Checks the serial with `CX`, and sends nothing else first. It does not read the scale first.
3. Sends ids 1 to N in id order. A live item goes exactly as the catalog holds it. Any other id gets a placeholder, so no gap is left.
4. Only if every record was accepted, reads the scale and compares all 20 fields. A scale ignores connections for a while after a send, so it tries through the read back window, 45 seconds by default, until a read starts. If that fails, or a read stalls once it has started, the report says the scale may be frozen and need a reboot, and it stops. Do not read that scale again until someone has checked it.

It prints one JSON object. Read `message`, then `status`. `message` gives the time of the last read and names each price that changed from what the scale showed then. `differences` lists every field, with when that read was. Where nothing was known, it says it could not name a price. `ok` is true only if every record reads back as sent and no PLU is left on two ids. A PLU left on two ids gives the status `mismatch`. Ids above N are not touched, and `left_above` lists any real records there. If a send stops part way, `not_accepted` lists the ids, nothing is read, and the status is `unknown`. Run it again once the scale answers.

Allow a few minutes. A full send of about 480 items took 90 to 160 seconds, then comes the read. The catalog's items do not change. Each scale needs its own reload.

## Output

Output on standard output is always one JSON document, printed with an indent of 2. Prices are always text.

- `capabilities`, `frozen`, `get`, `preview`, `settle`, `apply`, `change`, `resolve` and `reload` print one JSON object.
- `scales`, `search`, `changes`, `compare` and `read` print a JSON list.
- On failure the output is:

```json
{
  "ok": false,
  "error": {
    "code": "config_error",
    "message": "No config file at C:\\nonexistent\\x.toml. See config.example.toml."
  }
}
```

A validation failure also has an `errors` list inside `error`.

- `preview` and `settle` with errors print the full preview, then exit 3.
- `apply` prints the change record. Read its `display` line and each scale's `status`. Do not look at `success` alone.
- `compare` and `read` give each scale `read_repeated`, `preview` and `settle` give `scales_read_repeated`, and `apply` gives each scale result `read_repeated`. Each says a read of that scale needed a second try.
- A bad command line also prints JSON, with `"code": "usage"`, and exits 2.
- An unexpected error prints only the error type, with `"code": "unexpected"`, and exits 1. The text of the exception is not printed, because it could hold something from the config.
- `--help` is the one exception. It prints usage text, not JSON, and exits 0.

## Scale statuses in a result

`verified`, `accepted_not_verified`, `rejected`, `unknown`, `mismatch`, `unreachable`, `read_failed`, `wrong_scale`, `drift_refused`, `skipped_not_write_enabled`. What each one means is in `docs/LIBRARY.md`. Only `verified` on every configured scale is a full success. `accepted_not_verified` is a partial result.

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | Done. For `apply`, the outcome is `success`. For `compare`, every contacted scale matches. For `read`, every contacted scale was read. For `reload`, `ok` is true |
| 1 | Unexpected error |
| 2 | Bad command line, including `reload` without `--yes` |
| 3 | Validation failed, including a `preview` or `settle` with errors, an unknown scale name, or a `reload` of a scale that is not write enabled |
| 4 | Not found: item, change or preview |
| 5 | Preview expired, stale or already applied, or an interrupted change is in the way |
| 6 | Warnings must be acknowledged. Or `reload` found differences, or nothing known, and `--accept-differences` was not given. Nothing was sent |
| 7 | `apply` ran but the outcome is not `success`. Or `compare` found differences or could not read. Or `read` could not read a scale. Or `reload` ran and `ok` is false. Read the output |
| 8 | Config problem, or no seeded catalog |
| 9 | Backup failed. Nothing was sent |
| 10 | Another apply is running. Nothing was sent |

Exit 0 from `scales`, `frozen`, `search`, `get`, `capabilities`, `change`, `changes` and `resolve` only means the command ran.

## Examples

```
scaleconnect search cheese --limit 10
scaleconnect get --plu 4001
scaleconnect preview --read --json '[{"action":"edit","target_plu":"4001","price":"12.75"}]'
scaleconnect apply PREVIEW_ID --who bart
scaleconnect compare --scale SCALE1
scaleconnect change CHANGE_ID
```

On Windows PowerShell, quoting JSON on the command line is awkward. Use `--file` or `--stdin` instead.

Do not run the command line to write to scales from another computer at the same time as the app on bartcc. The apply lock covers one computer only.
