# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""The launcher files for bartcc: no systemd, no real addresses, paths from the repo location."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
FILES = ["scaleconnect-web.sh", "scaleconnect-mcp.sh", "install-launchers.sh", "scaleconnect.desktop",
         "scaleconnect-autostart.desktop", "scaleconnect-mcp-autostart.desktop"]
EM, EN = chr(0x2014), chr(0x2013)


def read(name):
    return (DEPLOY / name).read_bytes().decode("utf-8")


def test_all_files_exist_with_unix_line_endings():
    for name in FILES:
        assert "\r" not in read(name), name
    assert (ROOT / "docs" / "INSTALL.md").exists()


def test_no_systemd_and_no_sudo():
    for name in FILES:
        text = read(name)
        assert "systemctl" not in text and not re.search(r"^\s*service\s+\w+", text, re.M), name
        assert not re.search(r"^\s*sudo\b", text, re.M), name


def test_no_real_addresses():
    allowed = {"127.0.0.1", "0.0.0.0", "100.64.0.0", "100.127.255.255"}  # refused ranges named in the docs
    for path in [*(DEPLOY / n for n in FILES), ROOT / "docs" / "INSTALL.md"]:
        for ip in re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", path.read_text(encoding="utf-8")):
            assert ip in allowed, (path.name, ip)


def test_paths_work_from_the_repo_location():
    for name in ("scaleconnect.desktop", "scaleconnect-autostart.desktop"):
        text = read(name)
        assert "$HOME/CCScaleApp/ScaleConnect/deploy/scaleconnect-web.sh" in text
        assert "/home/" not in text
    script = read("scaleconnect-web.sh")
    assert 'dirname "$0"' in script  # finds the repo from where the script is
    assert "/home/" not in script


def test_launcher_and_autostart_roles():
    menu = read("scaleconnect.desktop")
    auto = read("scaleconnect-autostart.desktop")
    assert "Type=Application" in menu and "Categories=" in menu and "--no-open" not in menu
    assert "--no-open" in auto and "NoDisplay=true" in auto


def test_script_starts_only_if_not_running_and_opens_the_page():
    script = read("scaleconnect-web.sh")
    assert "page_answers" in script and "pid_alive" in script
    assert "xdg-open" in script and "--no-open" in script


def test_text_has_no_em_dashes():
    for path in [*(DEPLOY / n for n in FILES), ROOT / "docs" / "INSTALL.md"]:
        text = path.read_text(encoding="utf-8")
        assert EM not in text and EN not in text, path.name


def test_mcp_script_and_autostart():
    script = read("scaleconnect-mcp.sh")
    for needle in ("umask 077", "mcp.pid", "mcp.log", "scaleconnect.mcp_server --http", "stat -c %a", "--force",
                   "401", "MAX_TRIES"):
        assert needle in script, needle
    auto = read("scaleconnect-mcp-autostart.desktop")
    assert "$HOME/CCScaleApp/ScaleConnect/deploy/scaleconnect-mcp.sh" in auto and "NoDisplay=true" in auto
    install = read("install-launchers.sh")
    assert "scaleconnect-mcp-autostart.desktop" in install
    assert "scaleconnect-mcp.desktop" not in install  # no menu entry or desktop icon for it


def test_install_doc_has_the_firewall_commands_without_real_addresses():
    text = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    assert "sudo ufw allow from STORE-NETWORK/24 to any port 8080 proto tcp" in text
    assert "sudo ufw allow from STORE-NETWORK/24 to any port 8765 proto tcp" in text
    assert "Tailscale" in text