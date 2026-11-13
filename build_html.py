#!/usr/bin/env python3
"""
build_html.py - assemble self-contained GoldSrc-web HTML builds (Universal build)

Embeds the WebXash3D-FWGS engine (xash.wasm + side modules + raw.js glue) and
the game archives required by the selected GAME DEFINITION into ONE
self-contained HTML file. Payloads are streamed to disk in 3 MiB chunks and
base64-encoded on the fly - no archive is ever fully resident in RAM.

Game modes are defined in the GAMES dictionary below (game_dir, engine_args,
payloads, engine module set). Pick one with --game; only the payloads listed
for that game are base64-encoded, and the matching engine_args are injected
into the CONFIG block the loader template reads at boot.

Game archives are normalized so that the engine's archive scanner finds them.
Xash3D FWGS filesystem semantics (FWGS/xash3d-fwgs filesystem/searchpath.c):
  - FS_AddGameHierarchy mounts <rootdir>/<dir>/ and <rodir>/<dir>/ for each
    game dir ("valve" base + "<gamedir>"); it scans ONLY those folders
    for *.pak / *.pk3 / *.wad files and mounts each as an archive;
  - archive entries are looked up VERBATIM by bare name (FS_FindFile ->
    search->pfnFindFile), so a pk3 must contain gamedir-relative paths
    ("gfx.wad", "maps/de_dust2.bsp"...) - NOT a "valve/..." prefix.
    The engine's own extras.pk3 follows exactly this bare convention.
Therefore we repack:
  valve.zip   (entries valve/...)   -> /rodir/valve/valve.pk3   (bare entries)
  cstrike.zip (entries cstrike/...) -> /rodir/cstrike/cstrike.pk3
  dod.zip (entries dod/...)         -> /rodir/dod/dod.pk3       (bare entries)
The tiny mod-discovery files (liblist.gam / gameinfo.txt) are extracted and
embedded as real files (valve/liblist.gam, cstrike/liblist.gam, ...) because
mod discovery reads them from the mounted game directories.

Usage:
  python3 build_html.py --game cstrike|valve|dod
                        [--game-dir GAME] [--engine-dir DIR]
                        [--template FILE] [--out FILE]
                        [--valve-zip FILE] [--cstrike-zip FILE] [--dod-zip FILE]
                        [--list-games] [--skip-verify]
"""
import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
CHUNK = 3 * 1024 * 1024  # multiple of 3 -> exactly 4 MiB of base64 chars, padding only at EOF

# ---------------------------------------------------------------------------
# Engine modules.
# A set = engine core + FS + renderer (shared) + per-game menu/client/server
# side modules. Each entry: (tag id, file in engine-dir, locateFile key, label)
# ---------------------------------------------------------------------------
ENGINE_FILES_BASE = [
    ('b64-xash', 'xash.wasm', 'xash.wasm', 'xash.wasm (engine core)'),
    ('b64-fs', 'filesystem_stdio.wasm', 'filesystem_stdio.wasm', 'filesystem_stdio.wasm (FS module)'),
    ('b64-ref', 'libref_gles3compat.wasm', 'libref_gles3compat.wasm', 'libref_gles3compat.wasm (WebGL renderer)'),
]
ENGINE_FILES_MENU = [
    ('b64-menu', 'menu_emscripten_wasm32.wasm', 'cl_dlls/menu_emscripten_wasm32.wasm',
     'menu_emscripten_wasm32.wasm (menu)'),
]
ENGINE_FILES_CLIENT = [
    ('b64-client', 'client_emscripten_wasm32.wasm', 'cl_dlls/client_emscripten_wasm32.wasm',
     'client_emscripten_wasm32.wasm (client dll)'),
]

# -dll: verbatim gamedll override - this engine build has no '@name' syntax
# and appends no postfix, so pass the exact LDSO key of the preloaded server
# side module. The dest/registered name uses the .so extension so the
# engine-generated gamedll name matches the Emscripten LDSO registry key
# exactly (extension is irrelevant - the loader checks the wasm dylink magic).

