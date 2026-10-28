# Backup and Recovery

## What must never be lost
1. **Original recordings and photos**: external SSD `/Volumes/External storage/EchoPersona-Data/` (`raw/`, `consent/`).
   Everything else (voice references, face frames, atlases, transcripts) can be rebuilt from these.
2. **Memories and conversations**: `~/EchoPersona/data/memory.sqlite`
3. **the owner's approved personality**: `~/EchoPersona/data/personality/` (`draft.json`, `profile.json`, `evals.jsonl`)
4. Settings: `~/EchoPersona/config/voice.json` and any `config/*.yaml` you created.

## How it is backed up now (three copies)
| Copy | What | How |
|---|---|---|
| 1. external SSD | the dataset | where it is recorded |
| 2. `~/EchoPersona/data/dataset-backup/` on the Mac's internal drive | the dataset | `~/EchoPersona/scripts/backup_dataset.sh` (copies, then verifies every recording's SHA-256) |
| 3. Time Machine (any backup drive) | the internal drive, including copy 2, memories, personality, code | automatic |

Time Machine **excludes the external SSD**, which is why copy 2 exists. **Run `backup_dataset.sh` after every recording
session or phone copy.** It prints e.g. `backup verified: 172 recordings OK (studio + phone), 0 mismatched`.

Extra memory snapshot at any time: Echo window → What I remember → **Back up** (writes `data/backups/memory-<date>.sqlite`).

**Off-site copy (recommended):** plug in a spare drive and run
```
~/EchoPersona/scripts/offsite_backup.sh /Volumes/<DriveName>
```
The first time it asks you to choose a password and creates `Echo-Offsite.sparsebundle` (AES-256 encrypted) on that
drive; later runs ask for the password and copy only what changed. It holds the original recordings and photos, derived
data, memories, personality, settings, the background-service settings and the code, and it verifies every recording
against its checksum. **The password is never stored anywhere: write it down and keep it safe.** Keep the drive outside
the house and refresh it every month or two (the Status page shows its age).
To open it later: double-click `Echo-Offsite.sparsebundle` and enter the password.

## Restoring
**The external SSD failed:** get a new drive, name it `External storage`, then
```
mkdir -p "/Volumes/External storage/EchoPersona-Data"
rsync -a ~/EchoPersona/data/dataset-backup/ "/Volumes/External storage/EchoPersona-Data/"
```
Restart Echo (log out and in, or `launchctl kickstart -k gui/$(id -u)/com.echopersona.voice` and `…core`).

**Memories got damaged or deleted:** stop Echo Persona Core, restore a snapshot, start it again:
```
launchctl bootout gui/$(id -u)/com.echopersona.core
cd ~/EchoPersona && PYTHONPATH=. uv run python -c "from memory.store import MemoryStore as M; M.restore('data/backups/memory-YYYYMMDD-HHMMSS.sqlite', 'data/memory.sqlite')"
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.echopersona.core.plist
```
(Or restore `~/EchoPersona/data/memory.sqlite` from Time Machine.)

**The whole Mac was lost:** restore your user account from Time Machine (Migration Assistant), plug in the SSD (or
restore it from copy 2 as above), then follow PC_SETUP.md from "Re-create after a restore".

## Checking backups are good
```
~/EchoPersona/scripts/backup_dataset.sh          # verifies every recording in copy 2 against its checksum
# when Time Machine last ran: System Settings → General → Time Machine (Terminal needs Full Disk Access for tmutil)
```
