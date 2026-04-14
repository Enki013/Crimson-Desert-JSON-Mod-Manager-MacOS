r"""
PAMT Patcher — modifies an existing PAMT to redirect specific files to a new PAZ.

This creates MODIFIED copies of PAMT and PAPGT in a staging directory.
Original game files are NEVER touched. The hook DLL will redirect reads at runtime.

Usage:
    from pamt_patcher import patch_pamt_for_mods

    patch_pamt_for_mods(
        game_dir="/Applications/Crimson Desert.app/Contents/Resources/packages",
        staging_dir="./mods_data",
        mod_files={
            "gamedata/skill.pabgb": {
                "paz_data": compressed_bytes,
                "comp_size": len(compressed_bytes),
                "decomp_size": 1128500,
            }
        },
        target_group="0008",  # which group contains the files to patch
    )
"""

import os
import struct
import shutil
from pa_checksum import pa_checksum


def read_pamt_raw(pamt_path):
    """Read a raw PAMT file and return structured data + raw bytes."""
    with open(pamt_path, 'rb') as f:
        data = bytearray(f.read())

    result = {}
    result['raw'] = data

    # Header (12 bytes)
    result['header_crc'] = struct.unpack_from('<I', data, 0)[0]
    result['paz_count'] = struct.unpack_from('<I', data, 4)[0]
    result['unknown'] = struct.unpack_from('<I', data, 8)[0]

    # PazInfo array (12 bytes each, starting at offset 12)
    paz_infos = []
    off = 12
    for i in range(result['paz_count']):
        paz_infos.append({
            'index': struct.unpack_from('<I', data, off)[0],
            'crc': struct.unpack_from('<I', data, off + 4)[0],
            'file_size': struct.unpack_from('<I', data, off + 8)[0],
        })
        off += 12
    result['paz_infos'] = paz_infos
    result['paz_info_end'] = off  # offset after PazInfo array

    # DirBlock
    dir_size = struct.unpack_from('<I', data, off)[0]
    result['dir_block_offset'] = off
    result['dir_block_size'] = dir_size
    off += 4 + dir_size

    # FileNameBlock
    fn_size = struct.unpack_from('<I', data, off)[0]
    result['fn_block_offset'] = off
    result['fn_block_size'] = fn_size
    fn_data = data[off + 4:off + 4 + fn_size]
    result['fn_data'] = fn_data
    off += 4 + fn_size

    # DirHashTable
    hash_count = struct.unpack_from('<I', data, off)[0]
    off += 4
    result['hash_table_offset'] = off - 4
    result['hash_count'] = hash_count
    hash_entries = []
    for i in range(hash_count):
        hash_entries.append({
            'folder_hash': struct.unpack_from('<I', data, off)[0],
            'name_offset': struct.unpack_from('<I', data, off + 4)[0],
            'file_start_index': struct.unpack_from('<I', data, off + 8)[0],
            'file_count': struct.unpack_from('<I', data, off + 12)[0],
        })
        off += 16
    result['hash_entries'] = hash_entries

    # FileRecords
    file_count = struct.unpack_from('<I', data, off)[0]
    result['file_records_offset'] = off
    result['file_count'] = file_count
    off += 4
    file_records = []
    for i in range(file_count):
        fr = {
            'name_offset': struct.unpack_from('<I', data, off)[0],
            'paz_offset': struct.unpack_from('<I', data, off + 4)[0],
            'comp_size': struct.unpack_from('<I', data, off + 8)[0],
            'decomp_size': struct.unpack_from('<I', data, off + 12)[0],
            'paz_index': struct.unpack_from('<H', data, off + 16)[0],
            'flags': struct.unpack_from('<H', data, off + 18)[0],
            '_byte_offset': off,  # remember where this record lives
        }
        file_records.append(fr)
        off += 20
    result['file_records'] = file_records

    return result


def resolve_filename(fn_data, name_offset):
    """Resolve a filename from the FileNameBlock using VFS path traversal."""
    parts = []
    current = name_offset
    depth = 0
    while current != 0xFFFFFFFF and depth < 64:
        if current >= len(fn_data):
            break
        parent = struct.unpack_from('<I', fn_data, current)[0]
        slen = fn_data[current + 4]
        name = fn_data[current + 5:current + 5 + slen].decode('utf-8', errors='replace')
        parts.append(name)
        current = parent
        depth += 1
    return ''.join(reversed(parts))


