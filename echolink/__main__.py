"""Run Echo Link: the phones' door (TLS, 0.0.0.0:8771) + the Mac-only page (127.0.0.1:8772) + network discovery.

    uv run python -m echolink          (LaunchAgent com.echopersona.echolink keeps it running)
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from echolink.app import admin_app, public_app
from echolink.link import ADMIN_PORT, PORT, Advertiser, Link


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    link = Link()
    adv = Advertiser(link.identity["name"], link.identity["mac_id"])
    adv.start()
    public = uvicorn.Server(uvicorn.Config(public_app(link), host="0.0.0.0", port=PORT,  # noqa: S104 (phones need it)
                                           ssl_certfile=str(link.cert_path), ssl_keyfile=str(link.key_path),
                                           access_log=False, log_level="warning", ws_ping_interval=15))
    admin = uvicorn.Server(uvicorn.Config(admin_app(link), host="127.0.0.1", port=ADMIN_PORT,
                                          access_log=False, log_level="warning"))
    try:
        await asyncio.gather(public.serve(), admin.serve())
    finally:
        adv.close()


if __name__ == "__main__":
    asyncio.run(main())
