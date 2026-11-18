"""Terminal setup for Linux (Omarchy / Arch): the same three steps as the Windows installer, in a terminal: splash, terms and disclaimer, Mesh configuration.
It reuses the one legal text (agreement.TERMS) and the one set of roles (common.ROLES), and records the answers in the agent's policy.json,
exactly where the Windows installer records them. Nothing leaves this computer.

    python3 -m mesh.cli_setup --dir ~/.echo-mesh-agent                  interactive
    python3 -m mesh.cli_setup --dir DIR --accept-terms --role worker    no questions (the person has already read the terms elsewhere)
Exit code 0 = terms accepted and a role saved; 1 = declined."""
import argparse, json, os, shutil, subprocess, sys, time
from .agreement import TERMS, TERMS_READ, TERMS_VERSION
from .common import ROLES

CYAN, DIM, BOLD, OFF = "\033[96m", "\033[2m", "\033[1m", "\033[0m"
RING = r"""
            ▄▄▄██████████▄▄▄
         ▄██▀▀▀          ▀▀▀██▄
       ▄██▀                  ▀██▄
      ██▀                      ▀██
      ██                        ██
      ██▄                      ▄██
       ▀██▄                  ▄██▀
         ▀██▄▄▄          ▄▄▄██▀
            ▀▀▀██████████▀▀▀
"""
CHOICES = (("worker", "Worker node  (recommended)", "Shares this computer's spare power for jobs the Mesh sends. The right choice for a computer without an AI of its own."),
           ("peer", "Peer node  (has its own AI)", "This computer already runs an AI of its own. It keeps its own identity, and on its own dashboard it is the Primary while the Mesh owner's system appears as the Hub."))


def splash(out=sys.stdout, pause=0.0):
    out.write(CYAN + RING + OFF)
    out.write(BOLD + "          E C H O   M E S H" + OFF + "\n")
    out.write(DIM + "     Distributed AI. Infinite Possibilities.\n" + OFF + "\n")
    out.flush()
    time.sleep(pause)


def _policy_path(d): return os.path.join(d, "policy.json")


def load(d):
    try:
        with open(_policy_path(d)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(d, pol):
    os.makedirs(d, exist_ok=True)
    tmp = _policy_path(d) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pol, f)
    os.replace(tmp, _policy_path(d))


def record(d, role=None, accept_terms=False, autostart=None):
    """Write the answers. A role that is not in ROLES is refused: a computer can ask to be a worker or a peer, never the primary."""
    pol = load(d)
    if accept_terms:
        pol["terms_accepted"] = {"version": TERMS_VERSION, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if autostart is not None:
        pol["autostart"] = bool(autostart)
    if role is not None:
        if role not in ROLES:
            raise ValueError("role must be one of: " + ", ".join(ROLES))
        pol["role"] = role
    save(d, pol)
    return pol


def show_terms(out=sys.stdout):
    pager = shutil.which("less")
    if pager and out is sys.stdout and sys.stdout.isatty():
        subprocess.run([pager, "-R"], input=TERMS.encode(), check=False)            # scrolls; q to leave
    else:
        out.write(TERMS + "\n")


def check_ai(out=sys.stdout, test=True, mesh_models=None):
    """The model check (same detection as the Windows installer): find the AI already on this computer, list its models, test one with a tiny question.
    It never downloads anything. Returns True if a usable AI answered."""
    from . import winapp_core as W
    found = W.detect_runtimes()
    usable = [r for r in found if r["models"]]
    out.write("\nChecking for an AI on this computer...\n")
    if usable:
        for r in usable:
            out.write("  " + W.describe_models(r, mesh_models) + "\n")
        if test:
            out.write(f"  Asking {W.pick_model(usable[0]['models'])} one tiny question (it can take a moment)...\n")
            ok, msg = W.test_runtime(usable[0])
            out.write(("  OK: " if ok else "  It did not pass: ") + msg + "\n")
            return ok
        return True
    if found:
        out.write("  Ollama is running but has no model installed yet. This installer downloads nothing; add a model yourself when you like.\n")
    elif W.find_ollama_exe():
        out.write("  Ollama is installed but not running. Start it (ollama serve) and the Mesh will use it by itself.\n")
    else:
        out.write("  No AI found. That is fine: this computer can still take jobs that do not need a model.\n")
    return False


def run(d, role=None, accept_terms=False, ask=input, out=sys.stdout, pause=1.2, check=None, autostart=None):
    """-> True when the terms are accepted and a role is saved, False if declined."""
    splash(out, pause if not accept_terms else 0)
    if not accept_terms:
        show_terms(out)
        out.write("\n" + TERMS_READ + "\n")
        if ask("Type  I AGREE  to continue, or anything else to decline: ").strip().upper() != "I AGREE":
            out.write("Declined. Nothing was installed or changed.\n")
            return False
    if role is None and ask is input and not sys.stdin.isatty():
        role = "worker"                                   # nobody to ask (a script or a service): the recommended choice
    if role is None:
        out.write("\nChoose this computer's Mesh configuration:\n")
        for i, (val, title, blurb) in enumerate(CHOICES, 1):
            out.write(f"  {i}) {BOLD}{title}{OFF}\n     {DIM}{blurb}{OFF}\n")
        pick = ask("Number [1]: ").strip() or "1"
        role = CHOICES[int(pick) - 1][0] if pick in ("1", "2") else "worker"
    if autostart is None and "autostart" not in load(d):
        if ask is input and not sys.stdin.isatty():
            autostart = True                                   # nobody to ask: the recommended choice
        else:
            autostart = ask("Start Echo Mesh automatically every time this computer boots? [Y/n]: ").strip().lower() not in ("n", "no")
    record(d, role, accept_terms=True, autostart=autostart)
    out.write(f"Saved: {role} node, " + ("starts at every boot" if load(d).get("autostart", True) else "starts only when you start it") + ". The owner of the Mesh still approves every computer.\n")
    if check is None:
        check = not os.environ.get("ECHOMESH_NO_MODEL_CHECK")
    if check:
        ok = check_ai(out)
        if ok and (ask is not input or sys.stdin.isatty()):          # only with a person there to say yes or no
            from . import speedup, winapp_core as W
            speedup.offer(d, ask, out, models=[m for r in W.detect_runtimes() for m in r["models"] if r.get("kind") == "ollama"])
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--dir", required=True); ap.add_argument("--role", choices=ROLES); ap.add_argument("--accept-terms", action="store_true"); ap.add_argument("--autostart", choices=("yes", "no"))
    a = ap.parse_args(argv)
    return 0 if run(a.dir, a.role, a.accept_terms, autostart=None if a.autostart is None else a.autostart == "yes") else 1


if __name__ == "__main__":
    sys.exit(main())
