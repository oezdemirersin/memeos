# -*- coding: utf-8 -*-
"""Tests für die eingebaute Schriftbibliothek (fonts/ und die Tabellen in memeos_render).

Geprüft wird, dass jede eingebaute Schrift wirklich als TrueType-Datei vorliegt, mit Pillow
lädt und rendert, dass gängige Canva-/Systemnamen auf die richtigen Schlüssel zeigen und dass
der Stadt-Stil die neuen Schriften als Schriftpaar verwenden kann. Läuft ohne Netz.

    python3 -m pytest tests/test_schriften.py
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Kein Netz, keine Hintergrunddienste, keine echten Schlüssel (wie in den anderen Tests).
os.environ.update({k: '' for k in (
    'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
    'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
    'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN')})
os.environ.update({'MEMEOS_SCHEDULER': '0', 'MEMEOS_WORKERS': '0', 'MEMEOS_OFFLINE': '1',
                   'DATABASE_URL': 'sqlite://'})

from PIL import Image, ImageDraw, ImageFont   # noqa: E402

import memeos_render as R                     # noqa: E402


# Erwartete Schlüssel in Anzeige-Reihenfolge (erst die drei alten, dann die Google-Fonts,
# zuletzt die statische Oswald Bold als Ergänzung zum variablen Altbestand bold.ttf).
ALTE_KEYS = ['anton', 'bold', 'arial_rounded_bold']
NEUE_KEYS = ['bebas_neue', 'archivo_black', 'poppins_black', 'poppins_semibold', 'titan_one',
             'lilita_one', 'bangers', 'luckiest_guy', 'permanent_marker', 'kalam_bold',
             'alfa_slab_one', 'passion_one_black', 'rubik_mono_one', 'oswald_bold']
ALLE_KEYS = ALTE_KEYS + NEUE_KEYS

LABELS = {
    'bebas_neue': 'Bebas Neue', 'archivo_black': 'Archivo Black', 'poppins_black': 'Poppins Black',
    'poppins_semibold': 'Poppins SemiBold', 'titan_one': 'Titan One', 'lilita_one': 'Lilita One',
    'bangers': 'Bangers', 'luckiest_guy': 'Luckiest Guy', 'permanent_marker': 'Permanent Marker',
    'kalam_bold': 'Kalam Bold', 'alfa_slab_one': 'Alfa Slab One',
    'passion_one_black': 'Passion One Black', 'rubik_mono_one': 'Rubik Mono One',
    'oswald_bold': 'Oswald Bold',
}

PROBETEXT = 'Hallo Stadt'


def _builtin():
    """key -> Eintrag aus list_fonts() mit source builtin."""
    return {f['key']: f for f in R.list_fonts() if f['source'] == 'builtin'}


def _text_config(font, text=PROBETEXT):
    return {'canvas': {'width': 600, 'height': 200}, 'elements': [
        {'id': 't1', 'type': 'text', 'x': 20, 'y': 20, 'width': 560, 'height': 160,
         'font': font, 'max_size': 90, 'min_size': 20, 'color': '#000000',
         'align': 'center', 'valign': 'middle', 'fit': 'shrink', 'text': text},
    ]}


class TestEingebauteSchriften(unittest.TestCase):

    def test_schluessel_und_reihenfolge(self):
        """Erst die drei alten Schlüssel, dann die neuen in Vertragsreihenfolge."""
        self.assertEqual(list(R._BUILTIN_FONTS.keys()), ALLE_KEYS)

    def test_labels_menschlich(self):
        for key, label in LABELS.items():
            self.assertEqual(R._BUILTIN_FONTS[key][0], label)
        self.assertEqual(R._BUILTIN_FONTS['anton'][0], 'Anton')
        # bold.ttf ist Oswald[wght] und rendert Regular; das Label sagt das ehrlich.
        self.assertEqual(R._BUILTIN_FONTS['bold'][0], 'Oswald')

    def test_dateiname_ist_schluessel_plus_ttf(self):
        for key in NEUE_KEYS:
            self.assertEqual(R._BUILTIN_FONTS[key][1], key + '.ttf')

    def test_resolve_font_path_zeigt_auf_datei(self):
        for key, (_label, fn) in R._BUILTIN_FONTS.items():
            with self.subTest(key=key):
                p = R.resolve_font_path(key)
                self.assertTrue(os.path.isfile(p), f'{key}: {p} fehlt')
                self.assertEqual(os.path.basename(p), fn)
                self.assertTrue(p.lower().endswith('.ttf'))

    def test_dateien_sind_truetype_und_nicht_variabel(self):
        """TrueType-Kennung am Dateianfang; kein fvar-Tisch (variable Fonts rendert Pillow in der
        Standard-Instanz, meist die dünne). Einzige bekannte Ausnahme: der Altbestand bold.ttf."""
        for key in ALLE_KEYS:
            with self.subTest(key=key):
                with open(R.resolve_font_path(key), 'rb') as fh:
                    kopf = fh.read(4096)
                self.assertIn(kopf[:4], (b'\x00\x01\x00\x00', b'true', b'OTTO'),
                              f'{key}: keine TrueType-Datei')
                if key != 'bold':
                    self.assertNotIn(b'fvar', kopf, f'{key}: variabler Font')

    def test_oswald_bold_ist_dicker_als_bold(self):
        """bold.ttf ist Oswald[wght] (Standard Regular); oswald_bold.ttf ist die statische Bold.
        Die Ausgabe des Keys bold darf sich nicht ändern, deshalb ein eigener Key daneben."""
        with open(R.resolve_font_path('bold'), 'rb') as fh:
            self.assertIn(b'fvar', fh.read(4096), 'bold.ttf ist nicht mehr variabel: Test anpassen')
        def dunkle_pixel(key):
            font = ImageFont.truetype(R.resolve_font_path(key), 60)
            bild = Image.new('L', (700, 120), 255)
            ImageDraw.Draw(bild).text((10, 10), PROBETEXT, font=font, fill=0)
            return sum(1 for v in bild.getdata() if v < 128)
        self.assertEqual(ImageFont.truetype(R.resolve_font_path('oswald_bold'), 60).getname(),
                         ('Oswald', 'Bold'))
        self.assertGreater(dunkle_pixel('oswald_bold'), dunkle_pixel('bold') * 1.2)

    def test_laden_und_rendern_bei_groesse_40(self):
        """Jede Schrift lädt mit ImageFont bei 40 px und zeichnet 'Hallo Stadt' sichtbar."""
        for key in ALLE_KEYS:
            with self.subTest(key=key):
                font = ImageFont.truetype(R.resolve_font_path(key), 40)
                bild = Image.new('RGB', (600, 120), 'white')
                ImageDraw.Draw(bild).text((10, 20), PROBETEXT, font=font, fill='black')
                links, oben, rechts, unten = font.getbbox(PROBETEXT)
                self.assertGreater(rechts - links, 40, f'{key}: Text ohne Breite')
                self.assertGreater(unten - oben, 10, f'{key}: Text ohne Höhe')
                # Mindestens ein dunkler Pixel: die Schrift hat wirklich gezeichnet.
                self.assertLess(min(bild.convert('L').getdata()), 128, f'{key}: nichts gezeichnet')

    def test_load_font_liefert_echte_schrift(self):
        """load_font fällt nicht auf die Pillow-Standardschrift zurück."""
        for key in ALLE_KEYS:
            with self.subTest(key=key):
                f = R.load_font(key, 40)
                self.assertIsInstance(f, ImageFont.FreeTypeFont)
                self.assertEqual(os.path.basename(f.path), R._BUILTIN_FONTS[key][1])


class TestListFonts(unittest.TestCase):

    def test_neue_keys_als_builtin(self):
        eintraege = _builtin()
        for key in NEUE_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, eintraege)
                self.assertEqual(eintraege[key]['source'], 'builtin')
                self.assertEqual(eintraege[key]['label'], LABELS[key])
                self.assertTrue(os.path.isfile(eintraege[key]['path']))
                self.assertEqual(os.path.dirname(eintraege[key]['path']), R.FONT_BASE_DIR)

    def test_reihenfolge_der_eingebauten(self):
        keys = [f['key'] for f in R.list_fonts() if f['source'] == 'builtin']
        self.assertEqual(keys, ALLE_KEYS)

    def test_keys_eindeutig(self):
        keys = [f['key'] for f in R.list_fonts()]
        self.assertEqual(len(keys), len(set(keys)))


class TestNamensaufloesung(unittest.TestCase):

    def test_font_key_for_sechs_namen(self):
        """Canva-/Systemnamen: eigene Familie -> exact True; Ersatzschrift -> exact False."""
        erwartet = {
            'Bebas Neue':       ('bebas_neue', True),
            'Impact':           ('anton', False),          # Impact ist nicht Anton
            'Archivo Black':    ('archivo_black', True),
            'Poppins Black':    ('poppins_black', True),
            'Montserrat':       ('poppins_semibold', False),
            'Permanent Marker': ('permanent_marker', True),
        }
        for name, soll in erwartet.items():
            with self.subTest(name=name):
                self.assertEqual(R.font_key_for(name), soll)

    def test_font_key_for_weitere_namen(self):
        erwartet = {
            'Montserrat Black': ('archivo_black', False),
            'Arial Black':      ('archivo_black', False),
            'Poppins':          ('poppins_semibold', True),
            'Kalam':            ('kalam_bold', True),
            'Titan One':        ('titan_one', True),
            'Lilita One':       ('lilita_one', True),
            'Bangers':          ('bangers', True),
            'Luckiest Guy':     ('luckiest_guy', True),
            'Alfa Slab One':    ('alfa_slab_one', True),
            'Passion One':      ('passion_one_black', True),
            'Rubik Mono One':   ('rubik_mono_one', True),
            'Oswald':           ('bold', True),            # Altbestand bleibt
            'Oswald Bold':      ('oswald_bold', True),     # echte Bold, nicht mehr bold.ttf
            'Anton':            ('anton', True),
            'Arial Rounded MT Bold': ('arial_rounded_bold', True),
            'Gibt Es Nicht':    ('anton', False),          # Unbekannt -> Fallback
        }
        for name, soll in erwartet.items():
            with self.subTest(name=name):
                self.assertEqual(R.font_key_for(name), soll)

    def test_tolerante_schreibweisen(self):
        for name in ('bebas_neue', 'BEBAS NEUE', 'bebas-neue', 'bebas_neue.ttf', 'BebasNeue'):
            with self.subTest(name=name):
                self.assertEqual(R.font_key_for(name), ('bebas_neue', True))
                self.assertEqual(os.path.basename(R.resolve_font_path(name)), 'bebas_neue.ttf')

    def test_canva_map_zeigt_nur_auf_eingebaute_keys(self):
        for name, key in R.CANVA_FONT_MAP.items():
            with self.subTest(name=name):
                self.assertIn(key, R._BUILTIN_FONTS)

    def test_exact_aliases_sind_bekannte_namen(self):
        """Jeder Alias muss zu einem Namen aus CANVA_FONT_MAP oder einem Label/Key gehören."""
        bekannt = {R._norm(n) for n in R.CANVA_FONT_MAP}
        for key, (label, fn) in R._BUILTIN_FONTS.items():
            bekannt.update({R._norm(key), R._norm(label), R._norm(fn)})
        for alias in R._EXACT_ALIASES:
            with self.subTest(alias=alias):
                self.assertIn(alias, bekannt)

    def test_jedes_label_loest_auf_eigenen_key_auf(self):
        for key, (label, _fn) in R._BUILTIN_FONTS.items():
            with self.subTest(label=label):
                self.assertEqual(R.font_key_for(label), (key, True))


class TestRendern(unittest.TestCase):

    def test_render_mit_neuen_schriften(self):
        """Jede neue Schrift geht durch render(); verschiedene Schriften ergeben verschiedene Bilder."""
        bilder = {}
        for key in NEUE_KEYS:
            with self.subTest(key=key):
                png = R.render(None, _text_config(key), {}, brand=None)
                self.assertIsInstance(png, (bytes, bytearray))
                bild = Image.open(io.BytesIO(png))
                self.assertEqual(bild.size, (600, 200))
                bilder[key] = bytes(png)
        self.assertEqual(len(set(bilder.values())), len(NEUE_KEYS),
                         'zwei Schriften rendern bitgleich, Datei vermutlich doppelt')

    def test_render_ueber_canva_namen(self):
        """Ein Canva-Name im Element rendert dieselben Bytes wie der zugehörige Key."""
        ueber_name = R.render(None, _text_config('Bebas Neue'), {}, brand=None)
        ueber_key = R.render(None, _text_config('bebas_neue'), {}, brand=None)
        self.assertEqual(ueber_name, ueber_key)

    def test_stadt_stil_schriftpaar_mit_neuen_keys(self):
        """Der Stadt-Stil kann die neuen Schriften als headline/body führen (stil:-Token)."""
        stil = R.stil_dict({'headline': 'titan_one', 'body': 'poppins_semibold'})
        self.assertEqual(os.path.basename(R.resolve_font_path('stil:headline', brand=stil)),
                         'titan_one.ttf')
        self.assertEqual(os.path.basename(R.resolve_font_path('stil:body', brand=stil)),
                         'poppins_semibold.ttf')
        a = R.render(None, _text_config('stil:headline'), {}, brand=stil)
        b = R.render(None, _text_config('stil:headline'), {},
                     brand=R.stil_dict({'headline': 'rubik_mono_one', 'body': 'poppins_semibold'}))
        self.assertNotEqual(a, b, 'Stil-Schriftpaar wirkt nicht auf das Bild')


class TestLizenzen(unittest.TestCase):

    def test_lizenzdatei_nennt_jede_schrift(self):
        p = os.path.join(R.FONT_BASE_DIR, 'LIZENZEN.md')
        self.assertTrue(os.path.isfile(p), 'fonts/LIZENZEN.md fehlt')
        with open(p, encoding='utf-8') as fh:
            text = fh.read()
        for key, (_label, fn) in R._BUILTIN_FONTS.items():
            with self.subTest(key=key):
                self.assertIn(fn, text, f'{fn} nicht in LIZENZEN.md')
        self.assertIn('proprietär', text)
        self.assertIn('Apache', text)
        self.assertIn('OFL', text)


if __name__ == '__main__':
    unittest.main()
