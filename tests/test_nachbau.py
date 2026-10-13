# -*- coding: utf-8 -*-
"""Tests für "Meme nachbauen" v2: Rollen, Stil-Modi, Bildbereiche, Sitzungen.

Läuft ohne Netz, ohne Anthropic-Key und ohne Datenbankdatei: die KI-Antwort kommt über
MEMEOS_VISION_FAKE aus einer im Test erzeugten JSON-Datei, das Meme ist ein synthetisches
Bild (Pillow), die Datenbank liegt im Speicher, der Datenpfad in einem Wegwerf-Ordner.

    python3 -m pytest tests/test_nachbau.py
"""
import io
import os
import re
import sys
import json
import base64
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Vor jedem Import: keine Schlüssel, kein Netz, keine Hintergrunddienste, Datenbank im
# Speicher, Datenpfad in einem Wegwerf-Ordner (nichts landet in ./instance).
os.environ.update({k: '' for k in (
    'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
    'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
    'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN', 'MEMEOS_VISION_FAKE')})
_DATA_ROOT = tempfile.mkdtemp(prefix='memeos_test_nachbau_')
os.environ.update({'MEMEOS_SCHEDULER': '0', 'MEMEOS_WORKERS': '0', 'MEMEOS_OFFLINE': '1',
                   'DATABASE_URL': 'sqlite://', 'MEMEOS_DATA_ROOT': _DATA_ROOT})

from PIL import Image, ImageDraw          # noqa: E402

import memeos_render as R                 # noqa: E402
import models as M                        # noqa: E402
import vision_import_bp as V              # noqa: E402

try:
    import app as A                       # noqa: E402  (legt Seeds an: 20 Städte)
except Exception as _ex:                  # pragma: no cover – ohne App-Umgebung
    A = None
    _grund = str(_ex)


W, H = 1000, 1200

# Rohe KI-Antwort, wie Claude sie liefern würde (geht durch normalize_analysis).
ROH_ANALYSE = {
    'text_blocks': [
        {'text': 'POV: DU WARTEST AUF DEN BUS', 'box': {'x': 50, 'y': 30, 'w': 900, 'h': 80},
         'city_specific': False, 'suggested_var': None,
         'style': {'color': '#FFFFFF', 'uppercase': True, 'align': 'center', 'weight': 'bold'},
         'kontur': True, 'schatten': False, 'kasten': {'vorhanden': False, 'farbe': None},
         'schriftart': 'impact', 'groesse_rel': 0.06, 'reason': 'Formatzeile'},
        {'text': 'IN DARMSTADT', 'box': {'x': 50, 'y': 120, 'w': 900, 'h': 70},
         'city_specific': True, 'suggested_var': 'city_name',
         'style': {'color': '#FFE01B', 'uppercase': True, 'align': 'center', 'weight': 'bold'},
         'kontur': True, 'schatten': True, 'kasten': {'vorhanden': False, 'farbe': None},
         'schriftart': 'impact', 'groesse_rel': 0.05, 'reason': 'Stadtname'},
        {'text': 'Hauptbahnhof Südseite', 'box': {'x': 100, 'y': 760, 'w': 800, 'h': 60},
         'city_specific': True, 'suggested_var': 'problem_place',
         'style': {'color': '#111111', 'uppercase': False, 'align': 'center', 'weight': 'regular'},
         'kontur': False, 'schatten': False, 'kasten': {'vorhanden': True, 'farbe': '#FFFFFF'},
         'schriftart': 'sans', 'groesse_rel': 0.04, 'reason': 'Ort'},
        {'text': 'Und dann kommt er doch nicht', 'box': {'x': 60, 'y': 850, 'w': 880, 'h': 90},
         'city_specific': False, 'suggested_var': None,
         'style': {'color': '#FFFFFF', 'uppercase': False, 'align': 'center', 'weight': 'bold'},
         'kontur': True, 'schatten': False, 'kasten': {'vorhanden': False, 'farbe': None},
         'schriftart': 'serif', 'groesse_rel': 0.07, 'reason': 'Pointe'},
        {'text': 'Quelle: Stadtverwaltung 🚌😂', 'box': {'x': 50, 'y': 950, 'w': 400, 'h': 25},
         'city_specific': False, 'suggested_var': None,
         'style': {'color': '#DDDDDD', 'uppercase': False, 'align': 'left', 'weight': 'regular'},
         'kontur': False, 'schatten': False, 'kasten': {'vorhanden': False, 'farbe': None},
         'schriftart': 'sans', 'groesse_rel': 0.02, 'reason': 'Kleingedrucktes'},
        {'text': '@darmstadt_memes', 'box': {'x': 600, 'y': 950, 'w': 350, 'h': 25},
         'city_specific': False, 'suggested_var': None,
         'style': {'color': '#FFFFFF', 'uppercase': False, 'align': 'right', 'weight': 'regular'},
         'kontur': False, 'schatten': False, 'kasten': {'vorhanden': False, 'farbe': None},
         'schriftart': 'sans', 'groesse_rel': 0.02, 'reason': 'Seitenname'},
    ],
    'background_kind': 'photo',
    'format': {'name': None, 'sicherheit': 0},
    'aufbau': {
        'beschreibung': 'Foto oben, Fläche unten',
        'regionen': [
            {'art': 'foto', 'box': {'x': 0, 'y': 200, 'w': 1000, 'h': 500},
             'beschreibung': 'Skyline bei Nacht 🌃', 'schluessel': 'Skyline', 'stadtbezug': True},
            {'art': 'comic', 'box': {'x': 0, 'y': 700, 'w': 450, 'h': 450},
             'beschreibung': 'Meme-Figur', 'schluessel': 'figur', 'stadtbezug': False},
            {'art': 'foto', 'box': {'x': 900, 'y': 0, 'w': 100, 'h': 100},
             'beschreibung': 'winziges Logo', 'schluessel': 'logo', 'stadtbezug': False},
            {'art': 'flaeche', 'box': {'x': 0, 'y': 0, 'w': 1000, 'h': 200},
             'beschreibung': 'schwarzer Balken'},
        ],
    },
    'palette': ['#000000', '#FFE01B', '#FFFFFF'],
    'titel_vorschlag': 'POV Bus warten 🚌',
    'kategorie_vorschlag': 'opnv',
}


