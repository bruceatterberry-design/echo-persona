#!/bin/zsh
# Test: Echo's open window says this text (cues like "(smiles)" drive the face, never spoken). Not stored.
# Usage: scripts/say_test.sh "Hey (smiles). I can be sad (sad)."
python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "$1" |
  curl -s -X POST 127.0.0.1:8765/api/say -H 'Content-Type: application/json' --data @-; echo
