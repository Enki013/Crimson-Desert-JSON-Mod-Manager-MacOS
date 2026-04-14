"""
Crimson Desert — Mod Manager (macOS)
=====================================
Merges ALL enabled modpatch JSON files into a single overlay (0036/).

CLI (subcommands):
    python3 mod_manager.py wizard
    python3 mod_manager.py set-game "/Applications/Crimson Desert.app"
    python3 mod_manager.py install ./mods/foo --apply   # copy + patch game in one go
    python3 mod_manager.py install ~/Downloads/SomeModFolder
    python3 mod_manager.py status             # enabled mods, game path, health checks
    python3 mod_manager.py scan ~/Downloads/SomeModFolder [--game PATH]
    python3 mod_manager.py list
    python3 mod_manager.py apply [--game PATH]   # needs mods in mods/enabled/
    python3 mod_manager.py restore [--game PATH]   # vanilla overlay only (does not touch mods/enabled/)
    python3 mod_manager.py disable <name-or-index>  # only mods/enabled/ (re-apply yourself)
    python3 mod_manager.py remove <name-or-index>     # disable + apply in one step
    python3 mod_manager.py reset [-y]               # vanilla + clear mods/enabled/

Legacy flags (--list / --apply / --restore or --uninstall; unknown --flags exit with an error):
    python3 mod_manager.py --list
    python3 mod_manager.py [--game PATH] --apply | --restore
    (--uninstall is a legacy alias for --restore.)

Mod JSON format:
{
  "name": "...",
  "patches": [{
    "game_file": "gamedata/skill.pabgb",
    "changes": [{"offset": 123, "original": "aabb", "patched": "ccdd"}]
  }]
}
"""
import argparse
import os, sys, struct, json, glob, shutil, re
from collections import defaultdict
from pathlib import Path
from typing import List, Optional

import lz4.block

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pa_checksum import pa_checksum
from pamt_patcher import read_pamt_raw, resolve_filename, resolve_dirname

# ─── Configuration ──────────────────────────────────────────────────────

# GAME_DIR = "packages root": directory that contains meta/0.papgt and 0008/0.pamt
# On macOS this is usually .../Crimson Desert.app/Contents/Resources/packages

GAME_DIR = None
ENV_GAME = "CRIMSON_DESERT_GAME"
CONFIG_FILE_NAME = "_game_path.json"


def _is_valid_packages_dir(p: Path) -> bool:
    return (
        (p / "meta" / "0.papgt").is_file()
        and (p / "0008" / "0.pamt").is_file()
    )


def resolve_to_packages_dir(user_path: str) -> Optional[str]:
    """Resolve user-supplied path to the packages root (meta/ + 0008/)."""
    p = Path(user_path).expanduser().resolve()
    if not p.exists():
        return None
    if p.is_dir() and p.suffix == ".app":
        inner = p / "Contents" / "Resources" / "packages"
        if _is_valid_packages_dir(inner):
            return str(inner)
        return None
    if _is_valid_packages_dir(p):
        return str(p)
    return None


def _config_path() -> Path:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return Path(base) / CONFIG_FILE_NAME


def load_saved_packages_dir() -> Optional[str]:
    cfg = _config_path()
    if not cfg.is_file():
        return None
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            data = json.load(f)
        path = data.get("packages_dir") or data.get("game_path")
        if not path:
            return None
        resolved = resolve_to_packages_dir(path)
        if resolved:
            return resolved
        # BUG-9: stale/invalid saved path should not silently linger forever.
        warn(f"Ignoring invalid saved game path in {CONFIG_FILE_NAME}: {path}")
        try:
            cfg.unlink()
            info(f"Removed invalid {CONFIG_FILE_NAME}; use set-game to save a new path.")
        except OSError:
            pass
        return None
    except (json.JSONDecodeError, OSError):
        return None


def save_packages_dir(packages_dir: str):
    """Persist path we successfully used (canonical packages root)."""
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump({"packages_dir": packages_dir}, f, indent=2)
    except OSError:
        pass


def detect_packages_dir() -> Optional[str]:
    home = os.path.expanduser("~")
    candidates = [
        "/Applications/Crimson Desert.app",
        os.path.join(home, "Applications/Crimson Desert.app"),
        os.path.join(
            home,
            "Library/Application Support/Steam/steamapps/common/Crimson Desert/Crimson Desert.app",
        ),
        os.path.join(
            home,
            "Library/Application Support/Steam/steamapps/common/Crimson Desert",
        ),
    ]
    for c in candidates:
        resolved = resolve_to_packages_dir(c)
        if resolved:
            return resolved
    return None


def init_game_dir(override: Optional[str] = None) -> bool:
    """
    Set global GAME_DIR. Order: CLI override, env, saved config, auto-detect.
    Returns False if no valid path (caller may print help).
    """
    global GAME_DIR
    if override:
        resolved = resolve_to_packages_dir(override)
        if not resolved:
            print(f"ERROR: Not a valid Crimson Desert install: {override}")
            print("  Expected: Crimson Desert.app, or a folder containing meta/0.papgt and 0008/0.pamt")
            return False
        GAME_DIR = resolved
        save_packages_dir(GAME_DIR)
        return True

    env = os.environ.get(ENV_GAME)
    if env:
        resolved = resolve_to_packages_dir(env)
        if resolved:
            GAME_DIR = resolved
            return True
        print(f"WARNING: {ENV_GAME} is set but invalid: {env}")

    saved = load_saved_packages_dir()
    if saved:
        GAME_DIR = saved
        return True

    found = detect_packages_dir()
    if found:
        GAME_DIR = found
        save_packages_dir(GAME_DIR)
        return True

    GAME_DIR = None
    return False


def game_dir_help():
    print("Crimson Desert install not found.")
    print("  macOS example:")
    print("    python3 mod_manager.py set-game '/Applications/Crimson Desert.app'")
    print("    python3 mod_manager.py apply")
    print("  Or set environment variable:")
    print(f"    export {ENV_GAME}='/Applications/Crimson Desert.app'")
    print(f"  Or create {CONFIG_FILE_NAME} in the script folder with:")
    print('    {"packages_dir": "/path/to/.../packages"}')


# ─── CLI styling (colored log prefixes) ────────────────────────────────

class Style:
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def info(msg: str):
    print(f"{Style.CYAN}[INFO]{Style.RESET} {msg}")


def success(msg: str):
    print(f"{Style.GREEN}[OK]{Style.RESET}   {msg}")


def warn(msg: str):
    print(f"{Style.YELLOW}[WARN]{Style.RESET} {msg}")


def error(msg: str):
    print(f"{Style.RED}[ERR]{Style.RESET}  {msg}")


def fatal(msg: str):
    error(msg)
    sys.exit(1)


def _clean_user_path(raw: str) -> Path:
    s = raw.strip().strip('"').strip("'")
    return Path(s).expanduser().resolve()


def _iter_mod_paths_in_folder(root: Path, recursive: bool) -> List[Path]:
    out = []
    globs = (root.rglob if recursive else root.glob)
    for pattern in ("*.json", "*.modpatch"):
        for p in globs(pattern):
            if not p.is_file():
                continue
            if p.name.startswith("_"):
                continue
            out.append(p.resolve())
    return sorted(set(out))