def meme_bild(w=W, h=H):
    """Synthetisches Meme: dunkler Balken oben, bunter Verlauf (Foto-Ersatz) in der Mitte,
    helle Fläche unten – und ein paar Schriftzüge, damit das Freiräumen etwas zu tun hat."""
    im = Image.new('RGB', (w, h), (240, 240, 240))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w, int(h * 0.2)), fill=(20, 20, 20))
    for y in range(int(h * 0.2), int(h * 0.7)):
        t = (y - int(h * 0.2)) / float(int(h * 0.5))
        d.line((0, y, w, y), fill=(int(30 + 120 * t), int(60 + 80 * t), int(120 + 100 * t)))
    d.rectangle((0, int(h * 0.7), int(w * 0.45), h), fill=(200, 230, 255))
    d.text((60, 40), 'POV: DU WARTEST AUF DEN BUS', fill=(255, 255, 255))
    d.text((60, 130), 'IN DARMSTADT', fill=(255, 224, 27))
    d.text((120, 770), 'Hauptbahnhof Suedseite', fill=(17, 17, 17))
    return im


def png_bytes(im):
    buf = io.BytesIO()
    im.save(buf, format='PNG')
    return buf.getvalue()


def analyse():
    return V.normalize_analysis(json.loads(json.dumps(ROH_ANALYSE)), None,
                                kategorien=[(k, l) for k, l, _e, _g in M.TEMPLATE_CATEGORIES])


def hintergrund(weg='freigeraeumt'):
    return {'weg': weg, 'hinweis': 'Test', 'treffer': None,
            'zaehler': {'kasten': 0, 'flaeche': 0, 'inpaint': 0}, 'warnungen': [],
            'dateiname': 'hg.png'}


def texte(config):
    return [e for e in config['elements'] if e.get('type') == 'text']


def bilder(config):
    return [e for e in config['elements'] if e.get('type') == 'image']


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Rollen-Heuristik
# ═══════════════════════════════════════════════════════════════════════════════

def zeile(text='Zeile', y=100, h=60, rel=0.05, weight='bold', kasten=False,
          city_specific=False, var=None, index=0):
    return {'index': index, 'text': text, 'box': {'x': 50, 'y': y, 'w': 900, 'h': h},
            'city_specific': city_specific, 'suggested_var': var,
            'style': {'weight': weight}, 'kasten': {'vorhanden': kasten, 'farbe': '#FFFFFF'},
            'groesse_rel': rel}


class TestRollenHeuristik(unittest.TestCase):

    def test_1_handle_wird_wasserzeichen(self):
        blk = zeile('@stadt_memes', rel=0.09)
        self.assertEqual(V.rolle_fuer_zeile(blk, [blk], H), 'wasserzeichen')
        blk2 = zeile('irgendwas', var='city_handle', city_specific=True, rel=0.09)
        self.assertEqual(V.rolle_fuer_zeile(blk2, [blk2], H), 'wasserzeichen')

    def test_2_stadtname_wird_ort(self):
        blk = zeile('IN DARMSTADT', city_specific=True, var='city_name', rel=0.09)
        self.assertEqual(V.rolle_fuer_zeile(blk, [blk], H), 'ort')

    def test_3_kleine_zeile_wird_klein(self):
        blk = zeile('Quelle: xy', rel=0.02)
        gross = zeile('GROSS', rel=0.08, index=1)
        self.assertEqual(V.rolle_fuer_zeile(blk, [blk, gross], H), 'klein')
        # ohne groesse_rel zählt die Boxhöhe (25 Promille = 0.025 < 0.028)
        blk2 = zeile('Quelle: xy', rel=None, h=25)
        self.assertEqual(V.rolle_fuer_zeile(blk2, [blk2, gross], H), 'klein')

    def test_4_groesste_zeile_pointe_unten_ueberschrift_oben(self):
        oben = zeile('OBEN', y=50, h=80, rel=0.08, index=0)
        unten = zeile('unten', y=800, h=60, rel=0.05, index=1)
        self.assertEqual(V.rolle_fuer_zeile(oben, [oben, unten], H), 'ueberschrift')
        unten_gross = zeile('UNTEN GROSS', y=800, h=100, rel=0.09, index=1)
        self.assertEqual(V.rolle_fuer_zeile(unten_gross, [oben, unten_gross], H), 'pointe')

    def test_5_kasten_wird_ueberschrift(self):
        gross = zeile('GROSS', rel=0.09, index=0)
        blk = zeile('Mit Kasten', rel=0.05, kasten=True, index=1)
        self.assertEqual(V.rolle_fuer_zeile(blk, [gross, blk], H), 'ueberschrift')

    def test_6_normales_gewicht_wird_unterzeile(self):
        gross = zeile('GROSS', rel=0.09, index=0)
        blk = zeile('erklärt etwas', rel=0.04, weight='regular', index=1)
        self.assertEqual(V.rolle_fuer_zeile(blk, [gross, blk], H), 'unterzeile')

    def test_7_rest_nach_hintergrund(self):
        gross = zeile('GROSS', rel=0.09, index=0)
        blk = zeile('WEITERE ZEILE', rel=0.05, index=1)
        self.assertEqual(V.rolle_fuer_zeile(blk, [gross, blk], H, bg_art='photo'), 'memezeile')
        self.assertEqual(V.rolle_fuer_zeile(blk, [gross, blk], H, bg_art='mixed'), 'memezeile')
        self.assertEqual(V.rolle_fuer_zeile(blk, [gross, blk], H, bg_art='flat'), 'ueberschrift')


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Emoji, Schriften, Vorschläge
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmoji(unittest.TestCase):

    def test_entfernt_emoji_und_meldet_es(self):
        text, weg = V.emoji_entfernen('Bus kommt 🚌😂 nie')
        self.assertTrue(weg)
        self.assertEqual(text, 'Bus kommt nie')

    def test_laesst_normalen_text_in_ruhe(self):
        for s in ('Hauptbahnhof Südseite', 'Größe: 5 €', 'Straße & Platz (Nr. 3)', '',
                  'Heute 30 °C', 'Stadt © 2026', '100 % Sonne'):
            with self.subTest(s=s):
                text, weg = V.emoji_entfernen(s)
                self.assertFalse(weg)
                self.assertEqual(text, s)

    def test_variantenselektor_und_joiner(self):
        text, weg = V.emoji_entfernen('Hallo ❤️ Welt 👨‍👩‍👧')
        self.assertTrue(weg)
        self.assertEqual(text, 'Hallo Welt')

    def test_analyse_markiert_zeile_und_config_warnt(self):
        a = analyse()
        quelle = [b for b in a['text_blocks'] if b['text'].startswith('Quelle')][0]
        self.assertTrue(quelle['emoji_entfernt'])
        self.assertEqual(quelle['text'], 'Quelle: Stadtverwaltung')
        warn = []
        V.nachbau_config(a, hintergrund(), W, H, warn)
        self.assertTrue(any('Emoji entfernt' in w for w in warn), warn)
        self.assertEqual(a['titel_vorschlag'], 'POV Bus warten')
        self.assertEqual(a['aufbau']['regionen'][0]['beschreibung'], 'Skyline bei Nacht')