def resolve_dirname(dir_data, name_offset):
    """Resolve a directory name from the DirBlock."""
    parts = []
    current = name_offset
    depth = 0
    while current != 0xFFFFFFFF and depth < 64:
        if current >= len(dir_data):
            break
        parent = struct.unpack_from('<I', dir_data, current)[0]
        slen = dir_data[current + 4]
        name = dir_data[current + 5:current + 5 + slen].decode('utf-8', errors='replace')
        parts.append(name)
        current = parent
        depth += 1
    return ''.join(reversed(parts))


def find_file_record(pamt_info, target_filename):
    """Find a file record by filename. Returns (index, record) or None."""
    fn_data = pamt_info['fn_data']
    dir_data = pamt_info['raw'][
        pamt_info['dir_block_offset'] + 4:
        pamt_info['dir_block_offset'] + 4 + pamt_info['dir_block_size']
    ]

    # Build dir path lookup: hash_entry → dir_path
    dir_paths = {}
    for he in pamt_info['hash_entries']:
        dir_path = resolve_dirname(dir_data, he['name_offset'])
        dir_paths[he['folder_hash']] = dir_path

    # Search file records
    for idx, fr in enumerate(pamt_info['file_records']):
        fname = resolve_filename(fn_data, fr['name_offset'])

        # Find which directory this file belongs to
        for he in pamt_info['hash_entries']:
            if he['file_start_index'] <= idx < he['file_start_index'] + he['file_count']:
                dir_path = dir_paths[he['folder_hash']]
                full_path = dir_path + '/' + fname if dir_path else fname
                if full_path == target_filename:
                    return idx, fr
                break

    return None


def patch_pamt(original_pamt_path, mod_files, new_paz_size):
    """Create a modified PAMT with files redirected to a new PAZ.

    Args:
        original_pamt_path: path to original 0.pamt
        mod_files: dict: game_file_path → {comp_size, decomp_size, paz_offset}
        new_paz_size: total size of the new PAZ file

    Returns:
        bytes: modified PAMT binary with correct CRCs
    """
    pamt_info = read_pamt_raw(original_pamt_path)
    data = bytearray(pamt_info['raw'])

    # Step 1: Add new PazInfo entry
    old_paz_count = pamt_info['paz_count']
    new_paz_index = old_paz_count  # e.g., if was 3, new is 3

    # Insert 12 bytes for new PazInfo AFTER existing ones
    insert_offset = pamt_info['paz_info_end']
    new_paz_info = struct.pack('<III',
        new_paz_index,  # Index
        0,              # Crc placeholder (updated later)
        new_paz_size,   # FileSize
    )
    data[insert_offset:insert_offset] = new_paz_info

    # Update PazCount
    struct.pack_into('<I', data, 4, old_paz_count + 1)

    # All offsets after insert_offset shifted by 12
    SHIFT = 12

    # Step 2: Find and update file records for each modded file
    # We need to re-parse after insertion since offsets shifted
    # The file_records_offset shifted by SHIFT
    new_fr_offset = pamt_info['file_records_offset'] + SHIFT
    file_count = struct.unpack_from('<I', data, new_fr_offset)[0]

    # Re-parse file records at new position
    fn_data_offset = pamt_info['fn_block_offset'] + SHIFT
    fn_size = struct.unpack_from('<I', data, fn_data_offset)[0]
    fn_data = data[fn_data_offset + 4:fn_data_offset + 4 + fn_size]

    dir_data_offset = pamt_info['dir_block_offset'] + SHIFT
    dir_size = struct.unpack_from('<I', data, dir_data_offset)[0]
    dir_data = data[dir_data_offset + 4:dir_data_offset + 4 + dir_size]

    hash_table_offset = pamt_info['hash_table_offset'] + SHIFT
    hash_count = struct.unpack_from('<I', data, hash_table_offset)[0]

    # Build dir_paths
    dir_paths = {}
    ht_off = hash_table_offset + 4
    hash_entries_shifted = []
    for i in range(hash_count):
        he = {
            'folder_hash': struct.unpack_from('<I', data, ht_off)[0],
            'name_offset': struct.unpack_from('<I', data, ht_off + 4)[0],
            'file_start_index': struct.unpack_from('<I', data, ht_off + 8)[0],
            'file_count': struct.unpack_from('<I', data, ht_off + 12)[0],
        }
        hash_entries_shifted.append(he)
        dir_paths[he['folder_hash']] = resolve_dirname(dir_data, he['name_offset'])
        ht_off += 16

    # Scan file records
    fr_off = new_fr_offset + 4
    for i in range(file_count):
        fr_name_offset = struct.unpack_from('<I', data, fr_off)[0]
        fname = resolve_filename(fn_data, fr_name_offset)

        # Find directory
        for he in hash_entries_shifted:
            if he['file_start_index'] <= i < he['file_start_index'] + he['file_count']:
                dir_path = dir_paths[he['folder_hash']]
                full_path = dir_path + '/' + fname if dir_path else fname

                if full_path in mod_files:
                    mf = mod_files[full_path]
                    print(f"  Patching record [{i}] {full_path}:")
                    print(f"    old: paz_idx={struct.unpack_from('<H', data, fr_off+16)[0]}, "
                          f"offset=0x{struct.unpack_from('<I', data, fr_off+4)[0]:08X}, "
                          f"comp={struct.unpack_from('<I', data, fr_off+8)[0]}")
                    # Update: paz_offset, comp_size, paz_index
                    struct.pack_into('<I', data, fr_off + 4, mf['paz_offset'])
                    struct.pack_into('<I', data, fr_off + 8, mf['comp_size'])
                    struct.pack_into('<I', data, fr_off + 12, mf['decomp_size'])
                    struct.pack_into('<H', data, fr_off + 16, new_paz_index)
                    print(f"    new: paz_idx={new_paz_index}, "
                          f"offset=0x{mf['paz_offset']:08X}, "
                          f"comp={mf['comp_size']}")
                break

        fr_off += 20

    # Step 3: Recalculate HeaderCrc (skip first 12 bytes)
    new_header_crc = pa_checksum(bytes(data[12:]))
    struct.pack_into('<I', data, 0, new_header_crc)

    return bytes(data)


