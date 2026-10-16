#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Den Inhalt für ein Deployment einpacken – Datenbank und Bilder.

Warum es das braucht: `instance/` steht in `.gitignore`. Auf Render wird das Verzeichnis
als leere Platte eingehängt, der Klon des Repositoriums bringt davon nichts mit. Ein
frischer Deploy hat deshalb zwanzig Städte aus dem Seed – und sonst nichts: keine Vorlagen,
keine Hintergrundbilder, keine Stadt-Handschriften, keine Beiträge. Nachgemessen an einem
echten Klon mit leerer Platte.

Dieses Werkzeug packt genau das ein, was fehlt, und sagt, wie es hinüberkommt.

    python3 tools/deploy_paket.py                  # packen (ohne fertige Renderbilder)
    python3 tools/deploy_paket.py --mit-renders    # auch die erzeugten Bilder (gross)
    python3 tools/deploy_paket.py --pruefen <tar>  # ein Paket ansehen, ohne es auszupacken

Die Datenbank wird über die sqlite3-Backup-Schnittstelle gesichert, NICHT kopiert: im
WAL-Modus steht der zuletzt geschriebene Teil noch in der -wal-Datei, eine einfache Kopie
wäre unvollständig. Genau daran ist schon einmal eine Sicherung gescheitert.
"""

import argparse
import json
import os
import sqlite3
import shutil
import tarfile
import tempfile
from datetime import datetime

# Was mitmuss, und warum.
ORDNER = {
    'uploads': 'Hintergrundbilder der Vorlagen, hochgeladene Bilder, Stadtbilder',
    'assets': 'Aufkleber und Logos',
    'fonts': 'nachträglich hochgeladene Schriften (die 17 eingebauten liegen im Repo)',
}
ORDNER_GROSS = {
    'renders': 'fertig gerenderte Beitragsbilder – nur nötig, wenn Entwürfe im Vorrat '
               'ihr Bild behalten sollen',
}
# Bewusst NICHT dabei: exports (Wegwerfware), imports (Zwischenablage),
# nachbau_cache (Sitzungen, laufen ohnehin ab).


def _wurzel():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _datenpfad():
    return os.environ.get('MEMEOS_DATA_ROOT') or os.path.join(_wurzel(), 'instance')


def db_sichern(quelle, ziel):
    """WAL-sichere Kopie. Liefert (Pfad, Bytes)."""
    q = sqlite3.connect(quelle)
    try:
        z = sqlite3.connect(ziel)
        try:
            q.backup(z)
        finally:
            z.close()
    finally:
        q.close()
    return ziel, os.path.getsize(ziel)


def _inhalt_zaehlen(db_pfad):
    """Was in dieser Datenbank steckt – für den Beipackzettel."""
    zahlen = {}
    c = sqlite3.connect(db_pfad)
    try:
        for tabelle in ('city', 'city_style', 'city_knowledge', 'meme_template',
                        'meme_post', 'app_settings'):
            try:
                zahlen[tabelle] = c.execute(f'select count(*) from "{tabelle}"').fetchone()[0]
            except sqlite3.Error:
                zahlen[tabelle] = None
        try:
            zahlen['staedte_mit_handle'] = c.execute(
                "select count(*) from city where ifnull(instagram_handle,'') != ''"
            ).fetchone()[0]
        except sqlite3.Error:
            pass
    finally:
        c.close()
    return zahlen


def packen(mit_renders=False, ziel=None):
    daten = _datenpfad()
    db = os.path.join(daten, 'memeos.db')
    if not os.path.isfile(db):
        raise SystemExit(f'Keine Datenbank unter {db}')

    ordner = dict(ORDNER)
    if mit_renders:
        ordner.update(ORDNER_GROSS)

    stempel = datetime.now().strftime('%Y%m%d_%H%M')
    ziel = ziel or os.path.join(daten, 'exports', f'memeos_deploy_{stempel}.tar.gz')
    os.makedirs(os.path.dirname(ziel), exist_ok=True)

    tmp = tempfile.mkdtemp(prefix='memeos_deploy_')
    db_kopie = os.path.join(tmp, 'memeos.db')
    _, db_bytes = db_sichern(db, db_kopie)
    zahlen = _inhalt_zaehlen(db_kopie)

    beipackzettel = {
        'erstellt': datetime.now().isoformat(timespec='seconds'),
        'datenbank_bytes': db_bytes,
        'inhalt': zahlen,
        'ordner': sorted(ordner),
        'mit_renders': bool(mit_renders),
        'hinweis': 'Im Datenpfad auspacken (auf Render: /opt/render/project/src/instance), '
                   'dann den Dienst neu starten. Das Archiv legt alles unter _paket/ ab; '
                   'die App spielt es beim Start ein, bevor sie die Datenbank öffnet.',
    }
    zettel = os.path.join(tmp, 'beipackzettel.json')
    with open(zettel, 'w', encoding='utf-8') as fh:
        json.dump(beipackzettel, fh, ensure_ascii=False, indent=2)

    dateien = 0
    with tarfile.open(ziel, 'w:gz') as tar:
        tar.add(db_kopie, arcname='_paket/memeos.db')
        tar.add(zettel, arcname='_paket/beipackzettel.json')
        for name in ordner:
            pfad = os.path.join(daten, name)
            if not os.path.isdir(pfad):
                continue
            for wurzelpfad, _unterordner, namen in os.walk(pfad):
                for f in namen:
                    if f.startswith('.'):
                        continue
                    voll = os.path.join(wurzelpfad, f)
                    rel = os.path.relpath(voll, daten)
                    tar.add(voll, arcname=os.path.join('_paket', rel))
                    dateien += 1


    shutil.rmtree(tmp, ignore_errors=True)
    return ziel, dateien, beipackzettel


def pruefen(pfad):
    with tarfile.open(pfad, 'r:gz') as tar:
        namen = tar.getnames()
        zettel = None
        # Seit dem Umstieg auf _paket/ liegt der Zettel eine Ebene tiefer; ältere
        # Pakete haben ihn noch oben, und die will man auch noch ansehen können.
        for wo in ('_paket/beipackzettel.json', 'beipackzettel.json'):
            if wo in namen:
                zettel = json.load(tar.extractfile(wo))
                break
    print(f'Paket: {pfad}')
    print(f'  {len(namen)} Einträge, {os.path.getsize(pfad) / 1_048_576:.1f} MB')
    if zettel:
        print(f'  erstellt: {zettel["erstellt"]}')
        print('  Inhalt der Datenbank:')
        for k, v in (zettel.get('inhalt') or {}).items():
            print(f'    {k:<22}{v}')
        print(f'  Ordner: {", ".join(zettel.get("ordner") or [])}')
    nach_ordner = {}
    for n in namen:
        rest = n[len('_paket/'):] if n.startswith('_paket/') else n
        if not rest:
            continue
        teil = rest.split('/')[0] if '/' in rest else '(Wurzel)'
        nach_ordner[teil] = nach_ordner.get(teil, 0) + 1
    print('  Dateien je Ordner:')
    for k, v in sorted(nach_ordner.items()):
        print(f'    {k:<22}{v}')


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--mit-renders', action='store_true',
                   help='auch instance/renders einpacken (deutlich größer)')
    p.add_argument('--ziel', help='Pfad für das Paket')
    p.add_argument('--pruefen', metavar='PAKET', help='ein vorhandenes Paket ansehen')
    args = p.parse_args()

    if args.pruefen:
        pruefen(args.pruefen)
        return

    ziel, dateien, zettel = packen(args.mit_renders, args.ziel)
    mb = os.path.getsize(ziel) / 1_048_576
    print(f'Paket geschrieben: {ziel}')
    print(f'  {dateien} Dateien aus {", ".join(zettel["ordner"])} + Datenbank, {mb:.1f} MB')
    print('  Inhalt:')
    for k, v in (zettel.get('inhalt') or {}).items():
        print(f'    {k:<22}{v}')
    if not args.mit_renders:
        print('\n  Ohne instance/renders. Beiträge im Vorrat verlieren dadurch ihr Bild;'
              '\n  mit --mit-renders kommen sie mit (dafür wird das Paket deutlich größer).')
    print("""