class TestSchriften(unittest.TestCase):

    def test_abbildung_mit_voller_bibliothek(self):
        alle = {'anton', 'bold', 'bebas_neue', 'archivo_black', 'poppins_black',
                'poppins_semibold', 'lilita_one', 'bangers', 'permanent_marker',
                'kalam_bold', 'alfa_slab_one'}
        faelle = {('impact', 'bold'): 'anton', ('condensed', 'bold'): 'bebas_neue',
                  ('sans_fett', 'bold'): 'archivo_black', ('sans', 'bold'): 'poppins_black',
                  ('sans', 'regular'): 'poppins_semibold', ('rounded', 'bold'): 'lilita_one',
                  ('comic', 'bold'): 'bangers', ('handschrift', 'bold'): 'permanent_marker',
                  ('serif', 'bold'): 'alfa_slab_one', ('unbekannt', 'bold'): 'anton',
                  ('unbekannt', 'regular'): 'poppins_semibold'}
        for (art, weight), erwartet in faelle.items():
            with self.subTest(art=art, weight=weight):
                key, ersatz = V.schrift_key_fuer(art, weight, alle)
                self.assertEqual(key, erwartet)
                self.assertFalse(ersatz)

    def test_zweitwahl_handschrift(self):
        key, ersatz = V.schrift_key_fuer('handschrift', 'bold', {'anton', 'bold', 'kalam_bold'})
        self.assertEqual(key, 'kalam_bold')
        self.assertFalse(ersatz)

    def test_rueckfall_auf_anton_und_bold_wenn_key_fehlt(self):
        nur_alt = {'anton', 'bold', 'arial_rounded_bold'}
        key, ersatz = V.schrift_key_fuer('condensed', 'bold', nur_alt)
        self.assertEqual((key, ersatz), ('anton', True))
        key, ersatz = V.schrift_key_fuer('sans', 'regular', nur_alt)
        self.assertEqual((key, ersatz), ('bold', True))
        key, ersatz = V.schrift_key_fuer('serif', 'bold', set())
        self.assertEqual((key, ersatz), ('anton', True))
        # impact will anton und bekommt anton: kein Ersatz
        key, ersatz = V.schrift_key_fuer('impact', 'bold', nur_alt)
        self.assertEqual((key, ersatz), ('anton', False))

    def test_echte_bibliothek_liefert_nur_vorhandene_keys(self):
        keys = {f['key'] for f in R.list_fonts()}
        for art in V.SCHRIFT_ARTEN:
            for weight in ('bold', 'regular'):
                key, _e = V.schrift_key_fuer(art, weight)
                self.assertIn(key, keys, f'{art}/{weight} -> {key}')

    def test_warnung_nur_bei_ersatz(self):
        warn = []
        blk = {'schriftart': 'impact', 'style': {'weight': 'bold'}}
        V._schrift_fuer(blk, warn, 1, {'anton', 'bold'})
        self.assertEqual(warn, [])
        blk = {'schriftart': 'serif', 'style': {'weight': 'bold'}}
        self.assertEqual(V._schrift_fuer(blk, warn, 2, {'anton', 'bold'}), 'anton')
        self.assertEqual(len(warn), 1)
        self.assertIn('Zeile 2', warn[0])
        self.assertIn('Serifenschrift', warn[0])


class TestVorschlaege(unittest.TestCase):

    def test_titel_und_kategorie_der_ki(self):
        v = V.vorschlaege_fuer(analyse())
        self.assertEqual(v, {'name': 'POV Bus warten', 'kategorie': 'opnv'})

    def test_rueckfaelle(self):
        a = analyse()
        a['titel_vorschlag'] = ''
        a['kategorie_vorschlag'] = ''
        a['format'] = {'name': 'Drake Hotline Bling', 'sicherheit': 0.9}
        self.assertEqual(V.vorschlaege_fuer(a)['name'], 'Drake Hotline Bling')
        a['format'] = {'name': None, 'sicherheit': 0}
        v = V.vorschlaege_fuer(a)
        self.assertEqual(v['name'], 'POV: DU WARTEST AUF DEN BUS')
        self.assertEqual(v['kategorie'], '')
        self.assertEqual(V.vorschlaege_fuer({})['name'], '')

    def test_unbekannte_kategorie_faellt_weg(self):
        roh = json.loads(json.dumps(ROH_ANALYSE))
        roh['kategorie_vorschlag'] = 'gibt_es_nicht'
        a = V.normalize_analysis(roh, None, kategorien=[('opnv', 'ÖPNV')])
        self.assertEqual(a['kategorie_vorschlag'], '')

    def test_schluessel_bereinigen(self):
        self.assertEqual(V.schluessel_bereinigen('bild_Skyline'), 'skyline')
        self.assertEqual(V.schluessel_bereinigen('Straßen-Bahn'), 'strassen_bahn')
        self.assertEqual(V.schluessel_bereinigen('123'), '')
        self.assertEqual(V.schluessel_bereinigen(''), '')


# ═══════════════════════════════════════════════════════════════════════════════
# 3. nachbau_config – Stil-Modi, Bildbereiche, Reihenfolge
# ═══════════════════════════════════════════════════════════════════════════════

HEX = re.compile(r'^#[0-9A-Fa-f]{6}')


