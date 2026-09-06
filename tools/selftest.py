#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/selftest.py – MemeOS-Selbsttest von der Kommandozeile, ohne Server und ohne Risiko.

Was passiert:
  1. Alle Schlüssel-Variablen werden geleert (ANTHROPIC, Cloudinary, Canva, ContentOS, RapidAPI,
     Telegram) und Scheduler/Worker abgeschaltet – .env kann sie nicht mehr nachfüllen
     (load_dotenv überschreibt gesetzte Variablen nicht).
  2. Die Datenbank wird KOPIERT (Standard: <Arbeitsordner>/selftest_cli.db); die echte
     instance/memeos.db wird nie geöffnet. --live gibt es absichtlich nicht.
  3. Ein eigener Datenpfad (MEMEOS_DATA_ROOT) bekommt Kopien der Upload-Bilder und Schriften.
  4. app wird importiert, alle Blueprints des Projekts registriert (falls register_extensions
     sie noch nicht kennt) und GET /api/selftest über den Flask-test_client aufgerufen.
  5. Ausgabe je Check: ✓ / ✕, Schweregrad, Detail. Exit-Code 1 bei mindestens einem
     fehlgeschlagenen 'crit'-Check, sonst 0 (2 bei Bedienfehler).

Aufruf:  python3 tools/selftest.py [--quick] [--json] [--db PFAD] [--data-root PFAD] [--keep]
"""
import argparse
import glob
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
LIVE_DB = os.path.join(PROJECT, 'instance', 'memeos.db')

# Arbeitsordner: Scratchpad der laufenden Sitzung, wenn vorhanden; sonst Temp-Ordner
_SCRATCH_DEFAULT = ('/private/tmp/claude-501/-Users-ersinozdemir-Downloads-citybot-2/'
                    '5593af1a-2436-4cf9-935a-67cdf60ca8ca/scratchpad')
WORKDIR = os.getenv('MEMEOS_SELFTEST_DIR') or (
    _SCRATCH_DEFAULT if os.path.isdir(_SCRATCH_DEFAULT)
    else os.path.join(tempfile.gettempdir(), 'memeos_selftest'))

# Variablen, die für den Testlauf leer sein MÜSSEN (echte Schlüssel in .env)
SAFE_ENV = {
    'ANTHROPIC_API_KEY': '', 'CLOUDINARY_URL': '',
    'CANVA_CLIENT_ID': '', 'CANVA_CLIENT_SECRET': '', 'CANVA_REFRESH_TOKEN': '',
    'CONTENT_OS_URL': '', 'CONTENT_OS_KEY': '', 'RAPIDAPI_KEY': '',
    'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_TOKEN': '', 'TELEGRAM_CHAT_ID': '',
    'MEMEOS_SCHEDULER': '0', 'MEMEOS_WORKERS': '0',
}

SEVERITY_ORDER = {'crit': 0, 'warn': 1, 'info': 2}


def _color(enabled, code, text):
    return f'\033[{code}m{text}\033[0m' if enabled else text


def parse_args(argv):
    if '--live' in argv:
        print('--live ist nicht erlaubt: der Selbsttest läuft nur gegen eine DB-Kopie '
              '(Standard: ' + os.path.join(WORKDIR, 'selftest_cli.db') + ').', file=sys.stderr)
        sys.exit(2)
    ap = argparse.ArgumentParser(description='MemeOS-Selbsttest gegen eine Datenbankkopie.')
    ap.add_argument('--db', default=os.path.join(WORKDIR, 'selftest_cli.db'),
                    help='Zielpfad der DB-Kopie (Standard: %(default)s)')
    ap.add_argument('--source', default=LIVE_DB,
                    help='Quelle, die kopiert wird (Standard: instance/memeos.db; wird nur gelesen)')
    ap.add_argument('--data-root', default=None,
                    help='Datenpfad für den Test (Standard: <Arbeitsordner>/data_selftest_cli)')
    ap.add_argument('--keep', action='store_true',
                    help='vorhandene DB-Kopie weiterverwenden statt neu zu kopieren')
    ap.add_argument('--quick', action='store_true', help='nur /api/selftest/quick (Datenpfad, Konfiguration, Hintergrund)')
    ap.add_argument('--json', action='store_true', help='Rohantwort als JSON ausgeben')
    ap.add_argument('--no-color', action='store_true', help='ohne Farben')
    return ap.parse_args(argv)


def prepare_environment(args):
    """Env sicher leeren, DB kopieren, Datenpfad füllen. Gibt (db_path, data_root) zurück."""
    for key, value in SAFE_ENV.items():
        os.environ[key] = value

    db_path = os.path.abspath(args.db)
    if os.path.realpath(db_path) == os.path.realpath(LIVE_DB):
        print('Abbruch: --db zeigt auf die echte instance/memeos.db. Bitte einen anderen Pfad wählen.',
              file=sys.stderr)
        sys.exit(2)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if args.keep and os.path.exists(db_path):
        pass
    elif os.path.isfile(args.source):
        shutil.copy2(args.source, db_path)
        for suffix in ('-wal', '-shm'):
            side = args.source + suffix
            if os.path.isfile(side):
                shutil.copy2(side, db_path + suffix)
    else:
        # keine Quelle: leere DB, app.py legt die Tabellen an
        if os.path.exists(db_path) and not args.keep:
            os.remove(db_path)
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'   # absoluter Pfad → vier Schrägstriche

    data_root = os.path.abspath(args.data_root or os.path.join(WORKDIR, 'data_selftest_cli'))
    for sub in ('uploads', 'renders', 'exports', 'fonts'):
        os.makedirs(os.path.join(data_root, sub), exist_ok=True)
    copied = 0
    for sub in ('uploads', 'fonts'):
        src_dir = os.path.join(PROJECT, 'instance', sub)
        if not os.path.isdir(src_dir):
            continue
        for name in os.listdir(src_dir):
            src = os.path.join(src_dir, name)
            dst = os.path.join(data_root, sub, name)
            if os.path.isfile(src) and not name.startswith('.') and not os.path.exists(dst):
                shutil.copy2(src, dst)
                copied += 1
    os.environ['MEMEOS_DATA_ROOT'] = data_root
    return db_path, data_root, copied


def premigrate_copy(db_path):
    """Spalten, die models.py kennt, aber die DB-Kopie noch nicht, per ALTER TABLE nachziehen.
    Nur auf der Kopie! Grund: db.create_all() ergänzt keine Spalten in bestehenden Tabellen; wenn
    ein anderes Modul gerade Spalten ins Modell aufgenommen hat, würde app.py sonst schon beim
    Import (Seed-Abfragen) scheitern. Gibt die Liste der ergänzten Spalten zurück."""
    import sqlite3
    if not os.path.isfile(db_path):
        return []
    sys.path.insert(0, PROJECT)
    try:
        import models
        from sqlalchemy.dialects import sqlite as sqlite_dialect
    except Exception as ex:
        print(f'Hinweis: Vorab-Migration übersprungen ({type(ex).__name__}: {ex})', file=sys.stderr)
        return []
    added = []
    con = sqlite3.connect(db_path)
    try:
        existing_tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        dialect = sqlite_dialect.dialect()
        for table in models.db.Model.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue   # legt app.py per create_all selbst an
            have = {r[1] for r in con.execute(f'PRAGMA table_info("{table.name}")')}
            for col in table.columns:
                if col.name in have:
                    continue
                try:
                    coltype = col.type.compile(dialect=dialect)
                except Exception:
                    coltype = 'TEXT'
                try:
                    con.execute(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}')
                    added.append(f'{table.name}.{col.name}')
                except sqlite3.OperationalError as ex:
                    print(f'Hinweis: {table.name}.{col.name} nicht ergänzbar: {ex}', file=sys.stderr)
        con.commit()
    finally:
        con.close()
    return added


def register_all_blueprints(appmod):
    """Alle Projektmodule mit init_app() registrieren, die register_extensions noch nicht kennt."""
    flask_app = appmod.app
    report = []
    skip_files = {'app.py', 'models.py', 'memeos_render.py'}
    for path in sorted(glob.glob(os.path.join(PROJECT, '*.py'))):
        fn = os.path.basename(path)
        if fn in skip_files or fn.startswith('test_'):
            continue
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
                src = fh.read()
        except Exception:
            continue
        if not re.search(r'^def init_app\(', src, re.M):
            continue
        mod_name = fn[:-3]
        try:
            mod = importlib.import_module(mod_name)
        except Exception as ex:
            report.append((mod_name, f'Import fehlgeschlagen: {type(ex).__name__}: {ex}'))
            continue
        bp = getattr(mod, 'bp', None)
        bp_name = getattr(bp, 'name', None)
        if bp_name and bp_name in flask_app.blueprints:
            report.append((mod_name, f'bereits registriert ({bp_name})'))
            continue
        try:
            mod.init_app(flask_app)
            report.append((mod_name, 'registriert' + (f' ({bp_name})' if bp_name else '')))
        except Exception as ex:
            report.append((mod_name, f'init_app fehlgeschlagen: {type(ex).__name__}: {ex}'))
    return report


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    color = (not args.no_color) and sys.stdout.isatty()
    t0 = time.time()

    db_path, data_root, copied = prepare_environment(args)
    premigrated = premigrate_copy(db_path)

    sys.path.insert(0, PROJECT)
    os.chdir(PROJECT)
    import logging
    logging.disable(logging.INFO)   # das Startgeplapper von app.py unterdrücken (Warnungen bleiben)
    try:
        import app as appmod
    except Exception as ex:
        print(f'app.py nicht importierbar: {type(ex).__name__}: {ex}', file=sys.stderr)
        return 1
    appmod.app.config['TESTING'] = True

    bp_report = register_all_blueprints(appmod)
    if 'selftest' not in appmod.app.blueprints:
        print('selftest_bp konnte nicht registriert werden:', file=sys.stderr)
        for name, state in bp_report:
            print(f'  {name}: {state}', file=sys.stderr)
        return 1

    client = appmod.app.test_client()
    with client.session_transaction() as s:
        s['logged_in'] = True
        s['username'] = 'selftest-cli'
    url = '/api/selftest/quick' if args.quick else '/api/selftest'
    resp = client.get(url)
    if resp.status_code != 200:
        print(f'{url} → HTTP {resp.status_code}: {resp.get_data(as_text=True)[:400]}', file=sys.stderr)
        return 1
    result = resp.get_json()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_color(color, '1', f'MemeOS-Selbsttest ({"quick" if args.quick else "voll"})'))
        print(f'  DB-Kopie:  {db_path}')
        print(f'  Datenpfad: {data_root}' + (f'  ({copied} Dateien übernommen)' if copied else ''))
        print('  Module:    ' + ', '.join(f'{n} → {s}' for n, s in bp_report))
        if premigrated:
            print('  Kopie vorab migriert (Spalten aus models.py, die app.py noch nicht per ALTER TABLE anlegt): '
                  + ', '.join(premigrated))
        print()
        checks = sorted(result.get('checks', []),
                        key=lambda c: (c['ok'], SEVERITY_ORDER.get(c.get('severity'), 9), c['name']))
        for c in checks:
            if c['ok']:
                mark = _color(color, '32', '✓')
            elif c.get('severity') == 'crit':
                mark = _color(color, '31', '✕')
            elif c.get('severity') == 'warn':
                mark = _color(color, '33', '✕')
            else:
                mark = _color(color, '36', '○')
            sev = f'[{c.get("severity", "?"):4}]'
            print(f'  {mark} {sev} {c["name"]}')
            detail = (c.get('detail') or '').strip()
            if detail:
                for i, line in enumerate(_wrap(detail, 100)):
                    print('             ' + line)
        s = result.get('summary', {})
        print()
        verdict = _color(color, '32', 'OK') if result.get('ok') else _color(color, '31', 'FEHLER')
        print(f'  Ergebnis: {verdict} – {s.get("passed", 0)}/{s.get("total", 0)} bestanden, '
              f'{s.get("crit", 0)} kritisch, {s.get("warn", 0)} Warnungen, {s.get("info", 0)} Hinweise '
              f'({result.get("duration_ms", 0)} ms Checks, {int((time.time() - t0) * 1000)} ms gesamt)')
    return 0 if result.get('ok') else 1


def _wrap(text, width):
    words = []
    for w in text.split(' '):
        if w == '…' and words:
            words[-1] += ' …'      # Auslassungspunkte nie allein auf eine Zeile
        else:
            words.append(w)
    lines, cur = [], ''
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f'{cur} {w}'.strip()
    if cur:
        lines.append(cur)
    return lines


if __name__ == '__main__':
    sys.exit(main())
