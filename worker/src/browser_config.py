# worker/src/browser_config.py
"""Shared browser configuration.

EGRESS_PROXY env var, when set, routes all browser HTTP/HTTPS traffic
through the given proxy. Used to bypass per-IP filtering on certain
.nic.in portals — see the proxy VM setup in ops docs.
"""

from __future__ import annotations

import os

from browser_use import Browser


def make_browser(
    *, keep_alive: bool = False, user_data_dir: str | None = None
) -> Browser:
    """Build a Browser configured identically across the codebase.

    Honors EGRESS_PROXY (e.g. 'http://10.160.0.14:8888') by passing it to
    Chromium via --proxy-server. Localhost is excluded so VNC and any
    127.0.0.1 IPC stays direct.
    """
    args: list[str] = [
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    egress_proxy = os.environ.get("EGRESS_PROXY", "").strip()
    if egress_proxy:
        args.append(f"--proxy-server={egress_proxy}")
        args.append("--proxy-bypass-list=<-loopback>;localhost;127.0.0.1")

    return Browser(
        headless=False,
        chromium_sandbox=False,
        args=args,
        keep_alive=keep_alive,
        user_data_dir=user_data_dir,
    )