class TestNachbauConfigStadt(unittest.TestCase):

    def setUp(self):
        self.warn = []
        self.cfg = V.nachbau_config(analyse(), hintergrund('freigeraeumt'), W, H, self.warn,
                                    stil_modus='stadt')

    def test_jede_zeile_hat_rolle_und_keine_hexfarbe(self):
        for el in texte(self.cfg):
            with self.subTest(id=el['id']):
                self.assertIn(el.get('role'), R.ROLLEN)
                self.assertNotEqual(el.get('role'), 'frei')
                for feld in ('font', 'color', 'stroke', 'stroke_width', 'shadow', 'box',
                             'uppercase'):
                    self.assertNotIn(feld, el)
                for feld in ('id', 'x', 'y', 'width', 'height', 'align', 'valign', 'fit',
                             'max_size', 'min_size'):
                    self.assertIn(feld, el)
        self.assertEqual(V._config_fehler(self.cfg), [])

    def test_rollen_der_heuristik(self):
        rollen = {el['id']: el['role'] for el in texte(self.cfg)}
        self.assertEqual(rollen['text_1'], 'ort')             # IN DARMSTADT
        self.assertEqual(rollen['text_3'], 'pointe')          # größte Zeile, untere Hälfte
        self.assertEqual(rollen['text_4'], 'klein')           # Quelle
        self.assertEqual(rollen['text_5'], 'wasserzeichen')   # @darmstadt_memes
        self.assertEqual(rollen['text_2'], 'ueberschrift')    # Kasten
        self.assertEqual(rollen['text_0'], 'memezeile')       # Foto-Hintergrund

    def test_handle_zeile_wird_wasserzeichen_mit_city_handle(self):
        wz = [el for el in texte(self.cfg) if el['role'] == 'wasserzeichen']
        self.assertEqual(len(wz), 1)
        self.assertEqual(wz[0]['var'], 'city_handle')
        self.assertNotIn('fallback', wz[0])
        self.assertNotIn('text', wz[0])
        self.assertEqual(wz[0]['id'], 'text_5')

    def test_wasserzeichen_wird_angehaengt_wenn_keine_handle_zeile(self):
        a = analyse()
        a['text_blocks'] = [b for b in a['text_blocks'] if not b['text'].startswith('@')]
        cfg = V.nachbau_config(a, hintergrund(), W, H, [], stil_modus='stadt')
        letztes = cfg['elements'][-1]
        self.assertEqual(letztes['id'], 'wasserzeichen')
        self.assertEqual(letztes['role'], 'wasserzeichen')
        self.assertEqual(letztes['var'], 'city_handle')
        self.assertEqual(letztes['align'], 'right')
        self.assertEqual(letztes['width'], round(0.44 * W))
        self.assertEqual(V._config_fehler(cfg), [])

    def test_reihenfolge_cover_bild_text(self):
        bereiche = V.bildbereiche_aus_analyse(analyse(), W, H)
        for b in bereiche:
            b['datei'] = f'tok_bild_{b["i"]}.png'
        bereiche[0]['modus'] = 'stadt'
        bereiche[1]['modus'] = 'fest'
        cfg = V.nachbau_config(analyse(), hintergrund('freigeraeumt'), W, H, [],
                               stil_modus='stadt', bildbereiche=bereiche)
        typen = [el['type'] for el in cfg['elements']]
        erster_text = typen.index('text')
        letztes_cover = max(i for i, t in enumerate(typen) if t == 'cover')
        erstes_bild = typen.index('image')
        letztes_bild = max(i for i, t in enumerate(typen) if t == 'image')
        self.assertLess(letztes_cover, erstes_bild)
        self.assertLess(letztes_bild, erster_text)
        self.assertEqual(V._config_fehler(cfg), [])

    def test_required_vars_ohne_bild(self):
        bereiche = V.bildbereiche_aus_analyse(analyse(), W, H)
        bereiche[0]['modus'] = 'stadt'
        bereiche[0]['datei'] = 'tok_bild_0.png'
        cfg = V.nachbau_config(analyse(), hintergrund(), W, H, [], bildbereiche=bereiche)
        self.assertTrue(any(el.get('var') == 'bild_skyline' for el in bilder(cfg)))
        vars_ = V.nachbau_variablen(cfg)
        self.assertNotIn('bild_skyline', vars_)
        self.assertEqual(vars_, ['city_name', 'problem_place', 'city_handle'])

    def test_explizite_rollen_schlagen_heuristik(self):
        cfg = V.nachbau_config(analyse(), hintergrund(), W, H, [], stil_modus='stadt',
                               rollen={'text_0': 'pointe', 'text_3': 'frei', 'text_2': 'quatsch'})
        rollen = {el['id']: el['role'] for el in texte(cfg)}
        self.assertEqual(rollen['text_0'], 'pointe')
        self.assertEqual(rollen['text_3'], 'frei')
        self.assertEqual(rollen['text_2'], 'ueberschrift')     # unbekannte Rolle: Heuristik
        frei = [el for el in texte(cfg) if el['id'] == 'text_3'][0]
        self.assertTrue(HEX.match(frei['color']))
        self.assertIn('font', frei)

    def test_kasten_nur_als_cover_beim_freiraeumen(self):
        cover = [el for el in self.cfg['elements'] if el['type'] == 'cover' and el['id'] == 'cover_2']
        self.assertEqual(len(cover), 1)
        self.assertEqual(cover[0]['fill'], '#FFFFFF')
        cfg = V.nachbau_config(analyse(), hintergrund('katalog'), W, H, [], stil_modus='stadt')
        self.assertEqual([el for el in cfg['elements'] if el['type'] == 'cover'], [])
        self.assertNotIn('box', [el for el in texte(cfg) if el['id'] == 'text_2'][0])


class TestNachbauConfigOriginal(unittest.TestCase):

    def test_role_frei_und_hex_bleibt(self):
        warn = []
        cfg = V.nachbau_config(analyse(), hintergrund('katalog'), W, H, warn, stil_modus='original')
        for el in texte(cfg):
            if el['id'] == 'wasserzeichen' or el.get('var') == 'city_handle':
                self.assertEqual(el['role'], 'wasserzeichen')
                continue
            with self.subTest(id=el['id']):
                self.assertEqual(el['role'], 'frei')
                self.assertTrue(HEX.match(el['color']), el)
                self.assertIn('font', el)
        ort = [el for el in texte(cfg) if el['id'] == 'text_1'][0]
        self.assertEqual(ort['color'], '#FFE01B')
        self.assertEqual(ort['stroke'], '#000000')
        self.assertIn('shadow', ort)
        kasten = [el for el in texte(cfg) if el['id'] == 'text_2'][0]
        self.assertEqual(kasten['box']['fill'], '#FFFFFF')
        self.assertEqual(V._config_fehler(cfg), [])
        # Serifenzeile: hat die Bibliothek die Slab-Schrift, bekommt die Zeile sie ohne
        # Ersatzmeldung; fehlt sie (aeltere Bibliothek), wird der Ersatz gemeldet.
        pointe = [el for el in texte(cfg) if el['id'] == 'text_3'][0]
        ersatz_gemeldet = any('Zeile 4' in w and 'ersetzt' in w for w in warn)
        if 'alfa_slab_one' in V._verfuegbare_schriften():
            self.assertEqual(pointe['font'], 'alfa_slab_one')
            self.assertFalse(ersatz_gemeldet, warn)
        else:
            self.assertEqual(pointe['font'], 'anton')
            self.assertTrue(ersatz_gemeldet, warn)

    def test_wasserzeichen_auch_im_original(self):
        cfg = V.nachbau_config(analyse(), hintergrund(), W, H, [], stil_modus='original')
        wz = [el for el in texte(cfg) if el.get('role') == 'wasserzeichen']
        self.assertEqual(len(wz), 1)
        self.assertEqual(wz[0]['var'], 'city_handle')


