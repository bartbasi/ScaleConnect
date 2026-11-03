# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Start the web page: python -m scaleconnect.web [--config PATH]

Host and port come from [web] in the config. The page binds to that one address only.
It will not start without a PIN.
"""

from __future__ import annotations

import argparse
import sys

from scaleconnect.config import load_config
from scaleconnect.errors import ScaleConnectError
from scaleconnect.service import ScaleConnectService

from .app import create_app
from .security import config_mode_problem, listen_host_problem


# The edit screen lets staff change the PLU. Code always follows the PLU, so the page never
# shows Code as a field. The library still checks every PLU change and has the last word.
PLU_EDIT_ENABLED = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scaleconnect.web")
    parser.add_argument("--config", help="path to the config file")
    parser.add_argument("--print-url", action="store_true", help="print the page address from the config and exit")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.print_url:
            print(f"http://{config.web.host}:{config.web.port}/")
            return 0
        if not config.pin:
            print("No PIN is set in the config. The web page will not start without one. Set [auth] pin.", file=sys.stderr)
            return 8
        problem = (config.path and config_mode_problem(config.path)) or listen_host_problem(config.web.host)
        if problem:
            print(problem, file=sys.stderr)
            return 8
        service = ScaleConnectService.open(args.config)
        app = create_app(service, config, plu_edit_enabled=PLU_EDIT_ENABLED)
    except ScaleConnectError as exc:
        print(exc.message, file=sys.stderr)
        return exc.exit_code
    for warning in config.warnings:
        print("Note:", warning, file=sys.stderr)

    import uvicorn

    print(f"ScaleConnect web page on http://{config.web.host}:{config.web.port}/", flush=True)
    try:
        # proxy_headers off: a client must not be able to pose as another device with X-Forwarded-For.
        uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="info", proxy_headers=False)
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
