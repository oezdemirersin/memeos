#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stadtdaten aus CityBot nach MemeOS übernehmen.

CityBot führt für jede Stadt, die er betreibt, Angaben, die MemeOS auch braucht: den
Instagram-Namen, die Markenfarbe, die dunkle Fassung davon und den Fußballverein. Dieses
Werkzeug liest sie dort aus und schreibt sie hierher — es erfindet nichts.

Was übernommen wird (je Stadt, die in BEIDEN Anlagen vorkommt):

    CityBot `slug`         ->  city.instagram_handle   (klein geschrieben, ohne @)
    CityBot `accent`       ->  city.accent_color       und city_style.primary
    CityBot `logo_color`   ->  city_style.secondary
    CityBot `football_team`->  city_knowledge (Kategorie 'sport', source='citybot')

Alles Weitere der Handschrift rechnet stadt_handschrift daraus aus. Städte, die CityBot
NICHT kennt, bleiben unberührt — welches Instagram-Konto zu ihnen gehört, weiß nur der
Betreiber, und geraten wäre schlimmer als leer zu lassen.

CityBots app.py wird NICHT importiert (das würde seine Anwendung hochfahren und echte
Dienste anfassen), sondern mit `ast` gelesen.

    python3 tools/stadtdaten_aus_citybot.py                     # Trockenlauf, zeigt nur an
    python3 tools/stadtdaten_aus_citybot.py --echt              # schreibt
    python3 tools/stadtdaten_aus_citybot.py --citybot <pfad>    # anderer Ort für CityBot
    python3 tools/stadtdaten_aus_citybot.py --echt --ohne-handschrift   # nur Stammdaten

