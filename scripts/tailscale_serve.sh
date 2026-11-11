#!/bin/zsh
# After the owner has signed in to Tailscale: let devices on his private Tailscale network (the loved one's phone) reach ONLY the phone
# receiver (127.0.0.1:8770) and Echo Link (8771, phones sign in with their own key). Nothing else on the Mac is reachable. Run once; Tailscale remembers it.
TS=(/opt/homebrew/bin/tailscale --socket="$HOME/Echo/data/tailscale/tailscaled.sock")
$TS status >/dev/null || { echo "Tailscale isn't signed in yet: run $TS up"; exit 1; }
$TS serve --bg --tcp=8770 tcp://127.0.0.1:8770                       # http://<Mac's 100.x address>:8770
$TS serve --bg --tcp=8771 tcp://127.0.0.1:8771                       # Echo Link (phones, TLS + pinned certificate)
# (An https://….ts.net name would need HTTPS turned on in the Tailscale admin page; it waits for that, so it's not done here.
#  The connection is already encrypted by Tailscale.)
$TS serve status
