# Installing the web page on bartcc

This covers the staff web page only. It needs no sudo and no systemd. bartcc uses SysVinit, so nothing here is a service. The page is started by a small script, from the desktop session or from cron.

The repo lives at `~/CCScaleApp/ScaleConnect`. The launcher files expect exactly that path.

## What you need first

1. Python 3.13 with `fastapi` and `uvicorn` installed. The script uses `~/CCScaleApp/ScaleConnect/.venv/bin/python` if it exists, and `python3` if not.
2. A config file at `~/.config/scaleconnect/config.toml`. Start from `config.example.toml`, then lock it down, because it holds the PIN and the token:

   ```sh
   chmod 700 ~/.config/scaleconnect
   chmod 600 ~/.config/scaleconnect/config.toml
   ```

   The page will not start if other users can read the config. The start script also refuses, and says why.
3. A seeded item list. The page will not run without one. See `docs/LIBRARY.md`, "Seeding".

## The settings the page reads

```toml
[auth]
pin = "pick-a-pin"          # the one shared PIN. The page will not start without it

[web]
host = "ADDRESS-OF-THIS-PC" # one IPv4 address of bartcc, as four numbers
port = 8080
```

- Use the address of bartcc on the store network, so phones can reach it. Use `127.0.0.1` to test on bartcc only.
- The host must be one real IPv4 address, written as four numbers. These are refused on purpose: a name, an IPv6 address, a wildcard in any spelling (`0.0.0.0`, `0`, `::` and so on), and the Tailscale range (100.64.0.0 to 100.127.255.255). The page binds to the one address you name, and answers only requests that use that address and port.
- The PIN must be at least 4 characters.
- The example PIN `change-me` counts as no PIN, and the page will not start.
- Keep the PIN only in this file. Do not put it in the repo or in a message.

## Install the launchers

```sh
cd ~/CCScaleApp/ScaleConnect/deploy
sh install-launchers.sh
```

This copies three files. It needs no sudo and can be run again.

| What | Where it goes |
|---|---|
| Menu entry "Scale Prices" | `~/.local/share/applications/scaleconnect.desktop` |
| Desktop icon | `~/Desktop/scaleconnect.desktop`, if that folder exists |
| Autostart entry for the page | `~/.config/autostart/scaleconnect-autostart.desktop` |
| Autostart entry for the MCP server | `~/.config/autostart/scaleconnect-mcp-autostart.desktop` |

The menu entry and desktop icon start the page if it is not running, then open it in the browser. The autostart entry starts the page when the desktop starts, and opens nothing.

## Use the script by hand

```sh
sh ~/CCScaleApp/ScaleConnect/deploy/scaleconnect-web.sh          # start if needed, then open
sh ~/CCScaleApp/ScaleConnect/deploy/scaleconnect-web.sh start --no-open
sh ~/CCScaleApp/ScaleConnect/deploy/scaleconnect-web.sh status
sh ~/CCScaleApp/ScaleConnect/deploy/scaleconnect-web.sh stop
```

The script keeps a pid file and a log in `~/.local/state/scaleconnect/`. If the page does not start, read `web.log` there. The usual causes are a missing PIN, a wildcard host, or an item list that was never seeded.

## The MCP server

The MCP server is what Claude uses. It has its own script, in the same style. It needs `[auth] mcp_token` and an `[mcp]` host and port in the config. Use one IPv4 address of bartcc, the same rules as the page.

```sh
sh ~/CCScaleApp/ScaleConnect/deploy/scaleconnect-mcp.sh          # start if needed
sh ~/CCScaleApp/ScaleConnect/deploy/scaleconnect-mcp.sh status
sh ~/CCScaleApp/ScaleConnect/deploy/scaleconnect-mcp.sh stop
```

It works like the page script. It is safe to run twice. It tries again for about 90 seconds if the WiFi is not up. It makes private files, and refuses to start if other users can read the config. `stop` refuses while a change is being sent, and `stop --force` overrides that. The pid file and log are `mcp.pid` and `mcp.log` in `~/.local/state/scaleconnect/`. The script says it is up when the server answers 401 to a request with no token. See `docs/MCP.md` for connecting Claude.

## Open the firewall for the store network

bartcc has a firewall (ufw) that drops everything by default. Until it is opened, other computers cannot reach the page or the MCP server. This needs sudo, so Bart runs these two commands himself:

```sh
sudo ufw allow from STORE-NETWORK/24 to any port 8080 proto tcp
sudo ufw allow from STORE-NETWORK/24 to any port 8765 proto tcp
```

Replace `STORE-NETWORK/24` with the store network range, written as the first three numbers of the store addresses, then `.0/24`. The repo does not hold store addresses, so look them up on bartcc. If the config uses other ports, use those ports.

The rule is limited to the store network on purpose. It keeps both ports off Tailscale and every other network bartcc is on. Do not open them to anywhere.

To check it, go to another computer or a phone on the store network and open `http://ADDRESS-OF-BARTCC:8080/`. The page should load. For the MCP server, the same address with port 8765 and path `/mcp` should answer 401 without a token. From a device that is not on the store network, neither should answer.

## When it starts

Both autostart entries run when Bart's desktop session starts. It does not run at boot with nobody logged in. If bartcc must serve phones before anyone logs in, add one line to the user's crontab. Run `crontab -e` and add:

```
@reboot sh $HOME/CCScaleApp/ScaleConnect/deploy/scaleconnect-web.sh start --no-open
```

The script is safe to run twice. It starts the page only if it is not already answering. When started with `--no-open`, it tries again every 5 seconds for about 90 seconds, in case the WiFi is not up yet.

## PLU editing

PLU editing is on. `PLU_EDIT_ENABLED` is `True` near the top of `src/scaleconnect/web/__main__.py`, so the edit screen lets staff change the PLU. Code always follows the PLU, and the page never shows it as a field. The library still checks every PLU change, and a change it refuses shows its reason. To turn the PLU box back to read only, set the value to `False` and restart the page.

## Limits to know about

- **It is plain HTTP.** There is no certificate. Anyone on the store network who can watch traffic could read the PIN as it is typed. The router keeps the page off the internet, and customers cannot join the store WiFi. Do not forward a port to this page, and do not put it behind a public address.
- **One shared PIN.** Everyone uses the same PIN, so the change log shows "web" and not a person.
- **Sessions are kept in memory.** Restarting the page signs everyone out. They enter the PIN again.
- **Wrong PINs lock out for a short time.** After five wrong tries from one device, that device waits 60 seconds. After 20 wrong tries from all devices together, everyone waits 60 seconds. So a device on the WiFi can keep staff out for a minute at a time. The log shows nothing of the PIN.
- **Do not stop the page during a send.** `scaleconnect-web.sh stop` refuses while a change is going to the scales. `stop --force` overrides that. A change cut off that way is left as stopped, and it blocks its items until Bart looks at the scales and closes it on the page.
- **Bart only tools.** The page has two screens for Bart. "Fix this difference" on an item settles which side is right when a scale differs. "Mark a scale number as taken" records that a scale holds something at a number our list does not know, so a new item can replace it. Both need the PIN, and both are for Bart, not for staff.
- **One send at a time.** While a change is going to the scales, a second change is refused until it finishes. A phone can lock its screen during a send. Open the page again, and use "Recent changes" or "See the last change I sent" to see the result.
- **Launcher quoting.** The `.desktop` files start the script with `sh -c` and `$HOME`. This works in Xfce, the MX Linux default. If a different desktop shows no icon or does nothing, run the script by hand and tell the lead.