Vor `--echt` wird immer eine WAL-sichere Sicherung der Datenbank angelegt; der Pfad steht
am Ende der Ausgabe. Ein einfaches Kopieren der .db-Datei wäre unvollständig.
"""

import argparse
import ast
import io
import os
import sqlite3
import sys
from datetime import datetime

CITYBOT_VORGABE = '/Users/ersinozdemir/Downloads/citybot 2'


def citybot_staedte(wurzel):
    """Das Dict CITIES aus CityBots app.py – ohne die Datei auszuführen."""
    pfad = os.path.join(wurzel, 'app.py')
    if not os.path.isfile(pfad):
        raise SystemExit(f'CityBot nicht gefunden: {pfad}\n'
                         f'Anderen Ort mit --citybot angeben.')
    baum = ast.parse(io.open(pfad, encoding='utf-8').read())
    for knoten in baum.body:
        if not isinstance(knoten, ast.Assign):
            continue
        if not any(getattr(z, 'id', '') == 'CITIES' for z in knoten.targets):
            continue
        try:
            return ast.literal_eval(knoten.value)
        except ValueError as ex:
            raise SystemExit(f'CITIES ist nicht rein literal und darum nicht lesbar: {ex}')
    raise SystemExit(f'In {pfad} gibt es kein CITIES.')


def sichern(ziel_ordner):
    """WAL-sichere Kopie der Datenbank. Liefert den Pfad."""
    import app as A
    uri = A.app.config['SQLALCHEMY_DATABASE_URI']
    if not uri.startswith('sqlite:///'):
        print(f'  Keine SQLite-Datenbank ({uri}) – keine Sicherung angelegt.')
        return None
    quelle = uri[len('sqlite:///'):]
    os.makedirs(ziel_ordner, exist_ok=True)
    ziel = os.path.join(ziel_ordner,
                        f'memeos_vor_uebernahme_{datetime.now():%Y%m%d_%H%M%S}.db')
    q = sqlite3.connect(quelle)
    try:
        z = sqlite3.connect(ziel)
        try:
            q.backup(z)          # NICHT kopieren: im WAL-Modus wäre die Kopie unvollständig
        finally:
            z.close()
    finally:
        q.close()
    return ziel


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--echt', action='store_true', help='wirklich schreiben')
    p.add_argument('--citybot', default=CITYBOT_VORGABE, help='Wurzel der CityBot-Anlage')
    p.add_argument('--ohne-handschrift', action='store_true',
                   help='nur Stammdaten, keine Handschrift ableiten')
    args = p.parse_args()

    wurzel = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, wurzel)
    os.chdir(wurzel)
    # Keine echten Dienste anfassen, keine Hintergrund-Threads starten.
    for k in ('ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
              'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
              'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN'):
        os.environ[k] = ''
    os.environ.update({'MEMEOS_SCHEDULER': '0', 'MEMEOS_WORKERS': '0', 'MEMEOS_OFFLINE': '1'})

    bot = citybot_staedte(args.citybot)
    nach_name = {str(c.get('name') or '').strip(): c for c in bot.values()}
    print(f'CityBot: {len(bot)} Städte aus {args.citybot}')

    import app as A
    import stadt_handschrift as HS
    from models import db, City, CityStyle, CityKnowledge

    with A.app.app_context():
        staedte = City.query.order_by(City.name).all()
        mit = [s for s in staedte if s.name in nach_name]
        ohne = [s for s in staedte if s.name not in nach_name]
        print(f'MemeOS:  {len(staedte)} Städte, davon {len(mit)} auch in CityBot\n')

        sicherung = None
        if args.echt:
            sicherung = sichern(os.path.join(wurzel, 'instance', 'exports'))

        # Über ALLE Städte rechnen, nicht nur über die zehn aus CityBot: eine Stadt kann
        # Nachbarin einer sein, die CityBot nicht kennt. Nur so kommt dasselbe heraus wie
        # bei POST /api/stil/ableiten – zwei Wege, die sich widersprechen, wären schlimmer
        # als einer, der fehlt.
        verteilung = HS.anmutungen_verteilen(
            [(s.id, s.name, s.lat, s.lon) for s in staedte])
        geaendert = 0
        for s in mit:
            c = nach_name[s.name]
            handle = str(c.get('slug') or '').strip().lower() or None
            marke = str(c.get('accent') or '').strip() or None
            logo = str(c.get('logo_color') or '').strip() or None
            verein = str(c.get('football_team') or '').strip() or None
            was = []
            if handle and s.instagram_handle != handle:
                was.append(f'@{handle}')
                if args.echt:
                    s.instagram_handle = handle
            if marke and (s.accent_color or '').lower() != marke.lower():
                was.append(f'Marke {marke}')
                if args.echt:
                    s.accent_color = marke
            if verein:
                vorhanden = CityKnowledge.query.filter_by(
                    city_id=s.id, category='sport', name=verein).first()
                if not vorhanden:
                    was.append(f'Verein „{verein}"')
                    if args.echt:
                        db.session.add(CityKnowledge(
                            city_id=s.id, category='sport', name=verein,
                            description='Fußballverein der Stadt (übernommen aus CityBot)',
                            confidence=90, source='citybot', active=True))
            if marke and not args.ohne_handschrift:
                werte = HS.handschrift(marke, logo, verteilung.get(s.id, 0))
                if werte:
                    zeile = CityStyle.query.filter_by(city_id=s.id).first()
                    # Nur melden, was sich tatsaechlich aendert – sonst sieht ein
                    # Trockenlauf nach Arbeit aus, die gar nicht ansteht.
                    schon_so = zeile is not None and (zeile.paket or '') == '' and all(
                        str(getattr(zeile, feld, None)) == str(werte[schluessel])
                        for feld, schluessel in (
                            ('primary', 'primary'), ('secondary', 'secondary'),
                            ('accent', 'accent'), ('dark', 'dark'), ('light', 'light'),
                            ('font_headline', 'headline'), ('font_body', 'body'),
                            ('schreibweise', 'schreibweise'), ('kasten', 'kasten'),
                            ('kontur', 'kontur'),
                            ('wasserzeichen_ecke', 'wasserzeichen_ecke')))
                    if not schon_so:
                        was.append(f"Handschrift ({werte['headline']}/{werte['body']})")
                    if args.echt and not schon_so:
                        zeile = zeile or CityStyle(city_id=s.id)
                        for feld, schluessel in (
                                ('primary', 'primary'), ('secondary', 'secondary'),
                                ('accent', 'accent'), ('dark', 'dark'), ('light', 'light'),
                                ('font_headline', 'headline'), ('font_body', 'body'),
                                ('schreibweise', 'schreibweise'), ('kasten', 'kasten'),
                                ('radius', 'radius'), ('kontur', 'kontur'),
                                ('schatten', 'schatten'),
                                ('wasserzeichen_ecke', 'wasserzeichen_ecke')):
                            setattr(zeile, feld, werte[schluessel])
                        zeile.paket = ''          # eigene Handschrift, kein fertiges Paket
                        s.brand_bg = werte['light']
                        s.brand_text_color = werte['dark']
                        db.session.add(zeile)
                        zeile = None
            if was:
                geaendert += 1
            print(f"  {s.name:<16}{', '.join(was) if was else 'unverändert'}")

        if ohne:
            print(f'\nUnberührt (kennt CityBot nicht, also kein Instagram-Name): '
                  f'{", ".join(s.name for s in ohne)}')

        if args.echt:
            db.session.commit()
            print(f'\nGeschrieben: {geaendert} Städte geändert.')
            if sicherung:
                print(f'Sicherung vorher: {sicherung}')
        else:
            db.session.rollback()
            print(f'\nTrockenlauf – nichts geschrieben ({geaendert} Städte hätten sich '
                  f'geändert). Mit --echt ausführen.')


if __name__ == '__main__':
    main()
