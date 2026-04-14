# Crimson Desert — JSON Mod Manager (macOS)

A command-line tool for applying **JSON modpatch** files to **Crimson Desert** on macOS. Patches are merged into a **safe overlay** (`0036/`); original archives in `0008/` are not modified. The game loads the overlay via `meta/0.papgt` (with `0.papgt.bak` created on first apply).

## How It Works

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│ mods/enabled/   │────>│ Merge patches│────>│ LZ4 patch   │────>│ Write 0036/  │
│  *.json         │     │ per game_file│     │ (per file)  │     │ + 0.papgt    │
└─────────────────┘     └──────────────┘     └─────────────┘     └──────────────┘
        │                                    read 0008/ .paz
        ▼
   install / activate / toggle / wizard
```

1. **Load** — Reads `.json` / `.modpatch` from `mods/enabled/` (active set).
2. **Merge** — Groups `changes` by `game_file`.
3. **Resolve** — Uses `0008/0.pamt` to locate data in `0008/*.paz`.
4. **Patch** — LZ4 decompress → match `original` at `offset` → write `patched` → recompress.
5. **Emit** — Writes `0036/0.paz`, `0036/0.pamt`, updates `meta/0.papgt` (PA checksums).

**Restore** removes `0036/` and restores `meta/0.papgt` from backup (it does not delete anything in `mods/enabled/`). **Reset** runs that plus clears `mods/enabled/`. If `mods/enabled/` is empty, **apply** only prints a hint — use **restore** to remove a leftover overlay.

### Mod lifecycle directories

- `mods/available/` — installed archive copies (not active yet)
- `mods/enabled/` — active mods used by `apply`
- `mods/disabled/` — temporarily disabled mods (non-destructive)
- `mods/_mod_state.json` — per-mod patch toggle state

## Requirements

- **macOS** (App Store / standalone `.app` layout)
- **Python 3.9+**
- **`lz4`** (`pip3 install -r requirements.txt`)
- **Crimson Desert** — the game bundle must be writable for `apply` (see [Troubleshooting](#troubleshooting))

## Installation

```bash
python3 -m pip install -r requirements.txt
chmod +x mod_manager.py
```

Clone or download this project, then run commands from that folder (or `python3 /path/to/mod_manager.py …`).

## Usage

### Set game path (first time)

Auto-detection tries `/Applications/Crimson Desert.app`, `~/Applications/…`, and common Steam paths.

```bash
./mod_manager.py set-game "/Applications/Crimson Desert.app"
```

The path is saved in `_game_path.json` (optional). Override with `--game <path>` on any command or set `CRIMSON_DESERT_GAME`.

### Wizard

```bash
./mod_manager.py wizard
```

Guided flow: install to `mods/available/` + `mods/enabled/`, list available/enabled/disabled, activate/disable/re-enable mods, inspect patches, toggle per patch/category, apply, restore, purge, change game path, start game.

### Install mods

```bash
./mod_manager.py install ./mods/stamina_json_v1.02.00
./mod_manager.py install ./mods/stamina_json_v1.02.00 --apply
./mod_manager.py install ./mods/stamina_json_v1.02.00 -a --game "/Applications/Crimson Desert.app"
./mod_manager.py install ~/Downloads/stamina_json_v1.02.00 --pick --apply
```

Copies `*.json` / `*.modpatch` into both `mods/available/` (archive) and `mods/enabled/` (active), skipping names starting with `_`. Subfolders are included unless `--no-recursive`. If a folder contains **several** mod files, use **`--pick`** for an interactive menu (choose one variant — e.g. stamina). For scripts without a TTY, **`--max-files N`** installs the first `N` files in sorted-by-name order (not both `--pick` and `--max-files`).

**Tip:** `install --apply` (`-a`) copies and patches in one step. Use `--force` to overwrite files already in `mods/enabled/`. If you install multiple files without `--pick`, the tool warns you when more than one is copied — keep only one active mod per `game_file`.

Only one modpack that patches the same `game_file` should be active at a time.

### Scan (preview, no writes)

```bash
./mod_manager.py scan ./mods/stamina_json_v1.02.00
./mod_manager.py scan ~/Downloads/SomeMod --game "/Applications/Crimson Desert.app"
```

### Status

```bash
./mod_manager.py status
```

lz4 check, enabled mod list, game `packages` path, PAMT health, writability, overlay/backup flags. On macOS, prints an `open "…"` line for Finder.

### Lifecycle / patch toggle / apply

```bash
./mod_manager.py list
./mod_manager.py available
./mod_manager.py disabled
./mod_manager.py activate 1                # available -> enabled
./mod_manager.py enable 1                  # disabled -> enabled
./mod_manager.py apply
./mod_manager.py restore                   # vanilla overlay; leaves mods/enabled/ unchanged
./mod_manager.py disable 1
./mod_manager.py disable stamina_v1.02.00_infinite
./mod_manager.py remove 1                  # disable + sync game (apply, or restore if last mod)
./mod_manager.py purge stamina_v1.02.00_10pct
./mod_manager.py patches 1
./mod_manager.py toggle 1 "[Flight]"
./mod_manager.py toggle-category 1 Flight off
./mod_manager.py reset                     # restore + clear mods/enabled/ (prompt)
./mod_manager.py reset -y                  # same, no prompt (scripts)
./mod_manager.py start-game
```

**Three states:** `available` stores source copies, `enabled` is active, `disabled` is parked. **`apply`** builds the overlay from `mods/enabled/` only. **`restore`** removes the overlay and restores `meta/0.papgt` from backup — it does **not** delete files in `mods/enabled/`. If `mods/enabled/` is empty, **`apply`** does not touch the game and tells you to run **`restore`** to drop the overlay. **`disable`** is non-destructive (`enabled -> disabled`), **`enable`** restores (`disabled -> enabled`), **`purge`** permanently deletes.

Patch toggles are stored in `mods/_mod_state.json` and applied at runtime; original mod JSON files are not modified.

After `apply`, `restore`, `remove`, or `reset`, restart the game when the game data changed.

**`reset`:** In an interactive terminal you get a confirmation prompt. If stdin is not a TTY (pipes, some IDE tasks), you must pass **`-y`** or the command aborts — this avoids accidental full resets.

### Legacy flags

Only **`--list`**, **`--apply`**, **`--restore`**, and **`--uninstall`** are accepted with a leading `--` (`--uninstall` is the same as **`--restore`**). Any other `--something` exits with an error (use subcommands without a leading `--`, e.g. `apply`, `restore`).

```bash
./mod_manager.py --list
./mod_manager.py --apply
./mod_manager.py --restore
./mod_manager.py --uninstall
./mod_manager.py --game "/Applications/Crimson Desert.app" --apply
```

### Options

| Flag | Description |
|------|-------------|
| `--game <path>` | `.app` or `packages` folder (if not auto-detected / saved) |
| `install --apply` / `-a` | Run `apply` after copying |
| `install --force` / `-f` | Overwrite existing files in `mods/enabled/` (and refresh archive in `mods/available/`) |
| `install --no-recursive` | Only top-level `.json` / `.modpatch` in the given folder |
| `install --pick` | Interactive menu when several mod files are in the folder (pick one) |
| `install --max-files N` | Non-interactive: copy at most `N` mod files (sorted by name); cannot combine with `--pick` |
| `reset -y` / `--yes` | Confirm reset without prompt (required when stdin is not a TTY) |

Use `./mod_manager.py COMMAND -h` for subcommand help.

## Quick test transcript

Use this as a fast sanity pass after updating the tool:

```bash
./mod_manager.py available
./mod_manager.py list
./mod_manager.py disable 1
./mod_manager.py disabled
./mod_manager.py enable 1
./mod_manager.py patches 1
./mod_manager.py toggle 1 "[Flight]"
./mod_manager.py patches 1
./mod_manager.py toggle-category 1 Flight on
./mod_manager.py apply
./mod_manager.py status
./mod_manager.py restore
```

Expected high-level results:

- `disable` moves file from `mods/enabled/` to `mods/disabled/` (no deletion).
- `enable` moves the same file back to `mods/enabled/`.
- `toggle` / `toggle-category` change ON/OFF status without modifying the original mod JSON.
- `apply` builds the overlay from enabled patches only.
- `restore` removes overlay `0036/` and restores `meta/0.papgt` from backup.

## Mod JSON format (modpatch)

```json
{
  "name": "my_mod",
  "patches": [
    {
      "game_file": "gamedata/skill.pabgb",
      "changes": [
        {
          "offset": 12345,
          "original": "aabbccdd",
          "patched": "11223344",
          "label": "optional"
        }
      ]
    }
  ]
}
```

If the game build changed, `original` bytes may not match → that change is **skipped** and logged.

Example stamina packs for **game data v1.02.00** live under `mods/stamina_json_v1.02.00/` — pick **one** variant and `install` it.

## Example: stamina (v1.02.00)

```bash
./mod_manager.py set-game "/Applications/Crimson Desert.app"
./mod_manager.py scan ./mods/stamina_json_v1.02.00
./mod_manager.py install ./mods/stamina_json_v1.02.00 --apply
./mod_manager.py status
./mod_manager.py restore
```

## Game file structure (macOS)

| | Location |
|---|----------|
| App | `/Applications/Crimson Desert.app` |
| **Packages root** (what the tool uses) | `…/Contents/Resources/packages/` |
| Vanilla data | `packages/0008/` (`0.pamt`, `0.paz`, …) |
| Overlay | `packages/0036/` (created by this tool) |
| Group list | `packages/meta/0.papgt` |

## Technical details

- **LZ4** block compression via `lz4.block`; PAMT flags aligned with game records.
- **`pa_checksum.py`** — Pearl Abyss Jenkins-style hash for `0.pamt` / `0.papgt` CRC fields.
- **`pamt_patcher.py`** — Parses `0.pamt`, resolves VFS paths for patches.

## Troubleshooting

### Permission denied (do not use `sudo python3`)

Prefer owning the app bundle:

```bash
sudo chown -R "$(whoami)" "/Applications/Crimson Desert.app"
```

Or install the game under `~/Applications` and `set-game` there.

### Many `SKIP` lines when applying

The mod JSON targets a different game build. You need an updated modpatch or refreshed offsets/`original` hex.

### Game issues after modding

```bash
./mod_manager.py restore --game "/Applications/Crimson Desert.app"
```

The tool never modifies `0008/`; if `.bak` is missing, repair/reinstall the game as needed.

### Gatekeeper / “damaged” app (at your own risk)

```bash
xattr -cr "/Applications/Crimson Desert.app"
```

## License

[MIT](LICENSE)

## Credits

- **Pearl Abyss** — *Crimson Desert*
- Community JSON modpatch / overlay tooling