ENGINE_FILES_CS = (ENGINE_FILES_BASE + ENGINE_FILES_MENU + ENGINE_FILES_CLIENT + [
    ('b64-cs', 'cs_emscripten_wasm32.so', 'dlls/cs_emscripten_wasm32.so',
     'cs_emscripten_wasm32.so (CS server)'),
    ('b64-yapb', 'yapb_emscripten_wasm32.wasm', 'dlls/yapb_emscripten_wasm32.so',
     'yapb_emscripten_wasm32 (YaPB bot AI)'),
])
DYNAMIC_LIBRARIES_CS = [
    'filesystem_stdio.wasm',
    'libref_gles3compat.wasm',
    'cl_dlls/menu_emscripten_wasm32.wasm',
    'dlls/cs_emscripten_wasm32.so',
    'cl_dlls/client_emscripten_wasm32.wasm',
    '/rwdir/filesystem_stdio.so',
    # YaPB bot manager: loaded as an Emscripten side module; the engine then
    # opens it as the server game DLL via -dll (cs16-client convention), and
    # yapb itself dlopens the real CS server lib through XASH3D_GAMELIBPATH
    # (set via Module.ENV in the loader template).
    'dlls/yapb_emscripten_wasm32.so',
]

ENGINE_FILES_HL = (ENGINE_FILES_BASE + ENGINE_FILES_MENU + ENGINE_FILES_CLIENT + [
    ('b64-hl', 'hl_emscripten_wasm32.so', 'dlls/hl_emscripten_wasm32.so',
     'hl_emscripten_wasm32.so (HL server)'),
])
DYNAMIC_LIBRARIES_HL = [
    'filesystem_stdio.wasm',
    'libref_gles3compat.wasm',
    'cl_dlls/menu_emscripten_wasm32.wasm',
    'dlls/hl_emscripten_wasm32.so',
    'cl_dlls/client_emscripten_wasm32.wasm',
    '/rwdir/filesystem_stdio.so',
]

ENGINE_FILES_DOD = (ENGINE_FILES_BASE + ENGINE_FILES_MENU + ENGINE_FILES_CLIENT + [
    ('b64-dod', 'dod_emscripten_wasm32.so', 'dlls/dod_emscripten_wasm32.so',
     'dod_emscripten_wasm32.so (DoD server)'),
])
DYNAMIC_LIBRARIES_DOD = [
    'filesystem_stdio.wasm',
    'libref_gles3compat.wasm',
    'cl_dlls/menu_emscripten_wasm32.wasm',
    'dlls/dod_emscripten_wasm32.so',
    'cl_dlls/client_emscripten_wasm32.wasm',
    '/rwdir/filesystem_stdio.so',
]

# ---------------------------------------------------------------------------
# Universal game definitions.
#   game_dir:         gamedir passed to the engine via -game (also the mod
#                     folder name the archives are normalized against)
#   engine_args:      full argv injected into CONFIG.args; the loader template
#                     passes it verbatim to the Xash3D engine on boot
#   payloads:         game archives to embed (each <name> is read from
#                     <game-dir>/<name>.zip or --<name>-zip and embedded as
#                     /rodir/<name>/<name>.pk3). "valve" is the Half-Life
#                     base data every GoldSrc mod needs.
#   game_lib_path:    XASH3D_GAMELIBPATH env value for the loader (the real
#                     server lib a wrapper bot dll dlopens; must match the
#                     LDSO registry key in dynamic_libraries)
#   engine_files:     which engine side modules this game needs
#   dynamic_libraries: CONFIG.dynamicLibraries for the loader
# ---------------------------------------------------------------------------
GAMES = {
    'cstrike': {
        'title': 'Counter-Strike 1.6',
        'game_dir': 'cstrike',
        'engine_args': ['-windowed', '-game', 'cstrike',
                        '-dll', 'dlls/yapb_emscripten_wasm32.so',
                        '+_vgui_menus', '0'],
        'payloads': ['valve', 'cstrike'],
        'game_lib_path': 'dlls/cs_emscripten_wasm32.so',
        'engine_files': ENGINE_FILES_CS,
        'dynamic_libraries': DYNAMIC_LIBRARIES_CS,
    },
    'valve': {
        'title': 'Half-Life',
        'game_dir': 'valve',
        'engine_args': ['-windowed', '-game', 'valve',
                        '-dll', 'dlls/hl_emscripten_wasm32.so'],
        'payloads': ['valve'],
        'game_lib_path': 'dlls/hl_emscripten_wasm32.so',
        'engine_files': ENGINE_FILES_HL,
        'dynamic_libraries': DYNAMIC_LIBRARIES_HL,
    },
    'dod': {
        'title': 'Day of Defeat',
        'game_dir': 'dod',
        'engine_args': ['-windowed', '-game', 'dod',
                        '-dll', 'dlls/dod_emscripten_wasm32.so'],
        'payloads': ['valve', 'dod'],
        'game_lib_path': 'dlls/dod_emscripten_wasm32.so',
        'engine_files': ENGINE_FILES_DOD,
        'dynamic_libraries': DYNAMIC_LIBRARIES_DOD,
    },
}