class TestBildbereiche(unittest.TestCase):

    def test_auswahl_und_vorgaben(self):
        bereiche = V.bildbereiche_aus_analyse(analyse(), W, H)
        # das winzige Logo (1 %) fällt weg, die Fläche ist keine Bildregion
        self.assertEqual([b['art'] for b in bereiche], ['foto', 'comic'])
        self.assertEqual([b['i'] for b in bereiche], [0, 1])
        skyline, figur = bereiche
        self.assertEqual(skyline['modus'], 'stadt')
        self.assertEqual(skyline['key'], 'skyline')
        self.assertTrue(skyline['stadtbezug'])
        self.assertTrue(skyline['fallback_original'])
        self.assertEqual(skyline['box_px'], {'x': 0, 'y': 240, 'w': 1000, 'h': 600})
        self.assertEqual(figur['modus'], 'hintergrund')
        self.assertEqual(figur['quelle'], 'original')
        self.assertGreater(skyline['anteil'], figur['anteil'])

    def test_drei_modi(self):
        bereiche = V.bildbereiche_aus_analyse(analyse(), W, H)
        for b in bereiche:
            b['datei'] = f'tok_bild_{b["i"]}.png'
        # hintergrund: kein Element
        bereiche[0]['modus'] = 'hintergrund'
        bereiche[1]['modus'] = 'hintergrund'
        cfg = V.nachbau_config(analyse(), hintergrund(), W, H, [], bildbereiche=bereiche)
        self.assertEqual(bilder(cfg), [])
        # stadt mit Rückfall
        bereiche[0]['modus'] = 'stadt'
        bereiche[0]['key'] = 'bild_Rathaus'
        cfg = V.nachbau_config(analyse(), hintergrund(), W, H, [], bildbereiche=bereiche)
        el = bilder(cfg)[0]
        self.assertEqual(el['id'], 'bild_rathaus_0')
        self.assertEqual(el['var'], 'bild_rathaus')
        self.assertEqual(el['fallback'], 'tok_bild_0.png')
        self.assertEqual(el['fit'], 'cover')
        self.assertEqual((el['x'], el['y'], el['width'], el['height']), (0, 240, 1000, 600))
        # stadt ohne Rückfall
        bereiche[0]['fallback_original'] = False
        cfg = V.nachbau_config(analyse(), hintergrund(), W, H, [], bildbereiche=bereiche)
        self.assertNotIn('fallback', bilder(cfg)[0])
        # fest aus dem Original
        bereiche[1]['modus'] = 'fest'
        cfg = V.nachbau_config(analyse(), hintergrund(), W, H, [], bildbereiche=bereiche)
        fest = [el for el in bilder(cfg) if el['id'] == 'bildfest_1'][0]
        self.assertEqual(fest['src'], 'tok_bild_1.png')
        self.assertEqual(fest['name'], 'Festes Bild 2')
        # fest mit eigenem Bild
        bereiche[1]['quelle'] = 'upload'
        bereiche[1]['upload_datei'] = 'eigenes.png'
        cfg = V.nachbau_config(analyse(), hintergrund(), W, H, [], bildbereiche=bereiche)
        fest = [el for el in bilder(cfg) if el['id'] == 'bildfest_1'][0]
        self.assertEqual(fest['src'], 'eigenes.png')
        self.assertEqual(V._config_fehler(cfg), [])

    def test_normalisieren_aus_request(self):
        alt = V.bildbereiche_aus_analyse(analyse(), W, H)[0]
        neu = V._bildbereich_normalisieren({'modus': 'fest', 'key': 'bild_Wappen',
                                            'fallback_original': 'false', 'quelle': 'kaputt'}, alt)
        self.assertEqual(neu['modus'], 'fest')
        self.assertEqual(neu['key'], 'wappen')
        self.assertFalse(neu['fallback_original'])
        self.assertEqual(neu['quelle'], 'original')
        self.assertEqual(alt['modus'], 'stadt')          # das Original bleibt unberührt


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Rendern: dieselbe Config, zwei Stil-Pakete, zwei verschiedene Bilder
# ═══════════════════════════════════════════════════════════════════════════════

