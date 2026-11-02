# ScaleConnect

ScaleConnect adds and changes products on Tor-rey WiFi label printing scales, without using ScaleLink for each change.

It is for a small store that runs these scales and wants to update prices and items from a web page, from a command line, or by asking Claude through an MCP server. It was built for one deli with three scales. It has not been tried anywhere else.

## Not affiliated, and use at your own risk

- ScaleConnect is not affiliated with, endorsed by, or supported by Tor-rey.
- Tor-rey and ScaleLink are trademarks of Tor-rey. They are used here only to say which scales this works with.
- ScaleConnect never changes calibration. It only adds and edits product records. It sends nothing for zero, tare, units, firmware, factory reset, network settings or passwords.
- These are legal for trade scales. You use ScaleConnect at your own risk. Check prices on the scale after a change.
- No Tor-rey software, files or captures are part of this project. The protocol notes are written in our own words from what we observed.

## Current status

Read this first.

- ScaleConnect has not yet completed a full run on a real scale through this library. The library has been tested against a fake scale, and the message formats come from captures and tests on one test scale.
- Delete is not available yet. The delete message has not been proven, so the library reports `not_available` for any delete. Use ScaleLink to delete items.
- Changing a PLU or Code is on hold. Use ScaleLink for that too.
- Reading items back from a scale works in the lab on one test scale. Nobody has checked yet what the scale display and printed labels show for every case.
- Only scales marked write enabled in your config can be changed. Everything else is read only or left alone.
- License: not chosen yet. The owner decides.

The lead can update this section as things change.

## What it does

- Add a new item, or change the price or description of an item.
- Send each change to every configured scale, one scale at a time, and show a result for each scale.
- Read each scale first and compare, so a scale that differs from ScaleConnect's list is shown to you before anything is written.
- Read the item back after writing, so a result says `verified` only when the scale really holds the new values.
- Keep a permanent log of every change.

It never says everything worked unless every scale was changed and checked. A result of `accepted_not_verified` is a partial result, not a success.

There are three ways to use it. All three call the same library:

- A web page for staff. See `docs/STAFF.md`.
- A command line tool. See `docs/CLI.md`.
- An MCP server so Claude can update the scales. See `docs/MCP.md`.

## Install

You need Python 3.13 or newer. MX Linux 25 and Debian 13 ship Python 3.13. Older Debian releases do not, so check with `python3 --version`.

No sudo is needed except to install Python or venv support if they are missing.

1. Put the project in `~/CCScaleApp/ScaleConnect`. The launcher files expect that path.
2. Make a virtual environment and install:

   ```
   cd ~/CCScaleApp/ScaleConnect
   python3 -m venv .venv
   .venv/bin/pip install -e .
   ```

   On Debian, `python3 -m venv` may need `sudo apt install python3-venv` first.

3. Make your config file at `~/.config/scaleconnect/config.toml`. Copy `config.example.toml` and edit the copy. The config holds the scale addresses, serial numbers, the staff PIN and the MCP token. Keep it out of the repo. Use a real PIN, because the example value `change-me` counts as no PIN and the web page will not start.
4. Seed the item list. ScaleConnect keeps its own list of items in SQLite and will not run without one. See "Seeding" in `docs/LIBRARY.md`. The seed file comes from your own scale data and is not part of this project.
5. Install the launchers for the web page. See `docs/INSTALL.md`:

   ```
   cd ~/CCScaleApp/ScaleConnect/deploy
   sh install-launchers.sh
   ```

   This adds a "Scale Prices" menu entry and desktop icon, and starts the page when the desktop starts. It uses no systemd, so it works on MX Linux with SysVinit.

6. For Claude, follow `docs/MCP.md`.

Writes are off for every scale until you set `write_enabled = true` for it in the config. Each enabled scale also needs its serial number in the config, and ScaleConnect checks the serial on the scale before it sends anything.

## How to use it

Web page: open the "Scale Prices" icon, or the address you set under `[web]` in the config, in a browser on the store network. Enter the PIN, find an item, and follow the screens. Plain instructions for staff are in `docs/STAFF.md`.

Command line:

```
scaleconnect search 4001
scaleconnect preview --json '[{"action":"edit","target_plu":"4001","price":"12.75"}]'
scaleconnect apply PREVIEW_ID
```

Every change is a preview first, then an apply. Nothing is sent to a scale by a preview unless you ask it to read. All options, JSON output and exit codes are in `docs/CLI.md`.

Claude: the MCP server gives Claude tools to search, preview and apply. Claude always previews first, shows you any warning, and you decide.

## Rules ScaleConnect enforces

The scales accept almost any value, so ScaleConnect checks input before it reaches a scale.

- Price is more than 0 and at most 999.99, with two decimal places.
- PLU and Code are the same 4 digit number.
- No duplicate PLU.
- Ids stay between 1 and 500.
- A price change of more than 50 percent needs to be confirmed.

## ScaleLink and ScaleConnect together

A send from ScaleLink can overwrite what ScaleConnect set. ScaleLink's purge does not remove an item from a scale. If you use both, read `docs/STAFF.md`.

## Documents

- `docs/STAFF.md` for deli staff.
- `docs/CLI.md` for the command line.
- `docs/MCP.md` for Claude.
- `docs/INSTALL.md` for the web page install.
- `docs/LIBRARY.md` for the library interface.
- `docs/PROTOCOL.md` for what we know about the scale messages.
- `docs/DESIGN.md` for how the pieces fit.

## License

Not chosen yet.
