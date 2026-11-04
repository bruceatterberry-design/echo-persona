"""Automatic LAN discovery (BRIDGE_PROTOCOL.md section 2): UDP beacons on port 47800.

Beacons are only hints ("try this IP"); trust comes from the paired key.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import subprocess
import time

log = logging.getLogger("dream_bridge.discovery")

PORT = 47800
INTERVAL_S = 5.0
STALE_S = 30.0
IP_BOUND_IF = 25  # macOS: pin a socket to one interface (netinet/in.h)


def interface_ip(iface: str) -> tuple[str, str, ipaddress.IPv4Network] | None:
    """(ip, broadcast, network) of a macOS interface, or None if it has no IPv4 address."""
    try:
        out = subprocess.run(["ifconfig", iface], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if parts[:1] == ["inet"] and "netmask" in parts:
            ip = parts[1]
            mask = int(parts[parts.index("netmask") + 1], 16)
            net = ipaddress.IPv4Network(f"{ip}/{ipaddress.IPv4Address(mask)}", strict=False)
            return ip, str(net.broadcast_address), net
    return None


def parse_beacon(data: bytes, src_ip: str, own_id: str, lan: ipaddress.IPv4Network) -> dict | None:
    """Validate a beacon from a tower. Anything odd is ignored."""
    if len(data) > 2048:
        return None
    try:
        b = json.loads(data)
        if ipaddress.IPv4Address(src_ip) not in lan:
            return None
    except Exception:
        return None
    if not isinstance(b, dict) or b.get("svc") != "echo-dream" or b.get("role") != "tower":
        return None
    did, name, port = b.get("device_id"), b.get("name"), b.get("port")
    if not (isinstance(did, str) and 0 < len(did) <= 64 and did != own_id):
        return None
    if not (isinstance(port, int) and 1 <= port <= 65535):
        return None
    pw = b.get("paired_with")
    return {
        "device_id": did,
        "name": name[:64] if isinstance(name, str) else "?",
        "ip": src_ip,
        "port": port,
        "proto": b.get("proto") if isinstance(b.get("proto"), int) else None,
        "pairing_open": b.get("pairing_open") is True,
        "paired_with": pw if isinstance(pw, str) else None,
        "seen": time.time(),
    }


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, own_id: str, iface: str, beacon_fn):
        self.own_id = own_id
        self.iface = iface
        self.beacon_fn = beacon_fn   # () -> dict, the Mac's current beacon
        self.towers: dict[str, dict] = {}
        self.transport = None
        self.lan = ipaddress.IPv4Network("0.0.0.0/32")

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        t = parse_beacon(data, addr[0], self.own_id, self.lan)
        if t:
            if t["device_id"] not in self.towers or self.towers[t["device_id"]]["ip"] != t["ip"]:
                log.info("tower seen: %s %s at %s:%s", t["name"], t["device_id"], t["ip"], t["port"])
            self.towers[t["device_id"]] = t

    def live_towers(self) -> list[dict]:
        now = time.time()
        return [t for t in self.towers.values() if now - t["seen"] < STALE_S]

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        rx.bind(("", PORT))   # broadcasts only arrive on a wildcard bind; we only parse JSON
        await loop.create_datagram_endpoint(lambda: self, sock=rx)

        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        tx.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(self.iface))  # wired port only
        tx.setblocking(False)
        while True:
            addr = interface_ip(self.iface)
            if addr:
                ip, bcast, self.lan = addr
                msg = json.dumps(self.beacon_fn()).encode()
                for dest in (bcast, "255.255.255.255"):
                    try:
                        tx.sendto(msg, (dest, PORT))
                    except OSError as e:
                        log.debug("beacon to %s failed: %s", dest, e)
            await asyncio.sleep(INTERVAL_S)