def _load_mod_from_path(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            mod = json.load(f)
        mod["_path"] = str(path)
        return mod
    except (json.JSONDecodeError, OSError):
        return None


def _enabled_mod_files_ordered() -> List[Path]:
    """Same order as load_modpatches(): all .json then all .modpatch, each group sorted."""
    if not os.path.isdir(MODS_DIR):
        return []
    jsons = sorted(Path(MODS_DIR).glob("*.json"))
    mods = sorted(Path(MODS_DIR).glob("*.modpatch"))
    return list(jsons) + list(mods)


def _report_patch_overlaps(mods: List[dict]):
    """Warn when two mods patch the same byte range of the same game_file."""
    by_file = defaultdict(list)  # game_file -> [(path, start, end, label)]
    for mod in mods:
        mpath = mod.get("_path", "?")
        for patch in mod.get("patches", []):
            gf = patch.get("game_file")
            if not gf:
                continue
            for ch in patch.get("changes", []):
                off = ch.get("offset")
                if off is None:
                    continue
                orig = ch.get("original", "")
                try:
                    nbytes = len(bytes.fromhex(orig)) if orig else 0
                except ValueError:
                    nbytes = 0
                label = ch.get("label", "")
                by_file[gf].append((mpath, off, off + nbytes, label))

    for gf, entries in sorted(by_file.items()):
        entries.sort(key=lambda x: (x[1], x[2]))
        for i in range(len(entries) - 1):
            p1, s1, e1, _ = entries[i]
            p2, s2, e2, _ = entries[i + 1]
            if e1 > s2 and p1 != p2:
                warn(
                    f"Overlapping patches in {gf!r}: {Path(p1).name} @ {s1} vs {Path(p2).name} @ {s2}"
                )
            elif s1 == s2 and p1 != p2:
                warn(f"Same offset {s1} in {gf!r}: {Path(p1).name} and {Path(p2).name}")


def cmd_set_game_cli(game_path: str) -> bool:
    if not init_game_dir(game_path):
        return False
    success(f"Game path set (packages root): {GAME_DIR}")
    return True


def _interactive_pick_mod_path(paths: List[Path]) -> Optional[Path]:
    """Return the only path, or prompt for one of several. None = cancel / invalid."""
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    print()
    info(f"{len(paths)} mod files found — choose one to install:")
    for i, p in enumerate(paths, 1):
        print(f"  {i}) {p.name}")
    try:
        raw = input(f"Number [1–{len(paths)}] or 0 to cancel: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        info("Cancelled.")
        return None
    try:
        k = int(raw)
    except ValueError:
        error("Invalid number.")
        return None
    if k == 0:
        info("Cancelled.")
        return None
    if 1 <= k <= len(paths):
        return paths[k - 1]
    error("Out of range.")
    return None


def cmd_install_from_folder(
    folder: str,
    recursive: bool = True,
    *,
    force: bool = False,
    apply_after: bool = False,
    game_opt: Optional[str] = None,
    max_files: Optional[int] = None,
    pick: bool = False,
) -> bool:
    """Copy mod JSON into mods/available/ and mods/enabled/. Optionally run apply."""
    root = _clean_user_path(folder)
    if not root.is_dir():
        error(f"Folder not found: {root}")
        return False
    if pick and max_files is not None:
        error("Use either --pick or --max-files, not both.")
        return False
    if max_files is not None and max_files < 1:
        error("--max-files must be >= 1")
        return False
    paths = sorted(
        _iter_mod_paths_in_folder(root, recursive),
        key=lambda p: p.name.lower(),
    )
    if not paths:
        warn("No .json or .modpatch files found.")
        return False
    if pick:
        if len(paths) > 1 and not sys.stdin.isatty():
            error("install --pick needs an interactive terminal. For scripts use --max-files N.")
            return False
        chosen = _interactive_pick_mod_path(paths)
        if chosen is None:
            return False
        paths = [chosen]
    if max_files is not None and len(paths) > max_files:
        warn(
            f"{len(paths)} mod file(s) in folder — installing only the first {max_files} "
            f"(sorted by name; use --max-files to change)."
        )
        paths = paths[:max_files]
    os.makedirs(MODS_DIR, exist_ok=True)
    os.makedirs(MODS_AVAILABLE_DIR, exist_ok=True)
    n = 0
    for src in paths:
        dest = Path(MODS_DIR) / src.name
        avail_dest = Path(MODS_AVAILABLE_DIR) / src.name
        existed = dest.exists()
        if existed and not force:
            warn(f"Already exists (skipped): {dest.name}  (use install --force to overwrite)")
            continue
        # Always keep a copy in available/ as archive
        shutil.copy2(src, avail_dest)
        # Copy to enabled/ (active)
        shutil.copy2(src, dest)
        if existed:
            success(f"Overwrote → mods/enabled/{dest.name}")
        else:
            success(f"Installed → mods/enabled/{dest.name}")
        n += 1
    if n == 0:
        warn("No files copied.")
        if apply_after:
            warn("Skipping apply (nothing was installed).")
        return False

    info(f"Copied {n} mod file(s) (also archived in mods/available/).")
    if n > 1:
        warn(
            "Multiple mod files installed — if they patch the same game_file, keep only one in mods/enabled/ at a time."
        )
    if not apply_after:
        warn("Game not patched yet — run:  python3 mod_manager.py apply  (or use  install --apply )")

    if apply_after:
        if not init_game_dir(game_opt):
            game_dir_help()
            error("Cannot apply without a valid game path. Use: set-game … or install --game … --apply")
            return False
        print()
        info(f"Game packages: {GAME_DIR}")
        print()
        cmd_apply()
    return True


def _clear_enabled_mod_files():
    """Delete all .json / .modpatch in mods/enabled/."""
    n = 0
    for f in _enabled_mod_files_ordered():
        f.unlink()
        n += 1
    return n


def cmd_disable_enabled_mod(identifier: str, *, hint_apply: bool = True) -> bool:
    files = _enabled_mod_files_ordered()
    if not files:
        warn("mods/enabled/ is empty.")
        return False
    target: Optional[Path] = None
    try:
        idx = int(identifier)
        if 1 <= idx <= len(files):
            target = files[idx - 1]
    except ValueError:
        pass
    if target is None:
        id_lower = identifier.lower()
        for p in files:
            if p.stem.lower() == id_lower or p.name.lower() == id_lower:
                target = p
                break
    if target is None:
        error(f"No enabled mod matches {identifier!r}. Use `list` to see indices.")
        return False
    # Move to disabled instead of deleting
    os.makedirs(MODS_DISABLED_DIR, exist_ok=True)
    dest = Path(MODS_DISABLED_DIR) / target.name
    if dest.exists():
        # If already in disabled, overwrite
        dest.unlink()
    shutil.move(str(target), str(dest))
    success(f"Disabled: {target.name}  (enabled → disabled)")
    if hint_apply:
        info(
            "Update the game:  apply  if mods remain in mods/enabled/;  restore  if it is now empty. "
            "Or use  remove  for one-step updates. Re-enable with:  enable  "
        )
    return True


def cmd_remove_mod(identifier: str, game_opt: Optional[str] = None) -> bool:
    """Remove one mod from enabled and sync the game (apply or restore if none left)."""
    if not cmd_disable_enabled_mod(identifier, hint_apply=False):
        return False
    if not init_game_dir(game_opt):
        warn("Game path not set — mod file removed; run  set-game  then  apply  or  restore  when ready.")
        return True
    print()
    info(f"Game packages: {GAME_DIR}")
    print()
    if load_modpatches(MODS_DIR):
        cmd_apply()
    else:
        cmd_uninstall()
    return True


def cmd_reset(game_opt: Optional[str] = None, assume_yes: bool = False) -> bool:
    """Restore vanilla game and clear mods/enabled/."""
    if assume_yes:
        pass
    elif sys.stdin.isatty():
        ans = input("Reset game to vanilla and delete all mods in mods/enabled/? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            info("Cancelled.")
            return False
    else:
        error(
            "stdin is not a terminal — refusing reset without explicit confirmation. "
            "Use:  reset -y   (vanilla game + clear mods/enabled/)"
        )
        return False
    if not init_game_dir(game_opt):
        game_dir_help()
        return False
    cmd_uninstall()
    n = _clear_enabled_mod_files()
    if n:
        success(f"Cleared {n} file(s) from mods/enabled/")
    else:
        info("mods/enabled/ was already empty.")
    print("\nDone — full reset. Restart the game.")
    return True


def cmd_scan_mod_folder(folder: str, game_opt: Optional[str], recursive: bool = True) -> bool:
    root = _clean_user_path(folder)
    if not root.is_dir():
        error(f"Folder not found: {root}")
        return False
    paths = _iter_mod_paths_in_folder(root, recursive)
    if not paths:
        warn("No .json or .modpatch files in folder.")
        return False

    mods = []
    for p in paths:
        m = _load_mod_from_path(p)
        if not m:
            warn(f"Invalid JSON: {p.name}")
            continue
        if not isinstance(m.get("patches"), list):
            warn(f"Skip (not a modpatch, no 'patches' list): {p.name}")
            continue
        mods.append(m)

    if not mods:
        warn("No valid modpatch files in folder.")
        return False

    print()
    info(f"Found {len(mods)} modpatch file(s) under {root}")
    for mod in mods:
        name = mod.get("name", "?")
        n_patch = len(mod.get("patches", []))
        n_chg = sum(len(p.get("changes", [])) for p in mod.get("patches", []))
        print(f"  {Style.CYAN}{Path(mod['_path']).name}{Style.RESET}  [{name}]  {n_patch} patch(es), {n_chg} change(s)")

    print()
    info("Conflict check (within this folder):")
    _report_patch_overlaps(mods)

    pamt_ok = init_game_dir(game_opt) if game_opt else init_game_dir(None)
    if not pamt_ok:
        print()
        warn("Game not configured — skipping PAMT lookup. Use set-game or --game to validate paths.")
        return True

    pamt_path = os.path.join(GAME_DIR, SOURCE_GROUP, "0.pamt")
    try:
        pamt_info = read_pamt_raw(pamt_path)
    except OSError as e:
        warn(f"Could not read PAMT: {e}")
        return True

    file_index, full_index = build_file_index(pamt_info)
    print()
    info("Game file resolution (against current game data):")
    for mod in mods:
        for patch in mod.get("patches", []):
            gf = patch.get("game_file", "?")
            info_obj = resolve_game_file(gf, file_index, full_index)
            tag = f"{Style.GREEN}OK{Style.RESET}" if info_obj else f"{Style.RED}NOT FOUND{Style.RESET}"
            print(f"  [{tag}] {gf}  (from {Path(mod['_path']).name})")
    return True


def cmd_status(game_opt: Optional[str] = None):
    """Enabled mods, game path, overlay state, lz4/PAMT/writability checks."""
    print()
    info(f"Python {sys.version.split()[0]}")
    try:
        import lz4.block  # noqa: F401

        success("lz4: OK")
    except ImportError as e:
        error(f"lz4: missing — pip3 install -r requirements.txt  ({e})")
        return

    print()
    info(f"Tool directory: {SCRIPT_DIR}")
    info(f"Enabled mods: {MODS_DIR}")
    files = _enabled_mod_files_ordered()
    info(f"Enabled mod files: {len(files)}")
    for i, p in enumerate(files, 1):
        print(f"    {i}. {p.name}")

    if not init_game_dir(game_opt):
        print()
        warn("Game path not set — run `set-game` or `status --game \"…\"`")
        return

    print()
    info(f"Game packages: {GAME_DIR}")
    pamt = os.path.join(GAME_DIR, SOURCE_GROUP, "0.pamt")
    papgt = os.path.join(GAME_DIR, "meta", "0.papgt")
    if os.path.isfile(pamt) and os.path.isfile(papgt):
        success("meta + PAMT files present")
    else:
        error(f"Missing game files under packages/ (expected {SOURCE_GROUP}/0.pamt and meta/0.papgt)")
        return

    try:
        read_pamt_raw(pamt)
        success("PAMT: parses OK")
    except Exception as e:
        error(f"PAMT: parse error — {e}")
        return

    meta_dir = os.path.join(GAME_DIR, "meta")
    if os.access(meta_dir, os.W_OK):
        success("meta/: writable")
    else:
        warn("meta/: not writable — apply may fail (fix .app ownership; see README)")
    if os.access(GAME_DIR, os.W_OK):
        success("packages/: writable")
    else:
        warn("packages/: may not be writable")

    bak = papgt + ".bak"
    overlay = os.path.join(GAME_DIR, MOD_DIR_NAME)
    print()
    print(f"    meta/0.papgt       {'OK' if os.path.isfile(papgt) else 'MISSING'}")
    print(f"    meta/0.papgt.bak   {'OK' if os.path.isfile(bak) else '— (no backup yet)'}")
    o = "yes" if os.path.isdir(overlay) else "no (vanilla)"
    print(f"    {MOD_DIR_NAME}/ overlay   {o}")

    if sys.platform == "darwin":
        print()
        info(f'Open in Finder:  open "{GAME_DIR}"')


def _wizard_resolve_game() -> bool:
    if GAME_DIR:
        return True
    raw = input("Crimson Desert path (.app or packages folder), or Enter to auto-detect: ").strip()
    if raw:
        return cmd_set_game_cli(raw)
    return init_game_dir(None)


def run_wizard():
    """Interactive menu — full mod lifecycle matching GUI v7 flow."""
    print()
    print(f"{Style.BOLD}Crimson Desert — JSON Mod Manager{Style.RESET}")
    print(f"{Style.DIM}JSON modpatch → overlay 0036{Style.RESET}")
    print()

    if not init_game_dir(None):
        warn("Game not auto-detected.")
        gp = input("Enter game path now, or Enter to skip (you can use option 13 later): ").strip()
        if gp:
            cmd_set_game_cli(gp)
    if GAME_DIR:
        info(f"Game packages: {GAME_DIR}")

    def _wizard_summary():
        n_avail = len(_list_mod_files_in_dir(MODS_AVAILABLE_DIR))
        n_enabled = len(_enabled_mod_files_ordered())
        n_disabled = len(_list_mod_files_in_dir(MODS_DISABLED_DIR))
        print(f"{Style.DIM}  Mods: {n_avail} available, {n_enabled} enabled, {n_disabled} disabled{Style.RESET}")

    print()
    _wizard_summary()
    print()

    while True:
        print(f"{Style.BOLD}Choose an action:{Style.RESET}")
        print(f"  {Style.CYAN} 1){Style.RESET} 📦 Install mod(s) from folder")
        print(f"  {Style.CYAN} 2){Style.RESET} 📋 List available mods")
        print(f"  {Style.CYAN} 3){Style.RESET} ▶  Activate mod (available → enabled)")
        print(f"  {Style.CYAN} 4){Style.RESET} 📋 List enabled mods")
        print(f"  {Style.CYAN} 5){Style.RESET} ⏸  Disable mod (enabled → disabled)")
        print(f"  {Style.CYAN} 6){Style.RESET} 📋 List disabled mods")
        print(f"  {Style.CYAN} 7){Style.RESET} 🔄 Re-enable mod (disabled → enabled)")
        print(f"  {Style.CYAN} 8){Style.RESET} 🔧 Show patches for a mod")
        print(f"  {Style.CYAN} 9){Style.RESET} 🎚  Toggle patches (ON/OFF)")
        print(f"  {Style.CYAN}10){Style.RESET} ✅ Apply mods to game")
        print(f"  {Style.CYAN}11){Style.RESET} ↩  Restore game (vanilla)")
        print(f"  {Style.CYAN}12){Style.RESET} 🗑  Remove mod permanently")
        print(f"  {Style.CYAN}13){Style.RESET} ⚙  Change game path")
        print(f"  {Style.CYAN}14){Style.RESET} 📊 Status")
        print(f"  {Style.CYAN}15){Style.RESET} 🎮 Start game")
        print(f"  {Style.CYAN} 0){Style.RESET} Exit")
        choice = input("> ").strip()
        print()

        try:
            if choice == "1":
                raw = input("Mod folder path: ").strip()
                if not raw:
                    continue
                root = str(_clean_user_path(raw))
                cmd_install_from_folder(root, recursive=True, pick=True)
                print()
                _wizard_summary()
                print()
            elif choice == "2":
                cmd_available()
                print()
            elif choice == "3":
                cmd_available()
                ident = input("Index or file name to activate: ").strip()
                if ident:
                    cmd_activate_available(ident)
                print()
                _wizard_summary()
                print()
            elif choice == "4":
                cmd_list()
                print()
            elif choice == "5":
                cmd_list()
                ident = input("Index or file name to disable: ").strip()
                if ident:
                    cmd_disable_enabled_mod(ident)
                print()
                _wizard_summary()
                print()
            elif choice == "6":
                cmd_disabled_list()
                print()
            elif choice == "7":
                cmd_disabled_list()
                ident = input("Index or file name to re-enable: ").strip()
                if ident:
                    cmd_enable_mod(ident)
                print()
                _wizard_summary()
                print()
            elif choice == "8":
                cmd_list()
                ident = input("Mod index or name to show patches: ").strip()
                if ident:
                    cmd_patches(ident)
                print()
            elif choice == "9":
                cmd_list()
                ident = input("Mod index or name: ").strip()
                if not ident:
                    continue
                cmd_patches(ident)
                print("Toggle spec: index (e.g. 5), range (5-10), category ([Flight]),")
                print("             all, none, on 5, off [Sprint], etc.")
                spec = input("Toggle spec: ").strip()
                if spec:
                    cmd_toggle_patch(ident, spec)
                print()
            elif choice == "10":
                if not GAME_DIR and not _wizard_resolve_game():
                    warn("Set game path first (option 13).")
                    print()
                    continue
                cmd_apply()
                print()
            elif choice == "11":
                if not GAME_DIR and not _wizard_resolve_game():
                    warn("Set game path first (option 13).")
                    print()
                    continue
                cmd_uninstall()
                print()
            elif choice == "12":
                cmd_list()
                cmd_disabled_list()
                cmd_available()
                ident = input("Mod name to permanently delete: ").strip()
                if ident:
                    confirm = input(f"Permanently delete '{ident}'? [y/N]: ").strip().lower()
                    if confirm in ("y", "yes"):
                        cmd_purge_mod(ident)
                    else:
                        info("Cancelled.")
                print()
                _wizard_summary()
                print()
            elif choice == "13":
                raw = input("Path to Crimson Desert.app or packages folder: ").strip()
                if raw:
                    cmd_set_game_cli(raw)
                print()
            elif choice == "14":
                cmd_status()
                print()
            elif choice == "15":
                if not GAME_DIR and not _wizard_resolve_game():
                    warn("Set game path first (option 13).")
                    print()
                    continue
                cmd_start_game()
                print()
            elif choice == "0":
                info("Goodbye.")
                return
            else:
                warn("Invalid choice. Pick 0–15.")
                print()
        except KeyboardInterrupt:
            print()
            info("Interrupted.")
            return

SOURCE_GROUP = "0008"  # group containing original game data
MOD_DIR_NAME = "0036"  # overlay directory name
PAZ_ALIGNMENT = 16
PAMT_UNKNOWN = 0x610E0232
PAPGT_LANG_ALL = 0x3FFF

if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODS_DIR = os.path.join(SCRIPT_DIR, "mods", "enabled")
MODS_AVAILABLE_DIR = os.path.join(SCRIPT_DIR, "mods", "available")
MODS_DISABLED_DIR = os.path.join(SCRIPT_DIR, "mods", "disabled")
MOD_STATE_FILE = os.path.join(SCRIPT_DIR, "mods", "_mod_state.json")


# ─── Mod state (patch-level toggles) ───────────────────────────────────

def load_mod_state() -> dict:
    """Load patch toggle state from _mod_state.json."""
    if not os.path.isfile(MOD_STATE_FILE):
        return {}
    try:
        with open(MOD_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_mod_state(state: dict):
    """Persist patch toggle state."""
    try:
        os.makedirs(os.path.dirname(MOD_STATE_FILE), exist_ok=True)
        with open(MOD_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except OSError as e:
        warn(f"Could not save mod state: {e}")


def get_enabled_patches(mod: dict, state: dict) -> list:
    """Return only enabled patches for a mod, filtering by state.

    Each patch dict is returned as-is; disabled ones are omitted.
    Returns (enabled_list, disabled_count).
    """
    mod_key = Path(mod.get("_path", "")).name
    mod_state = state.get(mod_key, {})
    disabled_indices = set(mod_state.get("disabled_patches", []))
    if not disabled_indices:
        return mod.get("patches", []), 0

    enabled = []
    disabled_count = 0
    for i, patch in enumerate(mod.get("patches", [])):
        for j, change in enumerate(patch.get("changes", [])):
            # Global change index across all patches
            pass
        if i not in disabled_indices:
            enabled.append(patch)
        else:
            disabled_count += 1
    return enabled, disabled_count


def _flat_change_index(mod: dict) -> list:
    """Build flat list of (patch_idx, change_idx, change_dict, game_file) across all patches."""
    flat = []
    for pi, patch in enumerate(mod.get("patches", [])):
        gf = patch.get("game_file", "?")
        for ci, change in enumerate(patch.get("changes", [])):
            flat.append((pi, ci, change, gf))
    return flat


def _get_disabled_changes(state: dict, mod_key: str) -> set:
    """Return set of flat change indices that are disabled."""
    mod_state = state.get(mod_key, {})
    return set(mod_state.get("disabled_changes", []))


def _set_disabled_changes(state: dict, mod_key: str, disabled: set):
    """Set the disabled change indices for a mod."""
    if mod_key not in state:
        state[mod_key] = {}
    state[mod_key]["disabled_changes"] = sorted(disabled)
    if not disabled:
        # Clean up empty entries
        if "disabled_changes" in state[mod_key]:
            del state[mod_key]["disabled_changes"]
        if not state[mod_key]:
            del state[mod_key]


# ─── Category helpers ───────────────────────────────────────────────────

def extract_category(label: str) -> str:
    """Extract category from label like '[Flight] CrowWing...' → 'Flight'."""
    m = re.match(r'\[(\w+)\]', label or '')
    return m.group(1) if m else 'Other'


def _group_changes_by_category(flat_changes: list) -> dict:
    """Group flat change list by category. Returns {category: [(flat_idx, change, game_file)]}."""
    cats = defaultdict(list)
    for flat_idx, (pi, ci, change, gf) in enumerate(flat_changes):
        cat = extract_category(change.get("label", ""))
        cats[cat].append((flat_idx, change, gf))
    return dict(cats)


# ─── Mod lifecycle helpers ──────────────────────────────────────────────

def _list_mod_files_in_dir(directory: str) -> List[Path]:
    """List .json and .modpatch files in a directory (sorted)."""
    if not os.path.isdir(directory):
        return []
    jsons = sorted(Path(directory).glob("*.json"))
    mods = sorted(Path(directory).glob("*.modpatch"))
    return [p for p in (list(jsons) + list(mods)) if not p.name.startswith("_")]


def cmd_available():
    """List available (not yet enabled) mod files."""
    files = _list_mod_files_in_dir(MODS_AVAILABLE_DIR)
    if not files:
        print("No mods in mods/available/")
        print(f"  Place .json modpatch files in: {MODS_AVAILABLE_DIR}")
        print(f"  Or use: install <folder>")
        return
    enabled_names = {p.name.lower() for p in _enabled_mod_files_ordered()}
    print(f"Available mods ({len(files)}):")
    for i, p in enumerate(files, 1):
        mod = _load_mod_from_path(p)
        status = f"{Style.YELLOW}[enabled]{Style.RESET}" if p.name.lower() in enabled_names else ""
        if mod:
            name = mod.get("name", "?")
            desc = mod.get("description", "")[:60]
            n_patches = sum(len(pa.get("changes", [])) for pa in mod.get("patches", []))
            print(f"  {i}) {Style.CYAN}{p.name}{Style.RESET}  [{name}]  {n_patches} patch(es) {status}".rstrip())
            if desc:
                print(f"      {desc}")
        else:
            print(f"  {i}) {p.name}  (invalid JSON) {status}".rstrip())


def cmd_enable_mod(identifier: str) -> bool:
    """Move a mod from mods/disabled/ to mods/enabled/."""
    files = _list_mod_files_in_dir(MODS_DISABLED_DIR)
    if not files:
        warn("mods/disabled/ is empty.")
        return False

    target: Optional[Path] = None
    try:
        idx = int(identifier)
        if 1 <= idx <= len(files):
            target = files[idx - 1]
    except ValueError:
        pass
    if target is None:
        id_lower = identifier.lower()
        for p in files:
            if p.stem.lower() == id_lower or p.name.lower() == id_lower:
                target = p
                break
    if target is None:
        error(f"No disabled mod matches {identifier!r}. Use `disabled` to see list.")
        return False

    os.makedirs(MODS_DIR, exist_ok=True)
    dest = Path(MODS_DIR) / target.name
    if dest.exists():
        warn(f"Already exists in enabled: {dest.name}")
        return False
    shutil.move(str(target), str(dest))
    success(f"Enabled: {target.name}  (disabled → enabled)")
    info("Run `apply` to update the game.")
    return True


def cmd_activate_available(identifier: str) -> bool:
    """Move a mod from mods/available/ to mods/enabled/."""
    files = _list_mod_files_in_dir(MODS_AVAILABLE_DIR)
    if not files:
        warn("mods/available/ is empty.")
        return False

    target: Optional[Path] = None
    try:
        idx = int(identifier)
        if 1 <= idx <= len(files):
            target = files[idx - 1]
    except ValueError:
        pass
    if target is None:
        id_lower = identifier.lower()
        for p in files:
            if p.stem.lower() == id_lower or p.name.lower() == id_lower:
                target = p
                break
    if target is None:
        error(f"No available mod matches {identifier!r}. Use `available` to see list.")
        return False

    os.makedirs(MODS_DIR, exist_ok=True)
    dest = Path(MODS_DIR) / target.name
    if dest.exists():
        warn(f"Already exists in enabled: {dest.name}")
        return False
    shutil.move(str(target), str(dest))
    success(f"Activated: {target.name}  (available → enabled)")
    info("Run `apply` to update the game.")
    return True


def cmd_disabled_list():
    """List disabled mods."""
    files = _list_mod_files_in_dir(MODS_DISABLED_DIR)
    if not files:
        print("No mods in mods/disabled/")
        return
    print(f"Disabled mods ({len(files)}):")
    for i, p in enumerate(files, 1):
        mod = _load_mod_from_path(p)
        if mod:
            name = mod.get("name", "?")
            print(f"  {i}) {Style.DIM}{p.name}{Style.RESET}  [{name}]")
        else:
            print(f"  {i}) {p.name}  (invalid JSON)")


def cmd_purge_mod(identifier: str) -> bool:
    """Permanently delete a mod from any location (enabled, disabled, or available)."""
    # Search in all three directories
    for label, directory in [("enabled", MODS_DIR), ("disabled", MODS_DISABLED_DIR), ("available", MODS_AVAILABLE_DIR)]:
        files = _list_mod_files_in_dir(directory)
        for p in files:
            if p.stem.lower() == identifier.lower() or p.name.lower() == identifier.lower():
                p.unlink()
                # Also clean up state
                state = load_mod_state()
                if p.name in state:
                    del state[p.name]
                    save_mod_state(state)
                success(f"Purged: {p.name} (was in {label})")
                return True
    error(f"No mod matches {identifier!r} in any directory.")
    return False


# ─── Patch listing & toggling ──────────────────────────────────────────

def cmd_patches(identifier: str):
    """List all patches in a mod with ON/OFF status and categories."""
    # Find mod in enabled, disabled, or available
    mod_path = _find_mod_anywhere(identifier)
    if mod_path is None:
        error(f"No mod matches {identifier!r}.")
        return

    mod = _load_mod_from_path(mod_path)
    if not mod:
        error(f"Invalid JSON: {mod_path.name}")
        return

    flat = _flat_change_index(mod)
    state = load_mod_state()
    disabled = _get_disabled_changes(state, mod_path.name)
    cats = _group_changes_by_category(flat)

    total = len(flat)
    enabled_count = total - len(disabled)
    name = mod.get("name", "?")

    print(f"\nPatches for {Style.BOLD}{name}{Style.RESET} ({mod_path.name})")
    print(f"  Total: {total}, Enabled: {Style.GREEN}{enabled_count}{Style.RESET}, Disabled: {Style.RED}{len(disabled)}{Style.RESET}")
    print()

    for cat_name in sorted(cats.keys()):
        entries = cats[cat_name]
        cat_enabled = sum(1 for fi, _, _ in entries if fi not in disabled)
        cat_total = len(entries)
        print(f"  {Style.BOLD}[{cat_name}]{Style.RESET}  {cat_enabled}/{cat_total} enabled")
        for flat_idx, change, gf in entries:
            label = change.get("label", f"@{change.get('offset', '?')}")
            is_on = flat_idx not in disabled
            tag = f"{Style.GREEN}✅{Style.RESET}" if is_on else f"{Style.RED}❌{Style.RESET}"
            print(f"    {flat_idx + 1:3d}) {tag} {label}")
        print()


def cmd_toggle_patch(identifier: str, spec: str) -> bool:
    """Toggle patches ON/OFF for a mod.

    spec can be:
      - "all"           → enable all
      - "none"          → disable all
      - "5"             → toggle single patch (1-based)
      - "5-10"          → toggle range (1-based)
      - "[Flight]"      → toggle entire category
      - "on 5"          → force ON
      - "off 5"         → force OFF
      - "on [Flight]"   → force category ON
      - "off [Flight]"  → force category OFF
    """
    mod_path = _find_mod_anywhere(identifier)
    if mod_path is None:
        error(f"No mod matches {identifier!r}.")
        return False

    mod = _load_mod_from_path(mod_path)
    if not mod:
        error(f"Invalid JSON: {mod_path.name}")
        return False

    flat = _flat_change_index(mod)
    state = load_mod_state()
    disabled = _get_disabled_changes(state, mod_path.name)
    total = len(flat)

    spec = spec.strip()

    # Parse force direction
    force_on = None
    if spec.lower().startswith("on "):
        force_on = True
        spec = spec[3:].strip()
    elif spec.lower().startswith("off "):
        force_on = False
        spec = spec[4:].strip()

    if spec.lower() == "all":
        if force_on is None or force_on:
            disabled = set()
            success(f"All {total} patches enabled for {mod_path.name}")
        else:
            disabled = set(range(total))
            success(f"All {total} patches disabled for {mod_path.name}")
    elif spec.lower() == "none":
        disabled = set(range(total))
        success(f"All {total} patches disabled for {mod_path.name}")
    elif spec.startswith("["):
        # Category toggle
        cat_match = re.match(r'\[(\w+)\]', spec)
        if not cat_match:
            error(f"Invalid category: {spec}")
            return False
        cat_name = cat_match.group(1)
        if force_on is None:
            cats = _group_changes_by_category(flat)
            if cat_name not in cats:
                error(f"Category [{cat_name}] not found. Available: {', '.join(sorted(cats.keys()))}")
                return False
            indices = [fi for fi, _, _ in cats[cat_name]]
            # Toggle: if any are enabled, disable all; if all disabled, enable all
            currently_enabled = [i for i in indices if i not in disabled]
            desired_on = not bool(currently_enabled)
            return cmd_toggle_category(identifier, cat_name, on=desired_on)
        return cmd_toggle_category(identifier, cat_name, on=force_on)
    elif "-" in spec and not spec.startswith("-"):
        # Range: "5-10"
        try:
            parts = spec.split("-")
            start = int(parts[0]) - 1
            end = int(parts[1]) - 1
            if start < 0 or end >= total or start > end:
                error(f"Range out of bounds: {spec} (valid: 1-{total})")
                return False
            indices = list(range(start, end + 1))
            if force_on is True:
                disabled -= set(indices)
                success(f"Patches {start+1}-{end+1} enabled")
            elif force_on is False:
                disabled |= set(indices)
                success(f"Patches {start+1}-{end+1} disabled")
            else:
                currently_enabled = [i for i in indices if i not in disabled]
                if currently_enabled:
                    disabled |= set(indices)
                    success(f"Patches {start+1}-{end+1} disabled (toggle)")
                else:
                    disabled -= set(indices)
                    success(f"Patches {start+1}-{end+1} enabled (toggle)")
        except ValueError:
            error(f"Invalid range: {spec}")
            return False
    else:
        # Single index
        try:
            idx = int(spec) - 1
            if idx < 0 or idx >= total:
                error(f"Index out of range: {spec} (valid: 1-{total})")
                return False
            if force_on is True:
                disabled.discard(idx)
                label = flat[idx][2].get("label", f"@{flat[idx][2].get('offset', '?')}")
                success(f"Patch {idx+1} enabled: {label}")
            elif force_on is False:
                disabled.add(idx)
                label = flat[idx][2].get("label", f"@{flat[idx][2].get('offset', '?')}")
                success(f"Patch {idx+1} disabled: {label}")
            else:
                label = flat[idx][2].get("label", f"@{flat[idx][2].get('offset', '?')}")
                if idx in disabled:
                    disabled.discard(idx)
                    success(f"Patch {idx+1} enabled: {label}")
                else:
                    disabled.add(idx)
                    success(f"Patch {idx+1} disabled: {label}")
        except ValueError:
            error(f"Invalid patch spec: {spec}")
            return False

    _set_disabled_changes(state, mod_path.name, disabled)
    save_mod_state(state)
    return True


def _find_mod_anywhere(identifier: str) -> Optional[Path]:
    """Find a mod file by name or index across enabled, disabled, and available."""
    for directory in [MODS_DIR, MODS_DISABLED_DIR, MODS_AVAILABLE_DIR]:
        files = _list_mod_files_in_dir(directory)
        # Try index (only makes sense for enabled)
        try:
            idx = int(identifier)
            if directory == MODS_DIR and 1 <= idx <= len(files):
                return files[idx - 1]
        except ValueError:
            pass
        # Try name match
        id_lower = identifier.lower()
        for p in files:
            if p.stem.lower() == id_lower or p.name.lower() == id_lower:
                return p
    return None


def cmd_toggle_category(mod_identifier: str, category: str, on: bool) -> bool:
    """Enable/disable all patches in a category."""
    mod_path = _find_mod_anywhere(mod_identifier)
    if mod_path is None:
        error(f"No mod matches {mod_identifier!r}.")
        return False
    mod = _load_mod_from_path(mod_path)
    if not mod:
        error(f"Invalid JSON: {mod_path.name}")
        return False

    flat = _flat_change_index(mod)
    cats = _group_changes_by_category(flat)
    if category not in cats:
        error(f"Category [{category}] not found. Available: {', '.join(sorted(cats.keys()))}")
        return False

    state = load_mod_state()
    disabled = _get_disabled_changes(state, mod_path.name)
    indices = [fi for fi, _, _ in cats[category]]
    if on:
        disabled -= set(indices)
        success(f"[{category}] {len(indices)} patches enabled")
    else:
        disabled |= set(indices)
        success(f"[{category}] {len(indices)} patches disabled")
    _set_disabled_changes(state, mod_path.name, disabled)
    save_mod_state(state)
    return True


def cmd_start_game():
    """Start the game (macOS: open the .app)."""
    if sys.platform != "darwin":
        warn("start-game is only supported on macOS.")
        return
    if not GAME_DIR:
        warn("Game path not set. Use set-game first.")
        return
    # Try to find the .app bundle from packages dir
    packages = Path(GAME_DIR)
    # packages is typically .../Crimson Desert.app/Contents/Resources/packages
    app_path = packages.parent.parent.parent
    if app_path.suffix == ".app" and app_path.is_dir():
        info(f"Starting: {app_path.name}")
        os.system(f'open "{app_path}"')
    else:
        warn(f"Cannot find .app bundle from packages dir: {GAME_DIR}")
        info("Try: open /Applications/Crimson\\ Desert.app")


# ─── File resolution ────────────────────────────────────────────────────

def build_file_index(pamt_info):
    """Build index: simplified_path -> (full_vfs_path, dir_path, filename, file_record)"""
    fn_data = pamt_info['fn_data']
    dir_data = pamt_info['raw'][
        pamt_info['dir_block_offset'] + 4:
        pamt_info['dir_block_offset'] + 4 + pamt_info['dir_block_size']
    ]

    index = {}  # simplified -> info
    full_index = {}  # full_path -> info

    for he in pamt_info['hash_entries']:
        dir_path = resolve_dirname(dir_data, he['name_offset'])
        for i in range(he['file_start_index'], he['file_start_index'] + he['file_count']):
            fr = pamt_info['file_records'][i]
            fname = resolve_filename(fn_data, fr['name_offset'])
            full_path = f"{dir_path}/{fname}" if dir_path else fname

            info = {
                'full_path': full_path,
                'dir_path': dir_path,
                'filename': fname,
                'record': fr,
                'record_index': i,
            }

            full_index[full_path] = info

            # Build simplified path: strip intermediate dirs -> "gamedata/filename.ext"
            parts = dir_path.split('/') if dir_path else []
            if parts:
                simplified = f"{parts[0]}/{fname}"
            else:
                simplified = fname
            # Only add if not ambiguous (first match wins)
            if simplified not in index:
                index[simplified] = info

    return index, full_index


def resolve_game_file(simplified, file_index, full_index):
    """Resolve a modpatch game_file path to full VFS info."""
    # Try exact match on full path first
    if simplified in full_index:
        return full_index[simplified]
    # Try simplified index
    if simplified in file_index:
        return file_index[simplified]
    return None


# ─── Load modpatch files ────────────────────────────────────────────────

def load_modpatches(mods_dir):
    """Load all JSON modpatch files from the enabled mods directory."""
    mods = []
    skipped = []
    if not os.path.isdir(mods_dir):
        os.makedirs(mods_dir, exist_ok=True)
        return mods

    for path in sorted(glob.glob(os.path.join(mods_dir, "*.json"))):
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                mod = json.load(f)
            mod['_path'] = path
            mods.append(mod)
        except (json.JSONDecodeError, OSError) as e:
            skipped.append((path, str(e)))

    # Also support .modpatch (JSON format)
    for path in sorted(glob.glob(os.path.join(mods_dir, "*.modpatch"))):
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                mod = json.load(f)
            mod['_path'] = path
            mods.append(mod)
        except (json.JSONDecodeError, OSError) as e:
            skipped.append((path, str(e)))

    if skipped:
        warn(f"Skipped {len(skipped)} invalid mod file(s) in mods/enabled/:")
        for path, reason in skipped:
            print(f"  SKIP: {os.path.basename(path)} ({reason})")

    return mods


# ─── Multi-file PAMT builder ────────────────────────────────────────────

def build_multi_pamt(files, paz_data_len):
    """Build PAMT for multiple modded files in one overlay.

    Args:
        files: list of dicts with keys:
            dir_path, filename, comp_size, decomp_size, paz_offset
        paz_data_len: total aligned PAZ file size
    """
    # Step 1: Build DirBlock — collect all unique directory segments
    dir_block = bytearray()
    segment_offsets = {}  # partial_path -> offset in dir_block

    unique_dirs = sorted(set(f['dir_path'] for f in files))

    for dir_path in unique_dirs:
        parts = dir_path.split('/')
        for i, part in enumerate(parts):
            partial_path = '/'.join(parts[:i + 1])
            if partial_path in segment_offsets:
                continue

            offset = len(dir_block)
            segment_offsets[partial_path] = offset

            if i == 0:
                parent = 0xFFFFFFFF
                name = part
            else:
                parent_path = '/'.join(parts[:i])
                parent = segment_offsets[parent_path]
                name = '/' + part

            name_bytes = name.encode('utf-8')
            dir_block += struct.pack('<I', parent)
            dir_block += struct.pack('B', len(name_bytes)) + name_bytes

    # Step 2: Group files by directory, build FilenameBlock + records
    dir_files = defaultdict(list)
    for f in files:
        dir_files[f['dir_path']].append(f)

    fn_block = bytearray()
    hash_entries = []
    file_records = []
    file_index = 0

    for dir_path in sorted(dir_files.keys()):
        dir_hash = pa_checksum(dir_path.encode('utf-8'))
        dir_name_offset = segment_offsets[dir_path]

        file_start = file_index

        for f in dir_files[dir_path]:
            fn_off = len(fn_block)
            fn_block += struct.pack('<I', 0xFFFFFFFF)
            name_bytes = f['filename'].encode('utf-8')
            fn_block += struct.pack('B', len(name_bytes)) + name_bytes

            file_records.append(struct.pack('<IIIIHH',
                fn_off,
                f['paz_offset'],
                f['comp_size'],
                f['decomp_size'],
                0,       # paz_index (always 0)
                0x0002,  # flags: LZ4 compressed
            ))
            file_index += 1

        hash_entries.append(struct.pack('<IIII',
            dir_hash,
            dir_name_offset,
            file_start,
            len(dir_files[dir_path]),
        ))

    # Step 3: Assemble PAMT
    paz_info = struct.pack('<III', 0, 0, paz_data_len)

    body = bytearray()
    body += struct.pack('<II', 1, PAMT_UNKNOWN)
    body += paz_info
    body += struct.pack('<I', len(dir_block)) + dir_block
    body += struct.pack('<I', len(fn_block)) + fn_block
    body += struct.pack('<I', len(hash_entries))
    for he in hash_entries:
        body += he
    body += struct.pack('<I', len(file_records))
    for fr in file_records:
        body += fr

    header_crc = pa_checksum(bytes(body[8:]))
    return struct.pack('<I', header_crc) + bytes(body)


def update_pamt_paz_crc(pamt_data, paz_crc):
    """Update PAZ CRC in the first PazInfo entry and recalculate header CRC."""
    data = bytearray(pamt_data)
    struct.pack_into('<I', data, 16, paz_crc)
    new_crc = pa_checksum(bytes(data[12:]))
    struct.pack_into('<I', data, 0, new_crc)
    return bytes(data)


# ─── PAPGT builder ──────────────────────────────────────────────────────

def build_papgt_with_mod(papgt_path, mod_dir_name, pamt_crc):
    """Build PAPGT with mod overlay registered at position [0]."""
    with open(papgt_path, 'rb') as f:
        orig = f.read()

    gc = orig[8]
    sbo = 12 + gc * 12
    str_data = orig[sbo + 4:]

    entries = []
    names = []
    for i in range(gc):
        off = 12 + i * 12
        e = {
            'is_optional': orig[off],
            'lang_type': struct.unpack_from('<H', orig, off + 1)[0],
            'zero': orig[off + 3],
            'name_offset': struct.unpack_from('<I', orig, off + 4)[0],
            'pamt_crc': struct.unpack_from('<I', orig, off + 8)[0],
        }
        entries.append(e)
        noff = e['name_offset']
        end = str_data.find(b'\x00', noff)
        names.append(str_data[noff:end].decode('ascii'))

    pairs = list(zip(entries, names))

    mod_idx = next((i for i, n in enumerate(names) if n == mod_dir_name), None)
    if mod_idx is not None:
        pairs[mod_idx][0]['pamt_crc'] = pamt_crc
    else:
        new_entry = {'is_optional': 0, 'lang_type': PAPGT_LANG_ALL, 'zero': 0,
                     'name_offset': 0, 'pamt_crc': pamt_crc}
        pairs.insert(0, (new_entry, mod_dir_name))

    new_gc = len(pairs)
    str_block = bytearray()
    name_offsets = []
    for _, name in pairs:
        name_offsets.append(len(str_block))
        str_block += name.encode('ascii') + b'\x00'

    entry_block = bytearray()
    for i, (e, _) in enumerate(pairs):
        entry_block += struct.pack('B', e['is_optional'])
        entry_block += struct.pack('<H', e['lang_type'])
        entry_block += struct.pack('B', e['zero'])
        entry_block += struct.pack('<I', name_offsets[i])
        entry_block += struct.pack('<I', e['pamt_crc'])

    payload = entry_block + struct.pack('<I', len(str_block)) + str_block
    file_crc = pa_checksum(bytes(payload))
    header = struct.pack('<I', struct.unpack_from('<I', orig, 0)[0])
    header += struct.pack('<I', file_crc)
    header += struct.pack('B', new_gc)
    header += struct.pack('<H', struct.unpack_from('<H', orig, 9)[0])
    header += struct.pack('B', orig[11])
    return header + payload


# ─── Main logic ─────────────────────────────────────────────────────────

def cmd_list():
    """List enabled mods."""
    mods = load_modpatches(MODS_DIR)
    if not mods:
        print("No mods in mods/enabled/")
        print(f"  Place .json modpatch files in: {MODS_DIR}")
        return

    print(f"Enabled mods ({len(mods)}):")
    for i, mod in enumerate(mods, 1):
        name = mod.get('name', '?')
        desc = mod.get('description', '')[:60]
        files = set()
        for p in mod.get('patches', []):
            files.add(p['game_file'])
        src = Path(mod.get("_path", "")).name
        print(f"  {i}) [{name}]  ({src})")
        if desc:
            print(f"      {desc}")
        print(f"      Files: {', '.join(sorted(files))}")


def cmd_uninstall():
    """Restore original game files."""
    if not GAME_DIR:
        game_dir_help()
        return
    papgt_path = os.path.join(GAME_DIR, 'meta', '0.papgt')
    papgt_bak = papgt_path + '.bak'
    mod_dir = os.path.join(GAME_DIR, MOD_DIR_NAME)

    if os.path.exists(papgt_bak):
        shutil.copy2(papgt_bak, papgt_path)
        print("  Restored: meta/0.papgt from backup")
    else:
        print("  WARNING: No backup found!")

    if os.path.isdir(mod_dir):
        shutil.rmtree(mod_dir)
        print(f"  Removed: {MOD_DIR_NAME}/")

    print("\nDone — original game restored. Restart the game.")


def cmd_apply():
    """Apply all enabled mods (respecting patch-level toggles)."""
    if not GAME_DIR:
        game_dir_help()
        return
    papgt_path = os.path.join(GAME_DIR, 'meta', '0.papgt')
    papgt_bak = papgt_path + '.bak'
    pamt_path = os.path.join(GAME_DIR, SOURCE_GROUP, '0.pamt')
    mod_dir = os.path.join(GAME_DIR, MOD_DIR_NAME)

    # ── BUG-3 Fix: Clean old overlay before building ──
    if os.path.isdir(mod_dir):
        shutil.rmtree(mod_dir)

    # ── Step 1: Load all modpatches ──
    mods = load_modpatches(MODS_DIR)
    if not mods:
        warn("No mods in mods/enabled/ — nothing to merge. apply only builds the overlay from enabled mods.")
        info("To remove the overlay and return to vanilla, run:  restore")
        return

    print(f"Loaded {len(mods)} mod(s):")
    for m in mods:
        print(f"  - {m.get('name', '?')}")

    # ── Step 2: Group all changes by game_file (with patch-level filtering) ──
    # merged[game_file] = list of (mod_name, change)
    state = load_mod_state()
    merged = defaultdict(list)
    total_disabled = 0
    for mod in mods:
        mod_name = mod.get('name', '?')
        mod_key = Path(mod.get('_path', '')).name
        disabled_changes = _get_disabled_changes(state, mod_key)
        flat_idx = 0
        for patch in mod.get('patches', []):
            gf = patch['game_file']
            for change in patch['changes']:
                if flat_idx not in disabled_changes:
                    merged[gf].append((mod_name, change))
                else:
                    total_disabled += 1
                flat_idx += 1

    if total_disabled:
        info(f"Filtered out {total_disabled} disabled patch(es) via toggle state.")

    print(f"\nTarget files ({len(merged)}):")
    for gf, changes in sorted(merged.items()):
        mods_involved = sorted(set(m for m, _ in changes))
        print(f"  {gf}: {len(changes)} patches from [{', '.join(mods_involved)}]")

    # ── Step 3: Build file index from 0008 PAMT ──
    print(f"\nReading {SOURCE_GROUP}/0.pamt...")
    pamt_info = read_pamt_raw(pamt_path)
    file_index, full_index = build_file_index(pamt_info)

    # ── Step 4: For each game file, load original → patch → compress ──
    paz_buf = bytearray()
    overlay_files = []  # for PAMT builder

    for game_file, changes in sorted(merged.items()):
        info = resolve_game_file(game_file, file_index, full_index)
        if info is None:
            print(f"\n  ERROR: Cannot find '{game_file}' in {SOURCE_GROUP}/0.pamt!")
            print(f"  Skipping...")
            continue

        fr = info['record']
        full_path = info['full_path']
        dir_path = info['dir_path']
        filename = info['filename']

        print(f"\n  Processing: {full_path}")
        print(f"    Source: {SOURCE_GROUP}/{fr['paz_index']}.paz @ 0x{fr['paz_offset']:08X}")
        print(f"    Size: {fr['comp_size']} compressed, {fr['decomp_size']} decompressed")

        # Read original compressed data
        src_paz = os.path.join(GAME_DIR, SOURCE_GROUP, f"{fr['paz_index']}.paz")
        with open(src_paz, 'rb') as f:
            f.seek(fr['paz_offset'])
            comp_data = f.read(fr['comp_size'])

        # Decompress
        buf = bytearray(lz4.block.decompress(comp_data, uncompressed_size=fr['decomp_size']))

        # Apply all patches for this file
        applied = 0
        skipped = 0
        for mod_name, change in changes:
            offset = change['offset']
            orig_bytes = bytes.fromhex(change['original'])
            patch_bytes = bytes.fromhex(change['patched'])
            label = change.get('label', f'@{offset}')

            # BUG-5 Fix: Validate original/patched size match
            if len(orig_bytes) != len(patch_bytes):
                print(f"    SIZE MISMATCH [{mod_name}] {label}: original {len(orig_bytes)}B vs patched {len(patch_bytes)}B — skipping")
                skipped += 1
                continue

            current = bytes(buf[offset:offset + len(orig_bytes)])
            if current == orig_bytes:
                buf[offset:offset + len(patch_bytes)] = patch_bytes
                applied += 1
            else:
                print(f"    SKIP [{mod_name}] {label}: expected {orig_bytes.hex()}, got {current.hex()}")
                skipped += 1

        print(f"    Applied: {applied}, Skipped: {skipped}")

        # Recompress
        new_comp = lz4.block.compress(bytes(buf), store_size=False)
        print(f"    Recompressed: {len(buf)} -> {len(new_comp)} bytes")

        # Add to PAZ buffer
        paz_offset = len(paz_buf)
        paz_buf += new_comp

        # Align
        remainder = len(paz_buf) % PAZ_ALIGNMENT
        if remainder:
            paz_buf += b'\x00' * (PAZ_ALIGNMENT - remainder)

        overlay_files.append({
            'dir_path': dir_path,
            'filename': filename,
            'comp_size': len(new_comp),
            'decomp_size': fr['decomp_size'],
            'paz_offset': paz_offset,
        })

    if not overlay_files:
        print("\nNo files to patch!")
        return

    # ── Step 5: Write PAZ ──
    os.makedirs(mod_dir, exist_ok=True)
    paz_path = os.path.join(mod_dir, '0.paz')
    with open(paz_path, 'wb') as f:
        f.write(paz_buf)
    paz_crc = pa_checksum(bytes(paz_buf))
    print(f"\n  Wrote: {MOD_DIR_NAME}/0.paz ({len(paz_buf)} bytes, CRC=0x{paz_crc:08X})")

    # ── Step 6: Build and write PAMT ──
    pamt_data = build_multi_pamt(overlay_files, len(paz_buf))
    pamt_data = update_pamt_paz_crc(pamt_data, paz_crc)
    pamt_crc = struct.unpack_from('<I', pamt_data, 0)[0]

    pamt_out = os.path.join(mod_dir, '0.pamt')
    with open(pamt_out, 'wb') as f:
        f.write(pamt_data)
    print(f"  Wrote: {MOD_DIR_NAME}/0.pamt ({len(pamt_data)} bytes, CRC=0x{pamt_crc:08X})")

    # ── Step 7: Update PAPGT ──
    if not os.path.exists(papgt_bak):
        shutil.copy2(papgt_path, papgt_bak)
        print(f"  Backed up: meta/0.papgt -> 0.papgt.bak")
    else:
        shutil.copy2(papgt_bak, papgt_path)

    new_papgt = build_papgt_with_mod(papgt_path, MOD_DIR_NAME, pamt_crc)
    with open(papgt_path, 'wb') as f:
        f.write(new_papgt)
    print(f"  Wrote: meta/0.papgt ({len(new_papgt)} bytes)")

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"  {len(mods)} mod(s) merged into {MOD_DIR_NAME}/")
    if total_disabled:
        print(f"  {total_disabled} patch(es) skipped (disabled via toggle)")
    print(f"  {len(overlay_files)} game file(s) patched:")
    for of in overlay_files:
        print(f"    - {of['dir_path']}/{of['filename']}")
    print(f"  Restart the game to apply.")
    print(f"{'='*60}")


# ─── CLI ────────────────────────────────────────────────────────────────

def _parse_legacy_argv(argv):
    """Strip --game <path> / --game=path; return (remaining, game_override or None)."""
    game_override: Optional[str] = None
    out = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--game" and i + 1 < len(argv):
            game_override = argv[i + 1]
            i += 2
            continue
        if a.startswith("--game="):
            game_override = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    return out, game_override


def main_legacy(argv):
    """Backward compatibility: --list / --apply / --restore / --uninstall / --game."""
    args, game_override = _parse_legacy_argv(argv)
    if "--help" in args or "-h" in args:
        print(__doc__)
        print("Legacy: --list  --apply  --restore | --uninstall  --game PATH")
        print("Commands:  wizard | set-game | install [--apply] | scan | list | status | apply | restore | disable | remove | reset")
        return
    if "--list" in args:
        cmd_list()
        return
    if len(args) == 0:
        if game_override:
            if init_game_dir(game_override):
                success("Game path saved.")
            else:
                sys.exit(1)
        else:
            info("No action given. Try: wizard  |  install … --apply  |  --list")
        return

    legacy_restore = {"--restore", "--uninstall"}
    legacy_actions = {"--apply", *legacy_restore}
    unknown = [a for a in args if a not in legacy_actions]
    if unknown:
        error(f"Unknown legacy flag(s): {unknown}")
        info("Allowed with leading -- :  --list  --apply  --restore  --uninstall  (and optional --game PATH)")
        info("Subcommands do not use a leading -- :  e.g.  apply   restore   wizard")
        sys.exit(2)

    if "--apply" in args and legacy_restore.intersection(args):
        error("Use only one of: --apply   --restore | --uninstall")
        sys.exit(2)

    if "--uninstall" in args and "--restore" in args:
        error("Use only one of: --restore   --uninstall  (same action)")
        sys.exit(2)

    if not init_game_dir(game_override):
        game_dir_help()
        sys.exit(1)
    if legacy_restore.intersection(args):
        cmd_uninstall()
    else:
        cmd_apply()


def _build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="mod_manager.py",
        description="Crimson Desert JSON modpatch manager — merges enabled mods into overlay 0036.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s wizard
  %(prog)s set-game "/Applications/Crimson Desert.app"
  %(prog)s install ./mods/stamina_json_v1.02.00 --apply
  %(prog)s install ~/Mods --apply --force
  %(prog)s scan ./SomeModFolder
  %(prog)s status
  %(prog)s list
  %(prog)s apply
  %(prog)s restore
  %(prog)s install ./mods/stamina_pack --pick --apply
  %(prog)s disable 2
  %(prog)s remove 2
  %(prog)s reset
  %(prog)s reset -y

legacy (still supported):
  %(prog)s --list
  %(prog)s --apply
  %(prog)s --restore
  %(prog)s --uninstall
        """,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    game_help = "Crimson Desert.app or packages folder (optional if saved in _game_path.json / auto-detect)"

    p_sg = sub.add_parser("set-game", help="Save game path to _game_path.json")
    p_sg.add_argument("game_path", help="Path to .app or folder with meta/0.papgt")

    p_in = sub.add_parser(
        "install",
        help="Copy mod JSON into mods/enabled/ (use --apply to patch the game in one step)",
    )
    p_in.add_argument("mod_folder", help="Folder to scan")
    p_in.add_argument(
        "--apply",
        "-a",
        action="store_true",
        help="After copying, run apply (install + patch the game in one invocation)",
    )
    p_in.add_argument("--game", default=None, metavar="PATH", help=game_help)
    p_in.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite files that already exist in mods/enabled/",
    )
    p_in.add_argument(
        "--no-recursive",
        action="store_true",
        help="Only files directly in the folder (not subfolders)",
    )
    pick_mx = p_in.add_mutually_exclusive_group()
    pick_mx.add_argument(
        "--pick",
        action="store_true",
        help="If several mod files are in the folder, show a menu and install only the one you choose",
    )
    pick_mx.add_argument(
        "--max-files",
        type=int,
        default=None,
        metavar="N",
        help="Non-interactive: copy at most N mod files (sorted by name). For scripts/CI when a folder has variants.",
    )

    p_scan = sub.add_parser(
        "scan",
        help="Preview mods in a folder; optional PAMT check against the game",
    )
    p_scan.add_argument("mod_folder", help="Folder to scan")
    p_scan.add_argument("--game", default=None, metavar="PATH", help=game_help)
    p_scan.add_argument("--no-recursive", action="store_true")

    sub.add_parser("list", help="List mods in mods/enabled/")

    p_apply = sub.add_parser(
        "apply",
        help="Build overlay from mods/enabled/ and patch meta/0.papgt (no-op if enabled/ is empty — use restore)",
    )
    p_apply.add_argument("--game", default=None, metavar="PATH", help=game_help)

    p_rs = sub.add_parser(
        "restore",
        help="Remove 0036 overlay and restore meta/0.papgt (does not change mods/enabled/ — use reset to clear both)",
    )
    p_rs.add_argument("--game", default=None, metavar="PATH", help=game_help)

    p_dis = sub.add_parser(
        "disable",
        help="Move one mod from mods/enabled/ to mods/disabled/ (non-destructive)",
    )
    p_dis.add_argument("mod", help="Index from list (1,2,…) or file name / stem")

    p_rm = sub.add_parser(
        "remove",
        help="Disable one mod and sync the game (apply, or restore if that was the last mod)",
    )
    p_rm.add_argument("mod", help="Index from list (1,2,…) or file name / stem")
    p_rm.add_argument("--game", default=None, metavar="PATH", help=game_help)

    p_rst = sub.add_parser(
        "reset",
        help="Restore vanilla game (restore) and clear all mods in mods/enabled/",
    )
    p_rst.add_argument("--game", default=None, metavar="PATH", help=game_help)
    p_rst.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Confirm reset without prompt (required when stdin is not a TTY)",
    )

    sub.add_parser("wizard", help="Interactive menu (full mod lifecycle)")

    p_st = sub.add_parser(
        "status",
        help="Show enabled mods, game path, overlay, lz4/PAMT health, Finder hint (macOS)",
    )
    p_st.add_argument("--game", default=None, metavar="PATH", help=game_help)

    # ── New commands ──
    sub.add_parser("available", help="List mods in mods/available/")

    p_act = sub.add_parser(
        "activate",
        help="Move a mod from mods/available/ to mods/enabled/",
    )
    p_act.add_argument("mod", help="Index from available list (1,2,…) or file name / stem")

    p_en = sub.add_parser(
        "enable",
        help="Move a mod from mods/disabled/ to mods/enabled/",
    )
    p_en.add_argument("mod", help="Index from disabled list (1,2,…) or file name / stem")

    sub.add_parser("disabled", help="List mods in mods/disabled/")

    p_pa = sub.add_parser(
        "patches",
        help="List patches in a mod (with ON/OFF status and categories)",
    )
    p_pa.add_argument("mod", help="Mod index (from enabled list) or file name / stem")

    p_tg = sub.add_parser(
        "toggle",
        help="Toggle patches ON/OFF: index, range (5-10), category ([Flight]), all, none",
    )
    p_tg.add_argument("mod", help="Mod index or file name / stem")
    p_tg.add_argument("spec", nargs="+", help="Toggle spec: 5, 5-10, [Flight], all, none, 'on 5', 'off [Sprint]'")

    p_tc = sub.add_parser(
        "toggle-category",
        help="Enable/disable all patches in one category",
    )
    p_tc.add_argument("mod", help="Mod index or file name / stem")
    p_tc.add_argument("category", help="Category name without brackets (e.g. Flight)")
    p_tc.add_argument("state", choices=["on", "off"], help="Turn category on or off")

    p_pu = sub.add_parser(
        "purge",
        help="Permanently delete a mod from any location",
    )
    p_pu.add_argument("mod", help="File name / stem")

    sub.add_parser("start-game", help="Start Crimson Desert (macOS only)")

    return parser


def _print_cli_banner():
    print()
    print(f"{Style.BOLD}Crimson Desert — JSON Mod Manager{Style.RESET}")
    print(f"{Style.DIM}PAMT / PAZ / PAPGT overlay ({MOD_DIR_NAME}){Style.RESET}")
    print()


def main():
    argv = sys.argv[1:]

    if argv and argv[0].startswith("--"):
        main_legacy(argv)
        return

    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        print()
        info("Quick start:  python3 mod_manager.py wizard")
        info("Install + apply:  python3 mod_manager.py install ./mods/<folder> --apply")
        return

    if args.command == "wizard":
        run_wizard()
        return

    if args.command == "status":
        _print_cli_banner()
        cmd_status(getattr(args, "game", None))
        return

    _print_cli_banner()

    if args.command == "set-game":
        if not cmd_set_game_cli(args.game_path):
            sys.exit(1)
        return

    game_arg = getattr(args, "game", None)

    if args.command == "list":
        cmd_list()
        return

    if args.command == "install":
        recursive = not args.no_recursive
        cmd_install_from_folder(
            args.mod_folder,
            recursive,
            force=args.force,
            apply_after=args.apply,
            game_opt=game_arg,
            max_files=args.max_files,
            pick=args.pick,
        )
        return

    if args.command == "scan":
        recursive = not args.no_recursive
        cmd_scan_mod_folder(args.mod_folder, game_arg, recursive=recursive)
        return

    if args.command == "disable":
        cmd_disable_enabled_mod(args.mod)
        return

    if args.command == "remove":
        cmd_remove_mod(args.mod, game_arg)
        return

    if args.command == "reset":
        if not cmd_reset(game_arg, assume_yes=args.yes):
            sys.exit(1)
        return

    if args.command == "available":
        cmd_available()
        return

    if args.command == "activate":
        cmd_activate_available(args.mod)
        return

    if args.command == "enable":
        cmd_enable_mod(args.mod)
        return

    if args.command == "disabled":
        cmd_disabled_list()
        return

    if args.command == "patches":
        cmd_patches(args.mod)
        return

    if args.command == "toggle":
        spec = " ".join(args.spec)
        cmd_toggle_patch(args.mod, spec)
        return

    if args.command == "toggle-category":
        cmd_toggle_category(args.mod, args.category, on=(args.state == "on"))
        return

    if args.command == "purge":
        cmd_purge_mod(args.mod)
        return

    if args.command == "start-game":
        if not init_game_dir(game_arg):
            game_dir_help()
            sys.exit(1)
        cmd_start_game()
        return

    if args.command in ("apply", "restore"):
        if not init_game_dir(game_arg):
            game_dir_help()
            sys.exit(1)
        info(f"Game packages: {GAME_DIR}")
        print()
        if args.command == "apply":
            cmd_apply()
        else:
            cmd_uninstall()
        return


if __name__ == "__main__":
    main()
