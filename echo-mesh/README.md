# Echo Mesh

A private network of computers that share spare computing time, so a group of ordinary machines can do work none of them
could do alone. It is **invite-only**: a computer joins only with the owner's Yes, shown as a matching 4-digit number.

Each computer keeps running its own AI and keeps its own private information. The Mesh moves model requests and their
answers between computers; it never moves anyone's memories, personal files or recordings, and it never runs commands on a
computer. **Read the full story, with charts, in [`docs/ECHO_MESH.md`](../docs/ECHO_MESH.md).**

## What's here
- `mesh/`: the coordinator (the hub) and the node agent. Python standard library only.
- `mesh/winapp*.py`, `installers/windows/`: the Windows setup wizard and tray program (tkinter; pystray and Pillow for the tray icon).
- `installers/arch/`: a one-command Linux installer (systemd user service).
- `tools/`: `ask_all.py`, `fanout.py`, `meshcode.py` (Claude vs Mesh race, spread mode), `meshcoded.py`, `meshmcp.py`, and the `mclaude` launcher.
- `integrations/echo/`: the reference client for letting Echo hand small side-requests to the Mesh.
- `docs/`: the protocol and the design notes. `tests/`: 228 tests (`python3 -m unittest discover -s tests -t .`; the screen tests need a Python with tkinter).

## Quick start
```bash
python3 -m mesh.coordinator                       # hub: prints its ports and certificate fingerprint
sh installers/arch/install.sh                     # on a Linux laptop: finds the hub, shows a 4-digit number, waits for your Yes
python3 tools/ask_all.py --list                   # who is online and which models they have
python3 tools/meshcode.py "task" --files file.py  # needs a test suite; Claude Code and the Mesh race, your tests decide
```

## Safety in one paragraph
Join needs a matching number on both screens. Jobs are model requests only (`generate`, `ping`). Each computer enforces its
own share limit and puts its owner's work first. A limited client token lets a program hand over text jobs and read its own
answers, and nothing else. Private text (names, school and contact details) is never offered to the Mesh by the Echo client.
Coding tools work in scratch copies; nothing in your real folder changes until you apply a diff.

License: MIT.
