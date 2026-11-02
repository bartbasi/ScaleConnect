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
{"action": "delete", "target_plu": "4002"}
```

Delete is not available yet. A preview with a delete warns, and apply reports `not_available` for it. Changing a PLU or Code gives the error `plu_edit_held`. The full list of keys, warnings and error codes is in `docs/LIBRARY.md`.

### apply

- `PREVIEW_ID`: the `preview_id` from a preview.
- `--acknowledge-warnings`: allow every warning that needs it, such as a price more than 50 percent away, or a subset of scales. Only use it after a person has seen the warning.
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

## Output

Output on standard output is always one JSON document, printed with an indent of 2. Prices are always text.

- `capabilities`, `get`, `preview`, `settle`, `apply`, `change` and `resolve` print one JSON object.
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
- A bad command line also prints JSON, with `"code": "usage"`, and exits 2.
- An unexpected error prints only the error type, with `"code": "unexpected"`, and exits 1. The text of the exception is not printed, because it could hold something from the config.
- `--help` is the one exception. It prints usage text, not JSON, and exits 0.

## Scale statuses in a result

`verified`, `accepted_not_verified`, `rejected`, `unknown`, `mismatch`, `unreachable`, `read_failed`, `wrong_scale`, `drift_refused`, `skipped_not_write_enabled`, `not_available`. What each one means is in `docs/LIBRARY.md`. Only `verified` on every configured scale is a full success. `accepted_not_verified` is a partial result.

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | Done. For `apply`, the outcome is `success`. For `compare`, every contacted scale matches. For `read`, every contacted scale was read |
| 1 | Unexpected error |
| 2 | Bad command line |
| 3 | Validation failed, including a `preview` or `settle` with errors, or an unknown scale name |
| 4 | Not found: item, change or preview |
| 5 | Preview expired, stale or already applied, or an interrupted change is in the way |
| 6 | Warnings must be acknowledged |
| 7 | `apply` ran but the outcome is not `success`. Or `compare` found differences or could not read. Or `read` could not read a scale. Read the output |
| 8 | Config problem, or no seeded catalog |
| 9 | Backup failed. Nothing was sent |
| 10 | Another apply is running. Nothing was sent |

Exit 0 from `scales`, `search`, `get`, `capabilities`, `change`, `changes` and `resolve` only means the command ran.

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
