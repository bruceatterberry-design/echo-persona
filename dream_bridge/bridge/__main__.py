"""Command line for the Mac side of the Echo Dream Bridge.

  python -m bridge serve      run the service (launchd does this)
  python -m bridge status     what the bridge sees
  python -m bridge pair       pair with the tower (4-digit code on both screens)
  python -m bridge wake       send Wake-on-LAN to the paired tower
  python -m bridge unpair     forget the tower
  python -m bridge preview    show what the next memory batch would contain (sends nothing)
  python -m bridge go         "all systems go": switch memory export on and send the first batch
  python -m bridge stop       switch memory export off
  python -m bridge dreams     dream proposals waiting for the owner
  python -m bridge approve ID / reject ID [note]   decide one (approved = approved, not yet applied to Echo)
  python -m bridge retire ID  switch an applied dream insight off in Echo
  python -m bridge unlink A B remove the dream link between memories A and B
  python -m bridge check-synthetic FILE   check a Synthetic Dreams file before taking it to the tower
"""
import asyncio
import json
import sys

import httpx

CTL = "http://127.0.0.1:47803"


def call(method: str, path: str, timeout: float = 15, **kw):
    try:
        r = httpx.request(method, CTL + path, timeout=timeout, **kw)
    except httpx.ConnectError:
        sys.exit("The dream bridge isn't running. Start it with: launchctl kickstart gui/$(id -u)/com.echopersona.dreambridge")
    if r.status_code >= 400:
        d = r.json().get("detail", r.text)
        sys.exit(d if isinstance(d, str) else json.dumps(d, indent=1))
    return r.json()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "serve":
        from .service import main as serve
        asyncio.run(serve())
    elif cmd == "status":
        print(json.dumps(call("GET", "/status"), indent=1))
    elif cmd == "pair":
        params = {"device_id": sys.argv[2]} if len(sys.argv) > 2 else None
        s = call("POST", "/pair/start", params=params)
        print(f"\n   Pairing code:  {s['code']}\n\n   Tower: {s['tower']} ({s['ip']})")
        ok = input("   Does the tower show the SAME 4 digits? Type y and press Return: ").strip().lower()
        if ok != "y":
            call("POST", "/pair/cancel")
            sys.exit("Cancelled. Nothing was saved.")
        print("   Now click Match on the tower (waiting up to 5 minutes)...")
        r = call("POST", "/pair/confirm", timeout=320)
        print(f"   Paired with {r['tower']}. The link is encrypted from now on.")
    elif cmd == "wake":
        print(call("POST", "/wake"))
    elif cmd == "preview":
        print(json.dumps(call("GET", "/export/preview"), indent=1))
    elif cmd == "go":
        call("POST", "/export/enable")
        print(json.dumps(call("POST", "/export/send", timeout=60), indent=1))
    elif cmd == "stop":
        print(call("POST", "/export/disable"))
    elif cmd == "dreams":
        d = call("GET", "/dreams")
        print(f"dreams: {d['dreams']}  pending: {d['pending'] if isinstance(d['pending'], int) else len(d['pending'])}  "
              f"approved: {d['approved']}  insights in Echo: {d['applied']}  rejected: {d['rejected']}  failed checks: {d['invalid']}")
        for p in d["pending"] if isinstance(d["pending"], list) else []:
            flag = "  [PROTECTED: needs extra care]" if p["protected"] else ""
            print(f"\n  {p['proposal_id']}  ({p['kind']}, score {p['weighted']}){flag}\n    {p['proposed_change']}\n    why: {p['reason']}\n    from: {', '.join(p['refs'])}")
    elif cmd == "unlink" and len(sys.argv) > 3:
        print(call("POST", f"/links/{sys.argv[2]}/{sys.argv[3]}/remove"))
    elif cmd == "retire" and len(sys.argv) > 2:
        print(call("POST", f"/dreams/{sys.argv[2]}/retire"))
    elif cmd in ("approve", "reject") and len(sys.argv) > 2:
        print(call("POST", f"/dreams/{sys.argv[2]}/{cmd}", params={"note": " ".join(sys.argv[3:])}))
    elif cmd == "check-synthetic" and len(sys.argv) > 2:
        from .synthetic import main as check
        sys.exit(check(sys.argv[2]))
    elif cmd == "unpair":
        print(call("POST", "/unpair"))
    else:
        print(__doc__)


main()