So kommt es auf Render hinüber (der Starter-Plan hat SSH):

  1. Erst einmal normal deployen und einmal einloggen. Dann in der Render-Oberfläche
     beim Dienst oben rechts auf „Connect" -> „SSH" und den Befehl kopieren; er sieht
     so aus:  ssh srv-xxxxxxxxxxxx@ssh.frankfurt.render.com

  2. Das Paket hinüberschieben (von hier aus, in einem eigenen Terminal):
         scp {ziel} \\
             srv-xxxxxxxxxxxx@ssh.frankfurt.render.com:/opt/render/project/src/instance/

  3. Drüben auspacken – das legt alles unter _paket/ ab und rührt nichts an:
         ssh srv-xxxxxxxxxxxx@ssh.frankfurt.render.com
         cd /opt/render/project/src/instance
         tar xzf {name}
         rm {name}

  4. In der Render-Oberfläche „Manual Deploy" -> „Restart service".
     Eingespielt wird beim Start, bevor die App die Datenbank öffnet – das ist der
     einzige gefahrlose Zeitpunkt. Die bisherige Datenbank wird nicht gelöscht, sondern
     als memeos.db.vor_paket_<Zeitstempel> beiseitegelegt.

  5. Gegenprobe: im Log muss „Deploy-Paket eingespielt" stehen, und die Vorlagenliste
     zeigt {vorlagen} Vorlagen. Danach Einstellungen -> Selbsttest.
""".format(ziel=ziel, name=os.path.basename(ziel),
           vorlagen=(zettel.get('inhalt') or {}).get('meme_template', '?')))


if __name__ == '__main__':
    main()