PAYLOAD_LABELS = {
    'valve': 'valve.pk3 (Half-Life base data)',
    'cstrike': 'cstrike.pk3 (Counter-Strike data)',
    'dod': 'dod.pk3 (Day of Defeat data)',
}
PAYLOAD_DEFAULT_LABEL = '{name}.pk3 ({game} data)'


def norm_name(name):
    return name.replace('\\', '/').lstrip('/')


def entry_names(zf):
    return [norm_name(i.filename) for i in zf.infolist() if not i.is_dir()]


def find_gi(zf, target):
    """Locate liblist.gam / gameinfo.txt bytes for a mod folder inside the archive.
    Both prefixed (valve/liblist.gam) and bare (liblist.gam) layouts supported."""
    names = entry_names(zf)
    for candidate in (f'{target}/liblist.gam', f'{target}/gameinfo.txt',
                      'liblist.gam', 'gameinfo.txt'):
        if candidate in names:
            return candidate, zf.read(candidate)
    return None, None


def has_extras(zf, target):
    names = entry_names(zf)
    return f'{target}/extras.pk3' in names or 'extras.pk3' in names


def normalize_archive(src, target, out_path):
    """Return (embedded_path, gi_name, gi_bytes, extras_present).
    Produces a pk3 whose entries are RELATIVE TO THE GAMEDIR ROOT (bare):
    Xash3D mounts pk3s found in the game directories as searchpaths and
    looks files up verbatim by bare name, so 'valve/gfx.wad' must become
    'gfx.wad' inside valve.pk3."""
    with zipfile.ZipFile(src) as zf:
        names = entry_names(zf)
        gi_name, gi_bytes = find_gi(zf, target)
        extras = has_extras(zf, target)
        prefixed = [n.startswith(target + '/') for n in names]
        if all(prefixed):
            print(f'  [{os.path.basename(src)}] {len(names)} entries under {target}/ -> '
                  f'repacking gamedir-relative (bare) entries')
        elif not any(prefixed):
            print(f'  [{os.path.basename(src)}] entries already gamedir-relative '
                  f'({len(names)} files) - no repack needed')
            return src, gi_name, gi_bytes, extras
        else:
            mixed = [n for n, p in zip(names, prefixed) if not p][:5]
            raise SystemExit(f'{src}: mixed layout, entries outside {target}/: {mixed}')

    plen = len(target) + 1
    with zipfile.ZipFile(src) as zin, \
         zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zout:
        for info in zin.infolist():
            if info.is_dir():
                continue
            name = norm_name(info.filename)[plen:]
            if not name:
                continue
            zi = zipfile.ZipInfo(name, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi._compresslevel = 1
            zi.external_attr = info.external_attr
            with zin.open(info) as fin, zout.open(zi, 'w') as fout:
                shutil.copyfileobj(fin, fout, 1024 * 1024)
    return out_path, gi_name, gi_bytes, extras


def sha256_file(path, off=0, size=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        f.seek(off)
        remaining = size
        while True:
            b = f.read(min(1024 * 1024, remaining if remaining is not None else 1024 * 1024))
            if not b:
                break
            h.update(b)
            if remaining is not None:
                remaining -= len(b)
                if remaining <= 0:
                    break
    return h.hexdigest()


class TagWriter:
    """Streams a payload into the output HTML inside an inert script tag,
    recording byte offsets for post-build verification."""

    def __init__(self, out_f):
        self.f = out_f

    def open_tag(self, tag_id):
        self.start = self.f.tell()
        self.f.write(f'<script type="text/plain" id="{tag_id}">'.encode('ascii'))

    def stream_path(self, path):
        size = os.path.getsize(path)
        with open(path, 'rb') as src:
            while True:
                block = src.read(CHUNK)
                if not block:
                    break
                self.f.write(base64.b64encode(block))
        self.end = self.f.tell()
        return size

    def stream_bytes(self, data):
        self.f.write(base64.b64encode(data))
        self.end = self.f.tell()
        return len(data)

    def close_tag(self):
        self.f.write('</script>\n'.encode('ascii'))


def verify_tag(out_path, tag_id, start, end, src_path=None, src_bytes=None):
    """Check tag length parity and head/tail SHA-256 against the source payload."""
    size = os.path.getsize(src_path) if src_path else len(src_bytes)
    expected_len = 4 * ((size + 2) // 3)
    actual_len = end - start
    if actual_len != expected_len:
        return f'{tag_id}: LENGTH MISMATCH ({actual_len} != {expected_len})'
    probe_b64 = min(1024 * 1024, actual_len)
    probe_b64 -= probe_b64 % 4           # quantum alignment
    probe_bytes = probe_b64 // 4 * 3
    with open(out_path, 'rb') as f:
        f.seek(start)
        head_b64 = f.read(probe_b64)
        f.seek(end - probe_b64)
        tail_b64 = f.read(probe_b64)
    head_dec = base64.b64decode(head_b64, validate=True)
    tail_dec = base64.b64decode(tail_b64, validate=True)  # includes '=' padding on final quantum
    if src_path:
        with open(src_path, 'rb') as f:
            src_head = f.read(len(head_dec))
            if len(tail_dec) >= size:
                f.seek(0)
                src_tail = f.read()
            else:
                f.seek(-len(tail_dec), 2)
                src_tail = f.read()
    else:
        src_head = src_bytes[:len(head_dec)]
        src_tail = src_bytes[-len(tail_dec):] if len(tail_dec) else b''
    ok_h = hashlib.sha256(head_dec).hexdigest() == hashlib.sha256(src_head).hexdigest()
    ok_t = hashlib.sha256(tail_dec).hexdigest() == hashlib.sha256(src_tail).hexdigest()
    if not (ok_h and ok_t):
        return f'{tag_id}: SHA256 HEAD/TAIL MISMATCH (head={ok_h} tail={ok_t})'
    return None


def list_games():
    print(json.dumps(GAMES, indent=2))


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument('--game', choices=sorted(GAMES), default='cstrike',
                    help='game mode to build (default: cstrike)')
    ap.add_argument('--game-dir', default=os.path.join(ROOT, 'game'))
    ap.add_argument('--engine-dir', default=os.path.join(ROOT, 'engine', 'webxash'))
    ap.add_argument('--template', default=os.path.join(ROOT, 'template.html'))
    ap.add_argument('--out', default=None,
                    help='output HTML (default: download/<game>-offline.html; '
                         'cs16-offline.html for --game cstrike)')
    for name in sorted({p for g in GAMES.values() for p in g['payloads']}):
        ap.add_argument(f'--{name}-zip', default=None,
                        help=f'path to {name}.zip (default: <game-dir>/{name}.zip)')
    ap.add_argument('--list-games', action='store_true',
                    help='print the game definition dictionary and exit')
    ap.add_argument('--skip-verify', action='store_true')
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()

    if args.list_games:
        list_games()
        return

    game = GAMES[args.game]
    game_dir_name = game['game_dir']

    if args.out is None:
        out_name = 'cs16-offline.html' if args.game == 'cstrike' else f'{args.game}-offline.html'
        args.out = os.path.join(ROOT, 'download', out_name)

    engine_files = game['engine_files']
    payload_names = game['payloads']

    payload_srcs = {}
    for name in payload_names:
        src = getattr(args, f'{name}_zip') or os.path.join(args.game_dir, f'{name}.zip')
        if not os.path.isfile(src):
            raise SystemExit(f'missing game archive for {args.game}: {src}\n'
                             f'(provide it via --{name}-zip or place {name}.zip in --game-dir)')
        payload_srcs[name] = src
    for tag, fname, _, _ in engine_files:
        p = os.path.join(args.engine_dir, fname)
        if not os.path.isfile(p):
            raise SystemExit(f'missing engine file: {p}')
    raw_js_path = os.path.join(args.engine_dir, 'raw.js')
    extras_path = os.path.join(args.engine_dir, 'extras.pk3')
    raw_js = open(raw_js_path, encoding='utf-8').read()
    if '</script' in raw_js.lower():
        raise SystemExit('raw.js contains a literal </script - cannot inline safely')

    print(f'== normalizing game archives (game: {args.game} - {game["title"]}) ==')
    normalized = {}
    for name in payload_names:
        path, gi_n, gi_b, extras = normalize_archive(
            payload_srcs[name], name, os.path.join(args.game_dir, f'{name}.pk3'))
        normalized[name] = (path, gi_n, gi_b, extras)
        print(f'  {name}: {path}  gi={gi_n} extras_in_zip={extras}')

    print('== embedding ==')
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    template = open(args.template, encoding='utf-8').read()
    m_cfg = template.find('/*__CONFIG__*/')
    m_pay = template.find('<!--__PAYLOADS__-->')
    m_glue = template.find('<!--__GLUE__-->')
    if -1 in (m_cfg, m_pay, m_glue):
        raise SystemExit('template markers missing')
    head = template[:m_cfg] + '%CONFIG%' + template[m_cfg + len('/*__CONFIG__*/'):m_pay]
    tail_start = template.find('<script>', m_glue + len('<!--__GLUE__-->'))
    tail = template[tail_start:]

    payloads, gameinfo, tags = [], [], []

    def add_path_payload(tag_id, path, kind, dest, label):
        payloads.append({'id': tag_id, 'label': label, 'kind': kind, 'dest': dest,
                         'size': os.path.getsize(path)})
        tags.append(('path', tag_id, path))

    def add_bytes_payload(tag_id, data, kind, dest, label, into_payloads=True):
        entry = {'id': tag_id, 'label': label, 'kind': kind, 'dest': dest, 'size': len(data)}
        if into_payloads:
            payloads.append(entry)
        else:
            gameinfo.append(entry)
        tags.append(('bytes', tag_id, data))

    for tag_id, fname, dest, label in engine_files:
        add_path_payload(tag_id, os.path.join(args.engine_dir, fname), 'bytes', dest, label)

    for name in payload_names:
        path, gi_n, gi_b, _ = normalized[name]
        label = PAYLOAD_LABELS.get(name, PAYLOAD_DEFAULT_LABEL.format(name=name, game=game['title']))
        add_path_payload(f'b64-{name}', path, 'file', f'/rodir/{name}/{name}.pk3', label)
        if gi_b:
            add_bytes_payload(f'b64-gi-{name}', gi_b, 'file',
                              f'/rodir/{name}/{os.path.basename(gi_n)}',
                              f'{gi_n} (mod discovery)', into_payloads=False)

    if 'valve' in payload_names:
        valve_extras = normalized['valve'][3]
        if not valve_extras and os.path.isfile(extras_path):
            add_path_payload('b64-extras', extras_path, 'file', '/rodir/valve/extras.pk3',
                             'extras.pk3 (engine extras)')
        elif valve_extras:
            print('  valve archive already contains extras.pk3 - skipping engine extras')

    config = {
        'game': args.game,
        'title': game['title'],
        'gameDir': game_dir_name,
        'mountDir': '/rodir',
        'gameLibPath': game['game_lib_path'],
        'args': game['engine_args'],
        'dynamicLibraries': game['dynamic_libraries'],
        'payloads': payloads,
        'gameinfo': gameinfo,
    }

    cfg_json = json.dumps(config, ensure_ascii=True, separators=(',', ':'))

    with open(args.out, 'wb') as out_f:
        out_f.write(head.replace('%CONFIG%', cfg_json).encode('ascii'))
        out_f.write('\n'.encode('ascii'))
        tw = TagWriter(out_f)
        offsets = []
        for kind, tag_id, payload in tags:
            tw.open_tag(tag_id)
            if kind == 'path':
                sz = tw.stream_path(payload)
                src = ('path', payload)
            else:
                sz = tw.stream_bytes(payload)
                src = ('bytes', payload)
            tw.close_tag()
            offsets.append((tag_id, tw.start + len(f'<script type="text/plain" id="{tag_id}">'),
                            tw.end, src, sz))
            print(f'  embedded {tag_id:<16} {sz / 1048576:9.2f} MB')
        glue_start = out_f.tell()
        out_f.write('<script>\n'.encode('ascii') + raw_js.encode('utf-8') + '\n</script>\n'.encode('ascii'))
        out_f.write(tail.encode('ascii'))

    total_out = os.path.getsize(args.out)
    print(f'== output ==\n  {args.out}\n  {total_out:,} bytes ({total_out / 1048576:.1f} MiB)')

    if not args.skip_verify:
        print('== verifying embedded payloads ==')
        failures = 0
        for tag_id, start, end, src, sz in offsets:
            err = verify_tag(args.out, tag_id, start, end,
                             src_path=src[1] if src[0] == 'path' else None,
                             src_bytes=src[1] if src[0] == 'bytes' else None)
            if err:
                print(f'  FAIL {err}')
                failures += 1
            else:
                print(f'  OK   {tag_id} ({sz:,} bytes)')
        if failures:
            raise SystemExit(f'verification failed for {failures} payload(s)')

    print('== source hashes ==')
    for kind, tag_id, payload in tags:
        if kind == 'path':
            print(f'  {tag_id:<16} {sha256_file(payload)}  {os.path.basename(payload)}')
    print(f'  OUTPUT          {sha256_file(args.out)}')
    print('DONE')


if __name__ == '__main__':
    main()
