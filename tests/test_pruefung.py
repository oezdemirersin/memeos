# -*- coding: utf-8 -*-
"""Tests für die Lesbarkeitsmessung der Vorlagen-Prüfung.

Geprüft wird, was hinter einem Text liegt: ein Textkasten, eine Fläche, das
Hintergrundbild oder die Leinwandfarbe. Läuft ohne Netz und ohne Datenbankdatei.

    python3 -m pytest tests/test_pruefung.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Vor jedem Import: keine Schlüssel, kein Netz, keine Hintergrunddienste, Datenbank im
# Speicher. Sonst zöge der Import der Anwendung die echte Umgebung mit herein.
os.environ.update({k: '' for k in (
    'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
    'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
    'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN')})
os.environ.update({'MEMEOS_SCHEDULER': '0', 'MEMEOS_WORKERS': '0', 'MEMEOS_OFFLINE': '1',
                   'DATABASE_URL': 'sqlite://'})

import memeos_render as R      # noqa: E402

try:
    import studio_bp as SB     # noqa: E402
except Exception as _ex:       # pragma: no cover – ohne App-Umgebung
    SB = None
    _grund = str(_ex)


CANVAS = (1080, 1350)
STIL = R.stil_dict({'primary': '#ffd400', 'dark': '#111111', 'light': '#ffffff',
                    'secondary': '#a88b00', 'accent': '#e11d48'})


def text(**rest):
    el = {'id': 't', 'type': 'text', 'text': 'Probe', 'x': 100, 'y': 500,
          'width': 880, 'height': 120, 'color': '#ffffff'}
    el.update(rest)
    return el


def flaeche(**rest):
    el = {'id': 'f', 'type': 'shape', 'shape': 'rect', 'x': 0, 'y': 460,
          'width': 1080, 'height': 200, 'fill': '#111111'}
    el.update(rest)
    return el


@unittest.skipIf(SB is None, 'studio_bp nicht ladbar')
class TestUntergrund(unittest.TestCase):
    """Die Prüfung muss sehen, was der Renderer zeichnet – auch Elemente, keine nur Bilder."""

    def untergrund(self, elemente, nummer):
        return SB._pruef_untergrund(elemente[nummer - 1], CANVAS, STIL, None,
                                    {'canvas': {'width': 1080, 'height': 1350},
                                     'elements': elemente}, {},
                                    nummer, elemente)

    def test_ohne_alles_zaehlt_die_leinwand(self):
        farbe, woher = self.untergrund([text()], 1)
        self.assertEqual(woher, 'Hintergrundfarbe')
        self.assertEqual(SB._pruef_farbe(farbe, STIL), '#ffffff')

    def test_textkasten_schlaegt_alles(self):
        els = [flaeche(), text(box={'fill': '#ff0000'})]
        farbe, woher = self.untergrund(els, 2)
        self.assertEqual(woher, 'Textkasten')
        self.assertEqual(SB._pruef_farbe(farbe, STIL), '#ff0000')

    def test_deckende_flaeche_darunter_zaehlt(self):
        els = [flaeche(), text()]
        farbe, woher = self.untergrund(els, 2)
        self.assertEqual(woher, 'Fläche darunter')
        self.assertEqual(SB._pruef_farbe(farbe, STIL), '#111111')

    def test_flaeche_ueber_dem_text_zaehlt_nicht(self):
        # Reihenfolge entscheidet: was nach dem Text kommt, liegt darüber.
        els = [text(), flaeche()]
        farbe, woher = self.untergrund(els, 1)
        self.assertEqual(woher, 'Hintergrundfarbe')

    def test_nur_vollstaendige_ueberdeckung_zaehlt(self):
        # Die Fläche deckt nur die linke Hälfte – ein Urteil wäre geraten.
        els = [flaeche(width=400), text()]
        _farbe, woher = self.untergrund(els, 2)
        self.assertEqual(woher, 'Hintergrundfarbe')

    def test_ellipse_und_verlauf_zaehlen_nicht(self):
        for abweichung in ({'shape': 'ellipse'},
                           {'fill': {'type': 'linear', 'stops': [{'at': 0, 'color': '#000'},
                                                                 {'at': 1, 'color': '#fff'}]}},
                           {'rotation': 12}, {'hidden': True}):
            with self.subTest(**abweichung):
                _f, woher = self.untergrund([flaeche(**abweichung), text()], 2)
                self.assertEqual(woher, 'Hintergrundfarbe')

    def test_die_oberste_flaeche_gewinnt(self):
        els = [flaeche(id='a', fill='#ff0000'), flaeche(id='b', fill='#00ff00'), text()]
        farbe, woher = self.untergrund(els, 3)
        self.assertEqual(woher, 'Fläche darunter')
        self.assertEqual(SB._pruef_farbe(farbe, STIL), '#00ff00')

    def test_halbdurchsichtige_flaeche_wird_verrechnet(self):
        # Schwarze Fläche zu 50 % über weißer Leinwand ergibt Grau, nicht Schwarz.
        els = [flaeche(fill='#000000', opacity=0.5), text()]
        farbe, woher = self.untergrund(els, 2)
        self.assertEqual(woher, 'Fläche darunter')
        wert = SB._pruef_farbe(farbe, STIL)
        self.assertEqual(wert[1:3], wert[3:5])
        self.assertTrue(0x70 <= int(wert[1:3], 16) <= 0x90, wert)

    def test_stil_token_in_der_flaeche_loest_sich_je_stadt_auf(self):
        els = [flaeche(fill='stil:primary'), text()]
        farbe, _ = self.untergrund(els, 2)
        self.assertEqual(SB._pruef_farbe(farbe, STIL), '#ffd400')
        anderer = dict(STIL, primary='#0000ff')
        self.assertEqual(SB._pruef_farbe(farbe, anderer), '#0000ff')


@unittest.skipIf(SB is None, 'studio_bp nicht ladbar')
class TestMischen(unittest.TestCase):

    def test_raender(self):
        self.assertEqual(SB._pruef_mischen('#000000', '#ffffff', 1.0, STIL), '#000000')
        self.assertEqual(SB._pruef_mischen('#000000', '#ffffff', 0.0, STIL), '#ffffff')

    def test_nicht_messbares_gibt_nichts(self):
        verlauf = {'type': 'linear', 'stops': [{'at': 0, 'color': '#000'}]}
        self.assertIsNone(SB._pruef_mischen(verlauf, '#ffffff', 0.5, STIL))
        self.assertIsNone(SB._pruef_mischen('#000000', None, 0.5, STIL))


@unittest.skipIf(SB is None, 'studio_bp nicht ladbar')
class TestBefunde(unittest.TestCase):
    """Die Fläche darf keine Fehler verstecken, die es wirklich gibt.

    _pruefe_vorlage fragt Stadtwissen und Stadtbilder ab und braucht darum eine Datenbank –
    hier eine leere im Arbeitsspeicher, damit nichts von der echten Anlage berührt wird."""

    @classmethod
    def setUpClass(cls):
        import app as A
        from models import db, City, MemeTemplate
        assert ':memory:' in A.app.config['SQLALCHEMY_DATABASE_URI'] \
            or A.app.config['SQLALCHEMY_DATABASE_URI'] == 'sqlite://', \
            'Test würde gegen eine echte Datenbank laufen'
        cls.A, cls.db, cls.City, cls.Vorlage = A, db, City, MemeTemplate
        cls.ctx = A.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.stadt = City(name='Musterstadt', state='Hessen', instagram_handle='musterstadt')
        db.session.add(cls.stadt)
        cls.vorlage = MemeTemplate(name='Probe', render_type='pil', pil_config='{}')
        db.session.add(cls.vorlage)
        db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.ctx.pop()

    def befunde(self, elemente):
        config = {'canvas': {'width': 1080, 'height': 1350}, 'elements': elemente}
        befunde, _n = SB._pruefe_vorlage(self.vorlage, config, [self.stadt])
        return [b for b in befunde if b['art'] == 'lesbarkeit']

    def test_heller_text_auf_heller_flaeche_faellt_durch(self):
        els = [flaeche(fill='#f5f5f5'), text(color='#ffffff')]
        self.assertTrue(any(b['schwere'] == 'fehler' for b in self.befunde(els)),
                        self.befunde(els))

    def test_heller_text_auf_dunkler_flaeche_geht_durch(self):
        els = [flaeche(fill='#111111'), text(color='#ffffff')]
        self.assertEqual(self.befunde(els), [])

    def test_halbdurchsichtige_flaeche_rettet_nicht_zu_viel(self):
        # 20 % Schwarz über Weiß bleibt hell – weißer Text darauf ist nicht lesbar.
        els = [flaeche(fill='#000000', opacity=0.2), text(color='#ffffff')]
        self.assertTrue(any(b['schwere'] == 'fehler' for b in self.befunde(els)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