def update_pamt_new_paz_crc(pamt_data, paz_crc, paz_index):
    """Update the CRC for a specific PazInfo entry and recalculate HeaderCrc."""
    data = bytearray(pamt_data)
    paz_count = struct.unpack_from('<I', data, 4)[0]

    # Find the PazInfo entry with matching index
    for i in range(paz_count):
        off = 12 + i * 12
        idx = struct.unpack_from('<I', data, off)[0]
        if idx == paz_index:
            struct.pack_into('<I', data, off + 4, paz_crc)
            break

    # Recalculate HeaderCrc
    new_crc = pa_checksum(bytes(data[12:]))
    struct.pack_into('<I', data, 0, new_crc)
    return bytes(data)


def build_mod_papgt(original_papgt_path, group_name, new_pamt_crc):
    """Create modified PAPGT with updated PamtCrc for a specific group.

    Returns:
        bytes: modified PAPGT binary
    """
    with open(original_papgt_path, 'rb') as f:
        data = bytearray(f.read())

    group_count = data[8]

    # Find string block
    sbo = 12 + group_count * 12
    str_block_size = struct.unpack_from('<I', data, sbo)[0]
    str_data = data[sbo + 4:]

    # Find the group entry matching group_name
    for i in range(group_count):
        off = 12 + i * 12
        name_offset = struct.unpack_from('<I', data, off + 4)[0]
        end = str_data.find(b'\x00', name_offset)
        name = str_data[name_offset:end].decode('ascii')

        if name == group_name:
            # Update PamtCrc
            old_crc = struct.unpack_from('<I', data, off + 8)[0]
            struct.pack_into('<I', data, off + 8, new_pamt_crc)
            print(f"  Updated PAPGT group '{group_name}': CRC 0x{old_crc:08X} → 0x{new_pamt_crc:08X}")

            # Recalculate FileCrc
            new_file_crc = pa_checksum(bytes(data[12:]))
            struct.pack_into('<I', data, 4, new_file_crc)
            print(f"  Updated PAPGT FileCrc: 0x{new_file_crc:08X}")
            return bytes(data)

    raise ValueError(f"Group '{group_name}' not found in PAPGT")


if __name__ == '__main__':
    import sys
    from pathlib import Path

    raw = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Applications/Crimson Desert.app")
    raw = raw.expanduser().resolve()
    if raw.suffix == ".app" and raw.is_dir():
        game_dir = raw / "Contents" / "Resources" / "packages"
    else:
        game_dir = raw
    pamt_path = game_dir / "0008" / "0.pamt"

    # Just test parsing
    info = read_pamt_raw(str(pamt_path))
    print(f"PAMT: {len(info['raw'])} bytes")
    print(f"PazCount: {info['paz_count']}")
    print(f"FileRecords: {info['file_count']}")

    # Find skill.pabgb
    result = find_file_record(info, "gamedata/skill.pabgb")
    if result:
        idx, fr = result
        print(f"\nFound skill.pabgb at record [{idx}]:")
        print(f"  paz_index={fr['paz_index']}, offset=0x{fr['paz_offset']:08X}")
        print(f"  comp={fr['comp_size']}, decomp={fr['decomp_size']}")
        print(f"  flags=0x{fr['flags']:04X}")
    else:
        print("skill.pabgb NOT FOUND!")