class TestRendernZweiStile(unittest.TestCase):

    def test_zwei_pakete_zwei_bilder(self):
        cfg = V.nachbau_config(analyse(), hintergrund('katalog'), W, H, [], stil_modus='stadt')
        werte = {'city_name': 'Musterstadt', 'problem_place': 'Bahnhof', 'city_handle': '@muster'}
        boulevard = R.stil_dict(M.STIL_PAKETE['boulevard'])
        nacht = R.stil_dict(M.STIL_PAKETE['nacht'])
        a = R.render(meme_bild(), cfg, werte, brand=boulevard)
        b = R.render(meme_bild(), cfg, werte, brand=nacht)
        self.assertNotEqual(a, b)
        # und derselbe Stil zweimal ergibt dasselbe Bild (sonst sagte der Test nichts)
        self.assertEqual(a, R.render(meme_bild(), cfg, werte, brand=boulevard))

    def test_original_modus_ist_stilunabhaengig_bis_aufs_wasserzeichen(self):
        cfg = V.nachbau_config(analyse(), hintergrund('katalog'), W, H, [], stil_modus='original')
        cfg['elements'] = [el for el in cfg['elements'] if el.get('role') != 'wasserzeichen']
        werte = {'city_name': 'Musterstadt', 'problem_place': 'Bahnhof'}
        a = R.render(meme_bild(), cfg, werte, brand=R.stil_dict(M.STIL_PAKETE['boulevard']))
        b = R.render(meme_bild(), cfg, werte, brand=R.stil_dict(M.STIL_PAKETE['nacht']))
        self.assertEqual(a, b)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Flask: Probelauf mit Sitzung, Konfig, Vorschau, Anlegen, Löschen
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipIf(A is None, 'app nicht ladbar')
class TestSitzungFlask(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        assert ':memory:' in A.app.config['SQLALCHEMY_DATABASE_URI'] \
            or A.app.config['SQLALCHEMY_DATABASE_URI'] == 'sqlite://', \
            'Test würde gegen eine echte Datenbank laufen'
        cls.ctx = A.app.app_context()
        cls.ctx.push()
        M.db.create_all()
        cls.fake = os.path.join(_DATA_ROOT, 'fake_analyse.json')
        with open(cls.fake, 'w', encoding='utf-8') as fh:
            json.dump(ROH_ANALYSE, fh, ensure_ascii=False)
        os.environ['MEMEOS_VISION_FAKE'] = cls.fake
        # Städte mit verschiedenen Stil-Paketen, damit die Vorschau etwas zu wählen hat
        staedte = M.City.query.filter_by(active=True).order_by(M.City.id).all()
        assert len(staedte) >= 3, 'Seeds fehlen'
        for stadt, paket in zip(staedte[:3], ('boulevard', 'nacht', 'klar')):
            if not stadt.instagram_handle:
                stadt.instagram_handle = stadt.name.lower()
            if M.CityStyle.query.filter_by(city_id=stadt.id).first() is None:
                M.db.session.add(M.CityStyle.aus_paket(stadt.id, paket))
        M.db.session.commit()
        cls.client = A.app.test_client()
        with cls.client.session_transaction() as s:
            s['logged_in'] = True
            s['csrf_token'] = 'x'
        cls.H = {'X-CSRF-Token': 'x'}

    @classmethod
    def tearDownClass(cls):
        os.environ['MEMEOS_VISION_FAKE'] = ''
        M.db.session.remove()
        cls.ctx.pop()
        shutil.rmtree(_DATA_ROOT, ignore_errors=True)

    def probelauf(self, **felder):
        daten = {'file': (io.BytesIO(png_bytes(meme_bild())), 'fremdes_meme.png'),
                 'dry_run': '1', 'city_hint': 'Darmstadt'}
        daten.update(felder)
        return self.client.post('/api/templates/nachbau', data=daten, headers=self.H,
                                content_type='multipart/form-data')

    def test_01_login_und_csrf(self):
        fremd = A.app.test_client()
        r = fremd.post('/api/templates/nachbau', data={}, content_type='multipart/form-data')
        self.assertIn(r.status_code, (401, 403))
        with fremd.session_transaction() as s:
            s['logged_in'] = True
        r = fremd.post('/api/templates/nachbau', data={}, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 403)

    def test_02_probelauf_legt_sitzung_an(self):
        r = self.probelauf()
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertTrue(j['dry_run'])
        self.assertIsNone(j['template_id'])
        self.assertEqual(j['hint'], 'Probelauf: nichts angelegt. Uebernehmen legt die Vorlage '
                                    'ohne zweite Analyse an.')
        token = j['sitzung']
        self.assertRegex(token, r'^[A-Za-z0-9_-]{8,40}$')
        self.assertTrue(j['sitzung_bis'])
        self.assertEqual(j['stil_modus'], 'stadt')
        self.assertEqual(j['vorschlaege'], {'name': 'POV Bus warten', 'kategorie': 'opnv'})
        self.assertTrue(any(r_['key'] == 'pointe' and r_['label'] == 'Pointe' for r_ in j['rollen']))
        self.assertTrue(any(s['key'] == 'anton' for s in j['schriften']))
        self.assertIsInstance(j['stadtbild_keys'], list)
        self.assertEqual(len(j['bildbereiche']), 2)
        b0 = j['bildbereiche'][0]
        for feld in ('i', 'art', 'box', 'box_px', 'anteil', 'beschreibung', 'schluessel',
                     'stadtbezug', 'ausschnitt_b64', 'modus', 'key', 'fallback_original', 'quelle'):
            self.assertIn(feld, b0)
        self.assertNotIn('datei', b0)
        self.assertEqual(b0['modus'], 'stadt')
        self.assertEqual(b0['key'], 'skyline')
        thumb = Image.open(io.BytesIO(base64.b64decode(b0['ausschnitt_b64'])))
        self.assertLessEqual(max(thumb.size), 320)
        self.assertTrue(j['vorschau_b64'])
        self.assertTrue(j['original_b64'])
        self.assertEqual(j['analyse']['counts']['total'], 6)
        self.assertEqual(j['hintergrund']['weg'], 'freigeraeumt')
        self.assertIn('bild_skyline', [el.get('var') for el in j['config']['elements']])
        self.assertNotIn('bild_skyline', j['variables'])
        # Dateien liegen flach im Sitzungsordner, nichts in uploads
        cache = os.path.join(_DATA_ROOT, 'nachbau_cache')
        self.assertTrue(os.path.isfile(os.path.join(cache, token + '.json')))
        self.assertTrue(os.path.isfile(os.path.join(cache, token + '_hg.png')))
        self.assertTrue(os.path.isfile(os.path.join(cache, token + '_quelle.png')))
        self.assertTrue(os.path.isfile(os.path.join(cache, token + '_bild_0.png')))
        uploads = os.path.join(_DATA_ROOT, 'uploads')
        self.assertFalse(any(n.startswith('nachbau_') for n in os.listdir(uploads))
                         if os.path.isdir(uploads) else False)
        # der Sitzungsordner ist beim Renderer angemeldet
        self.assertIn(os.path.realpath(cache), [os.path.realpath(d) for d in R._IMAGE_DIRS])
        self.__class__.token = token
        self.__class__.config = j['config']

    def test_03_konfig_wechsel(self):
        token = self.__class__.token
        # Stil-Modus auf original: Hexfarben kommen zurück
        r = self.client.post(f'/api/templates/nachbau/sitzung/{token}/konfig',
                             json={'stil_modus': 'original'}, headers=self.H)
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual(j['stil_modus'], 'original')
        frei = [el for el in texte(j['config']) if el['role'] == 'frei']
        self.assertTrue(frei)
        self.assertTrue(all(HEX.match(el['color']) for el in frei))
        # zurück auf stadt, Bildbereich 1 fest, Bereich 0 mit neuem Schlüssel, Rolle umgestellt
        r = self.client.post(f'/api/templates/nachbau/sitzung/{token}/konfig',
                             json={'stil_modus': 'stadt',
                                   'bildbereiche': [{'i': 0, 'modus': 'stadt', 'key': 'bild_Rathaus',
                                                     'fallback_original': True},
                                                    {'i': 1, 'modus': 'fest', 'quelle': 'original'}],
                                   'rollen': {'text_0': 'pointe'}}, headers=self.H)
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual(j['stil_modus'], 'stadt')
        ids = {el['id']: el for el in j['config']['elements']}
        self.assertIn('bild_rathaus_0', ids)
        self.assertEqual(ids['bild_rathaus_0']['fallback'], f'{token}_bild_0.png')
        self.assertIn('bildfest_1', ids)
        self.assertEqual(ids['bildfest_1']['src'], f'{token}_bild_1.png')
        self.assertEqual(ids['text_0']['role'], 'pointe')
        self.assertEqual([b['modus'] for b in j['bildbereiche']], ['stadt', 'fest'])
        self.assertEqual(j['bildbereiche'][0]['key'], 'rathaus')
        self.assertNotIn('bild_rathaus', j['variables'])
        cache = os.path.join(_DATA_ROOT, 'nachbau_cache')
        self.assertTrue(os.path.isfile(os.path.join(cache, f'{token}_bild_1.png')))
        self.__class__.config = j['config']
        # ungültiger Rumpf
        r = self.client.post(f'/api/templates/nachbau/sitzung/{token}/konfig',
                             json={'stil_modus': 'bunt'}, headers=self.H)
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f'/api/templates/nachbau/sitzung/{token}/konfig',
                             json={'bildbereiche': 'x'}, headers=self.H)
        self.assertEqual(r.status_code, 400)
        # unbekannte Sitzung
        r = self.client.post('/api/templates/nachbau/sitzung/gibtesnicht12/konfig',
                             json={}, headers=self.H)
        self.assertEqual(r.status_code, 404)

    def test_04_vorschau_in_staedten(self):
        token = self.__class__.token
        r = self.client.post(f'/api/templates/nachbau/sitzung/{token}/vorschau',
                             json={'anzahl': 3}, headers=self.H)
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual(j['bild_mime'], 'image/jpeg')
        self.assertGreaterEqual(len(j['bilder']), 1)
        self.assertLessEqual(len(j['bilder']), 3)
        pakete = []
        for b in j['bilder']:
            self.assertNotIn('fehler', b, b)
            self.assertTrue(b['b64'])
            self.assertTrue(b['city_name'])
            bild = Image.open(io.BytesIO(base64.b64decode(b['b64'])))
            self.assertLessEqual(max(bild.size), 540)
            self.assertIn('bild_rathaus', b['fehlend'])
            pakete.append(b['paket'])
        # drei verschiedene Pakete, weil es drei verschieden gestylte Städte gibt
        self.assertEqual(len(set(pakete)), len(pakete))
        # verschiedene Städte, verschiedene Bilder
        self.assertEqual(len({b['b64'] for b in j['bilder']}), len(j['bilder']))
        # gezielt eine Stadt, mit eigener Config
        stadt = M.City.query.filter_by(active=True).order_by(M.City.id).first()
        r = self.client.post(f'/api/templates/nachbau/sitzung/{token}/vorschau',
                             json={'city_ids': [stadt.id], 'config': self.__class__.config},
                             headers=self.H)
        j = r.get_json()
        self.assertEqual([b['city_id'] for b in j['bilder']], [stadt.id])
        # andere Städte würfeln liefert weiterhin Bilder
        r = self.client.post(f'/api/templates/nachbau/sitzung/{token}/vorschau',
                             json={'anzahl': 2, 'anders': True}, headers=self.H)
        self.assertEqual(len(r.get_json()['bilder']), 2)

    def test_05_anlegen(self):
        token = self.__class__.token
        vorher = M.MemeTemplate.query.count()
        eigenes = (io.BytesIO(png_bytes(Image.new('RGB', (300, 300), (255, 0, 0)))), 'eigen.png')
        r = self.client.post(
            f'/api/templates/nachbau/sitzung/{token}/anlegen',
            data={'config': json.dumps(self.__class__.config), 'name': 'Bus-Meme',
                  'category': 'opnv',
                  'bildbereiche': json.dumps([{'i': 1, 'modus': 'fest', 'quelle': 'upload'}]),
                  'bild_1': eigenes},
            headers=self.H, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 201, r.get_json())
        j = r.get_json()
        self.assertEqual(M.MemeTemplate.query.count(), vorher + 1)
        t = M.db.session.get(M.MemeTemplate, j['template_id'])
        self.assertEqual(t.name, 'Bus-Meme')
        self.assertEqual(t.category, 'opnv')
        self.assertEqual(j['studio_url'], f'/studio/{t.id}')
        self.assertIn('Stil-Modus: stadt', t.notes)
        cfg = json.loads(t.pil_config)
        self.assertEqual(cfg['meta'], {'quelle': 'nachbau', 'hintergrund_sauber': True,
                                       'stil_modus': 'stadt'})
        ids = {el['id']: el for el in cfg['elements']}
        uploads = os.path.join(_DATA_ROOT, 'uploads')
        self.assertTrue(t.preview_image.startswith('nachbau_bg_'))
        self.assertTrue(os.path.isfile(os.path.join(uploads, t.preview_image)))
        self.assertRegex(ids['bild_rathaus_0']['fallback'], r'^nachbau_bild_\d+_0\.png$')
        self.assertTrue(os.path.isfile(os.path.join(uploads, ids['bild_rathaus_0']['fallback'])))
        self.assertRegex(ids['bildfest_1']['src'], r'^nachbau_bild_\d+_1\.png$')
        hochgeladen = Image.open(os.path.join(uploads, ids['bildfest_1']['src']))
        self.assertEqual(hochgeladen.size, (300, 300))          # das eigene Bild, kein Ausschnitt
        self.assertTrue(any(n.startswith('nachbau_quelle_') for n in os.listdir(uploads)))
        self.assertFalse(any(token in str(el.get('src', '')) + str(el.get('fallback', ''))
                             for el in cfg['elements']))
        self.assertEqual(j['variables'], ['city_name', 'problem_place', 'city_handle'])
        self.assertEqual(j['elemente'], len(cfg['elements']))
        self.assertIn('pruefung', j)
        for feld in ('fehler', 'hinweise', 'befunde'):
            self.assertIn(feld, j['pruefung'])
        self.assertLessEqual(len(j['pruefung']['befunde']), 8)
        self.assertGreater(j['pruefung']['staedte'], 0)
        self.assertIn('Prüfung:', j['hint'])
        self.assertEqual(j['template']['id'], t.id)
        # Sitzung ist weg
        cache = os.path.join(_DATA_ROOT, 'nachbau_cache')
        self.assertFalse(any(n.startswith(token) for n in os.listdir(cache)))
        r = self.client.post(f'/api/templates/nachbau/sitzung/{token}/anlegen',
                             data={'name': 'nochmal'}, headers=self.H,
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 404)

    def test_06_loeschen(self):
        j = self.probelauf().get_json()
        token = j['sitzung']
        cache = os.path.join(_DATA_ROOT, 'nachbau_cache')
        self.assertTrue(os.path.isfile(os.path.join(cache, token + '.json')))
        r = self.client.delete(f'/api/templates/nachbau/sitzung/{token}', headers=self.H)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {'ok': True})
        self.assertFalse(any(n.startswith(token) for n in os.listdir(cache)))
        r = self.client.delete(f'/api/templates/nachbau/sitzung/{token}', headers=self.H)
        self.assertEqual(r.get_json(), {'ok': True})
        r = self.client.post(f'/api/templates/nachbau/sitzung/{token}/vorschau', json={},
                             headers=self.H)
        self.assertEqual(r.status_code, 404)
        # Pfad-Spielereien im Token laufen ins Leere
        r = self.client.delete('/api/templates/nachbau/sitzung/..%2F..%2Fx', headers=self.H)
        self.assertIn(r.status_code, (200, 404))

    def test_07_abgelaufene_sitzung_wird_aufgeraeumt(self):
        j = self.probelauf().get_json()
        token = j['sitzung']
        cache = os.path.join(_DATA_ROOT, 'nachbau_cache')
        alt = os.path.getmtime(os.path.join(cache, token + '.json')) - 30 * 3600
        for n in os.listdir(cache):
            if n.startswith(token):
                os.utime(os.path.join(cache, n), (alt, alt))
        V._sitzungen_aufraeumen()
        self.assertFalse(any(n.startswith(token) for n in os.listdir(cache)))
        r = self.client.post(f'/api/templates/nachbau/sitzung/{token}/konfig', json={},
                             headers=self.H)
        self.assertEqual(r.status_code, 404)

    def test_08_original_modus_im_probelauf_und_altweg(self):
        j = self.probelauf(stil_modus='original').get_json()
        self.assertEqual(j['stil_modus'], 'original')
        self.assertTrue(all(el['role'] in ('frei', 'wasserzeichen') for el in texte(j['config'])))
        self.client.delete(f'/api/templates/nachbau/sitzung/{j["sitzung"]}', headers=self.H)
        # Altweg: dry_run=0 ohne Sitzung legt sofort an
        vorher = M.MemeTemplate.query.count()
        r = self.probelauf(dry_run='0', name='Altweg')
        self.assertEqual(r.status_code, 201, r.get_json())
        j = r.get_json()
        self.assertIsNone(j['sitzung'])
        self.assertEqual(M.MemeTemplate.query.count(), vorher + 1)
        t = M.db.session.get(M.MemeTemplate, j['template_id'])
        self.assertEqual(json.loads(t.pil_config)['meta']['hintergrund_sauber'], True)
        self.assertTrue(any(el.get('role') for el in texte(j['config'])))

    def test_09_help_nennt_sitzung(self):
        r = self.client.get('/api/templates/import/image/help')
        j = r.get_json()
        self.assertTrue(j['nachbau']['sitzung'])
        self.assertEqual(j['nachbau']['stil_modi'], ['stadt', 'original'])
        self.assertIn('stil_modus', j['nachbau']['felder'])
        self.assertIn('Schriften werden auf die vorhandene Bibliothek abgebildet',
                      ' '.join(V.GRENZEN))

    def test_10_bild_import_markiert_unsauberen_hintergrund(self):
        r = self.client.post('/api/templates/import/image',
                             data={'file': (io.BytesIO(png_bytes(meme_bild())), 'import.png'),
                                   'analyze': '0', 'name': 'Roh importiert'},
                             headers=self.H, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 201, r.get_json())
        t = M.db.session.get(M.MemeTemplate, r.get_json()['id'])
        self.assertEqual(json.loads(t.pil_config)['meta']['hintergrund_sauber'], False)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Bibliothek: unsaubere Vorlagen bleiben draußen
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipIf(A is None, 'app nicht ladbar')
class TestBibliothek(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import meme_katalog as K
        cls.K = K
        cls.ctx = A.app.app_context()
        cls.ctx.push()
        M.db.create_all()

    @classmethod
    def tearDownClass(cls):
        M.db.session.remove()
        cls.ctx.pop()

    def vorlage(self, name, config, description=''):
        t = M.MemeTemplate(name=name, render_type='pil', description=description,
                           pil_config=json.dumps(config), preview_image=f'{name}.png')
        M.db.session.add(t)
        M.db.session.commit()
        return t

    def test_unsaubere_vorlagen_werden_uebersprungen(self):
        K = self.K
        sauber = self.vorlage('bib_sauber', {'elements': [], 'meta': {'hintergrund_sauber': True}})
        unsauber = self.vorlage('bib_unsauber', {'elements': [], 'meta': {'hintergrund_sauber': False}})
        alt = self.vorlage('bib_alt', {'elements': []}, description='Importiert aus Bild am 01.01.2026')
        ohne = self.vorlage('bib_ohne', {'elements': []}, description='irgendwas')
        self.assertFalse(K.hintergrund_unsauber(sauber))
        self.assertTrue(K.hintergrund_unsauber(unsauber))
        self.assertTrue(K.hintergrund_unsauber(alt))
        self.assertFalse(K.hintergrund_unsauber(ohne))
        ergebnis = K.eigene_aufnehmen(_DATA_ROOT)
        self.assertGreaterEqual(ergebnis['uebersprungen'], 2)
        self.assertIn('übersprungen', ergebnis['meldung'])
        namen = {z.extern_id for z in M.MemeFormat.query.filter_by(quelle='eigen').all()}
        self.assertIn(f'template:{sauber.id}', namen)
        self.assertIn(f'template:{ohne.id}', namen)
        self.assertNotIn(f'template:{unsauber.id}', namen)
        self.assertNotIn(f'template:{alt.id}', namen)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestSatzMitStadt(unittest.TestCase):
    """Ein Satz, in dem der Stadtname nur ein Wort ist, bleibt als Satz stehen."""

    def test_satz_wird_vorlagentext(self):
        self.assertEqual(V.satz_mit_stadt('Wenn du in Darmstadt wohnst', 'Darmstadt'),
                         'Wenn du in {city_name} wohnst')
        self.assertEqual(V.satz_mit_stadt('WENN DU IN DARMSTADT WOHNST', 'darmstadt'),
                         'WENN DU IN {city_name} WOHNST')

    def test_nur_der_name_bleibt_variable(self):
        self.assertIsNone(V.satz_mit_stadt('Darmstadt', 'Darmstadt'))
        self.assertIsNone(V.satz_mit_stadt('DARMSTADT!', 'Darmstadt'))
        self.assertIsNone(V.satz_mit_stadt('„Darmstadt“', 'Darmstadt'))

    def test_gebeugt_oder_fremd_bleibt_variable(self):
        self.assertIsNone(V.satz_mit_stadt('Typisch Darmstädter', 'Darmstadt'))
        self.assertIsNone(V.satz_mit_stadt('Jeder Berliner kennt das', 'Berlin'))
        self.assertIsNone(V.satz_mit_stadt('Wenn du in Mainz wohnst', 'Darmstadt'))
        self.assertIsNone(V.satz_mit_stadt('Wenn du in Darmstadt wohnst', None))

    def test_nachbau_setzt_satz_als_vorlagentext(self):
        roh = json.loads(json.dumps(ROH_ANALYSE))
        roh['text_blocks'][1]['text'] = 'WENN DU IN DARMSTADT WOHNST'
        a = V.normalize_analysis(roh, 'Darmstadt')
        self.assertEqual(a.get('city_hint'), 'Darmstadt')
        for modus in ('stadt', 'original'):
            with self.subTest(modus=modus):
                cfg = V.nachbau_config(a, hintergrund('katalog'), W, H, [], stil_modus=modus)
                zeile = [el for el in texte(cfg) if el['id'] == 'text_1'][0]
                self.assertEqual(zeile.get('text'), 'WENN DU IN {city_name} WOHNST')
                self.assertNotIn('var', zeile)
                self.assertEqual(zeile.get('fallback'), 'WENN DU IN DARMSTADT WOHNST')
                self.assertNotEqual(zeile.get('role'), 'ort')
                self.assertIn('city_name', V.nachbau_variablen(cfg))
                self.assertEqual(V._config_fehler(cfg), [])

    def test_ohne_hinweis_wie_bisher(self):
        a = V.normalize_analysis(json.loads(json.dumps(ROH_ANALYSE)), None)
        cfg = V.nachbau_config(a, hintergrund('katalog'), W, H, [], stil_modus='stadt')
        zeile = [el for el in texte(cfg) if el['id'] == 'text_1'][0]
        self.assertEqual(zeile.get('var'), 'city_name')
        self.assertEqual(zeile.get('role'), 'ort')
