# -*- coding: utf-8 -*-
"""Tests I1 – Stadt-Stil, Rollen, Lesbarkeit und Textmaße im Renderer.

Läuft ohne Netz, ohne Datenbankdatei und ohne die Anwendung: alle Bilder entstehen
im Speicher, die Modelltests benutzen sqlite:///:memory:.

    python3 -m pytest tests/test_i1_stil.py
    python3 -m unittest tests.test_i1_stil
"""
import hashlib
import os
import sys
import unittest

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memeos_render as R          # noqa: E402
import models as M                 # noqa: E402


ALT_BRAND = {'bg': '#ffffff', 'text': '#000000', 'accent': '#3b82f6', 'font': 'Arial'}

STIL_HELL = {
    'primary': '#ffd400', 'secondary': '#a88b00', 'accent': '#e11d48',
    'dark': '#111111', 'light': '#ffffff',
    'headline': 'anton', 'body': 'bold',
    'schreibweise': 'gross', 'kasten': 'voll', 'radius': 0,
    'kontur': 'dunkel', 'schatten': True, 'wasserzeichen_ecke': 'unten_rechts',
}
STIL_DUNKEL = dict(STIL_HELL, kasten='keiner', light='#0b1020', dark='#000000',
                   primary='#22d3ee', schreibweise='normal', kontur='palette')


def grundbild(w=1080, h=1350):
    """Fester Untergrund ohne Datei: helles Feld oben links, schwarzes unten rechts."""
    im = Image.new('RGB', (w, h), (30, 60, 90))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w // 2, h // 2), fill=(220, 220, 200))
    d.rectangle((w // 2, h // 2, w, h), fill=(10, 10, 10))
    return im


def png_bild(daten):
    import io
    return Image.open(io.BytesIO(daten)).convert('RGB')


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Abwärtskompatibilität – der wichtigste Test
# ═══════════════════════════════════════════════════════════════════════════════

# Konfigurationen von Template 5 und 6 aus der Bestandsdatenbank (Stand 09.09.2026),
# wortwörtlich. Sie sind die Prüfsteine: alles Neue ist rein additiv.
TEMPLATE_5 = {'elements': [
    {'var': 'pool_story', 'type': 'text', 'x': 30, 'y': 10, 'width': 1020, 'height': 330,
     'font': 'arial_rounded_bold', 'max_size': 58, 'min_size': 28, 'color': '#FFFFFF',
     'align': 'center', 'stroke': '#0A008D', 'stroke_width': 6},
]}

TEMPLATE_6 = {'canvas': {'width': 1080, 'height': 1350}, 'elements': [
    {'id': 's1_t1', 'type': 'text', 'x': 67, 'y': 72, 'width': 946, 'height': 173,
     'font': 'anton', 'max_size': 59, 'min_size': 30, 'color': '#FFFFFF', 'align': 'center',
     'valign': 'middle', 'uppercase': True, 'line_height': 1.15, 'fit': 'shrink',
     'text': 'POV: DU MUSST KURZ DURCH'},
    {'id': 's1_t2', 'type': 'text', 'x': 67, 'y': 235, 'width': 946, 'height': 173,
     'font': 'anton', 'max_size': 83, 'min_size': 42, 'color': '#FFE01B', 'align': 'center',
     'valign': 'middle', 'uppercase': False, 'line_height': 1.15, 'fit': 'shrink',
     'var': 'problem_place', 'fallback': '{problem_place}'},
    {'id': 's1_t3', 'type': 'text', 'x': 67, 'y': 1157, 'width': 946, 'height': 173,
     'font': 'anton', 'max_size': 51, 'min_size': 26, 'color': '#FFFFFF', 'align': 'center',
     'valign': 'middle', 'uppercase': False, 'line_height': 1.15, 'fit': 'shrink',
     'text': '{city_name} KENNT DAS', 'fallback': '{city_name} KENNT DAS'},
]}

WERTE = {'pool_story': 'Am Freibad war wieder die Hoelle los und keiner wusste warum',
         'problem_place': 'Hauptbahnhof Suedseite', 'city_name': 'Darmstadt'}

# Summen, aufgenommen mit dem Stand VOR dem Umbau (Pillow 11.3.0 / FreeType 2.13.3).
# Ändern sie sich, ohne dass Pillow ausgetauscht wurde, hat der Umbau Altbestand angefasst.
ALT_SUMMEN = {
    'template_5': '99a0e0862a9700519f0d2e2228504530',
    'template_6': '07ee9d5e78100e8115670ccca365f810',
}


class TestAltbestand(unittest.TestCase):

    def test_template_5_und_6_bitgleich(self):
        """Prüfstein: dieselben Bytes wie vor dem Umbau (Pillow-abhängige Kontrollsumme)."""
        for name, cfg in (('template_5', TEMPLATE_5), ('template_6', TEMPLATE_6)):
            png = R.render(grundbild(), cfg, WERTE, brand=ALT_BRAND)
            self.assertEqual(hashlib.md5(png).hexdigest(), ALT_SUMMEN[name],
                             f'{name} rendert nicht mehr bitgleich')

    def test_vollstaendiges_stil_dict_aendert_altbestand_nicht(self):
        """Version-unabhängig: ein erweitertes brand-Dict darf alte Vorlagen nicht anfassen."""
        for cfg in (TEMPLATE_5, TEMPLATE_6):
            alt = R.render(grundbild(), cfg, WERTE, brand=ALT_BRAND)
            neu = R.render(grundbild(), cfg, WERTE, brand=R.stil_dict(ALT_BRAND))
            self.assertEqual(hashlib.md5(alt).hexdigest(), hashlib.md5(neu).hexdigest())

    def test_ohne_rolle_bleibt_das_element_dasselbe_objekt(self):
        el = {'type': 'text', 'x': 10, 'var': 't'}
        self.assertIs(R.rolle_anwenden(el, STIL_HELL, 1350), el)
        frei = {'type': 'text', 'x': 10, 'var': 't', 'role': 'frei'}
        self.assertIs(R.rolle_anwenden(frei, STIL_HELL, 1350), frei)

    def test_unbekannte_rolle_laesst_element_unveraendert(self):
        el = {'type': 'text', 'x': 10, 'var': 't', 'role': 'gibtesnicht'}
        self.assertIs(R.rolle_anwenden(el, STIL_HELL, 1350), el)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Rollen
# ═══════════════════════════════════════════════════════════════════════════════

class TestRollen(unittest.TestCase):

    BASIS = {'type': 'text', 'x': 60, 'y': 400, 'width': 960, 'height': 300,
             'align': 'center', 'valign': 'middle', 'var': 't'}
    WERT = {'t': 'Die Umleitung bleibt bis Freitag'}

    def _render(self, el, stil=None):
        cfg = {'canvas': {'width': 1080, 'height': 1350}, 'elements': [el]}
        return R.render(grundbild(), cfg, self.WERT, brand=stil or STIL_HELL)

    def test_jede_rolle_greift_und_ist_gleichbedeutend_mit_ihren_werten(self):
        """Ein Render mit Rolle muss Byte für Byte dem Render mit den aufgelösten
        Einzelwerten entsprechen – die Rolle ist also nur eine Abkürzung, kein Sonderweg."""
        nackt = hashlib.md5(self._render(dict(self.BASIS))).hexdigest()
        for rolle in R.ROLLEN_NAMEN:
            with self.subTest(rolle=rolle):
                el = dict(self.BASIS, role=rolle)
                aufgeloest = R.rolle_anwenden(el, STIL_HELL, 1350)
                self.assertIsInstance(aufgeloest, dict)
                mit_rolle = hashlib.md5(self._render(el)).hexdigest()
                if rolle != 'frei':
                    # die Rolle muss überhaupt etwas bewirken …
                    self.assertGreater(len(aufgeloest), len(self.BASIS) + 1, rolle)
                    self.assertNotEqual(mit_rolle, nackt, f'Rolle {rolle} ändert nichts')
                # … und genau das, was in ihren aufgelösten Werten steht
                ohne_rolle = {k: v for k, v in aufgeloest.items() if k != 'role'}
                self.assertEqual(mit_rolle, hashlib.md5(self._render(ohne_rolle)).hexdigest())

    def test_rolle_setzt_die_vorgesehenen_felder(self):
        erwartet = {
            'ueberschrift': {'font': 'stil:headline', 'color': 'stil:auto',
                             'max_size': round(0.075 * 1350), 'min_size': round(0.035 * 1350),
                             'uppercase': True},
            'pointe':       {'font': 'stil:headline', 'color': 'stil:accent',
                             'max_size': round(0.095 * 1350), 'min_size': round(0.045 * 1350),
                             'stroke': STIL_HELL['dark']},
            'unterzeile':   {'font': 'stil:body', 'color': 'stil:auto',
                             'max_size': round(0.042 * 1350), 'min_size': round(0.024 * 1350)},
            'ort':          {'font': 'stil:headline', 'color': 'stil:primary',
                             'max_size': round(0.085 * 1350), 'min_size': round(0.040 * 1350),
                             'uppercase': True},
            'klein':        {'font': 'stil:body', 'color': 'stil:dark',
                             'max_size': round(0.026 * 1350), 'min_size': round(0.018 * 1350),
                             'opacity': 0.85},
            'wasserzeichen': {'font': 'stil:body', 'color': 'stil:light',
                              'max_size': round(0.024 * 1350), 'min_size': round(0.016 * 1350),
                              'opacity': 0.7},
        }
        for rolle, felder in erwartet.items():
            el = R.rolle_anwenden(dict(self.BASIS, role=rolle), STIL_HELL, 1350)
            for k, v in felder.items():
                self.assertEqual(el.get(k), v, f'{rolle}.{k}')

    def test_ueberschrift_nimmt_kasten_und_schreibweise_aus_dem_stil(self):
        # Stil mit vollem Kasten und Großschreibung
        el = R.rolle_anwenden(dict(self.BASIS, role='ueberschrift'), STIL_HELL, 1350)
        self.assertEqual(el['box']['fill'], STIL_HELL['light'])
        self.assertTrue(el['uppercase'])
        # Stil ohne Kasten und mit normaler Schreibweise
        el2 = R.rolle_anwenden(dict(self.BASIS, role='ueberschrift'), STIL_DUNKEL, 1350)
        self.assertNotIn('box', el2)
        self.assertFalse(el2['uppercase'])

    def test_pointe_hat_immer_dunkle_kontur(self):
        for stil in (STIL_HELL, STIL_DUNKEL, dict(STIL_HELL, kontur='keine')):
            el = R.rolle_anwenden(dict(self.BASIS, role='pointe'), stil, 1350)
            self.assertEqual(el['stroke'], stil['dark'])
            self.assertGreaterEqual(el['stroke_width'], 2)

    def test_ort_ist_immer_gross_auch_bei_normaler_schreibweise(self):
        el = R.rolle_anwenden(dict(self.BASIS, role='ort'), STIL_DUNKEL, 1350)
        self.assertTrue(el['uppercase'])

    def test_frei_setzt_nichts(self):
        el = dict(self.BASIS, role='frei')
        self.assertEqual(R.rolle_anwenden(el, STIL_HELL, 1350), el)

    def test_gesetzter_einzelwert_schlaegt_die_rolle(self):
        """Für jede Rolle: ein am Element gesetzter Wert gewinnt – im Dict und im Bild."""
        proben = [('color', '#00ff00'), ('max_size', 140), ('min_size', 20),
                  ('font', 'bold'), ('uppercase', False), ('box', False),
                  ('stroke', '#ff00ff'), ('opacity', 0.42)]
        for rolle in R.ROLLEN_NAMEN:
            if rolle == 'frei':
                continue
            nur_rolle = R.rolle_anwenden(dict(self.BASIS, role=rolle), STIL_HELL, 1350)
            for feld, wert in proben:
                with self.subTest(rolle=rolle, feld=feld):
                    el = R.rolle_anwenden(dict(self.BASIS, role=rolle, **{feld: wert}),
                                          STIL_HELL, 1350)
                    self.assertEqual(el.get(feld), wert)
            # und im Bild: eine andere Farbe muss ein anderes Bild ergeben
            anders = self._render(dict(self.BASIS, role=rolle, color='#00ff00'))
            gleich = self._render(dict(self.BASIS, role=rolle))
            if nur_rolle.get('color') != '#00ff00':
                self.assertNotEqual(hashlib.md5(anders).hexdigest(),
                                    hashlib.md5(gleich).hexdigest(),
                                    f'Rolle {rolle}: gesetzte Farbe wurde ignoriert')

    def test_groessen_haengen_an_der_leinwandhoehe(self):
        klein = R.rolle_anwenden(dict(self.BASIS, role='pointe'), STIL_HELL, 1080)
        gross = R.rolle_anwenden(dict(self.BASIS, role='pointe'), STIL_HELL, 1350)
        self.assertLess(klein['max_size'], gross['max_size'])
        self.assertEqual(klein['max_size'], round(0.095 * 1080))

    def test_rolle_wirkt_auch_in_text_metrics(self):
        m_ohne = R.text_metrics('Test', dict(self.BASIS), (1080, 1350), brand=STIL_HELL)
        m_mit = R.text_metrics('Test', dict(self.BASIS, role='klein'), (1080, 1350),
                               brand=STIL_HELL)
        self.assertNotEqual(m_ohne['size'], m_mit['size'])
        self.assertEqual(m_mit['size'], round(0.026 * 1350))


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Lesbarkeit: Kontrast und 'stil:auto'
# ═══════════════════════════════════════════════════════════════════════════════

class TestKontrast(unittest.TestCase):

    def test_bekannte_paare(self):
        self.assertAlmostEqual(R.kontrast('#000000', '#ffffff'), 21.0, places=6)
        self.assertAlmostEqual(R.kontrast('#ffffff', '#000000'), 21.0, places=6)
        self.assertAlmostEqual(R.kontrast('#ffffff', '#ffffff'), 1.0, places=6)
        self.assertAlmostEqual(R.kontrast('#000000', '#000000'), 1.0, places=6)
        # Nachschlagewerte aus der WCAG-Rechnung
        self.assertAlmostEqual(R.kontrast('#777777', '#ffffff'), 4.478, places=2)
        self.assertAlmostEqual(R.kontrast('#0000ff', '#ffffff'), 8.592, places=2)

    def test_tokens_werden_aufgeloest(self):
        self.assertAlmostEqual(R.kontrast('stil:dark', 'stil:light', STIL_HELL),
                               R.kontrast('#111111', '#ffffff'), places=6)
        self.assertAlmostEqual(R.kontrast('brand:text', 'brand:bg', ALT_BRAND),
                               21.0, places=6)

    def test_auto_farbe(self):
        self.assertEqual(R.auto_farbe('#ffffff'), '#000000')
        self.assertEqual(R.auto_farbe('#000000'), '#ffffff')
        self.assertEqual(R.auto_farbe('#ffd400'), '#000000')      # Signalgelb -> schwarz
        self.assertEqual(R.auto_farbe('#0b1020'), '#ffffff')      # Nachtblau -> weiß


class TestStilAuto(unittest.TestCase):
    """'stil:auto' muss auf hellem Grund schwarz und auf dunklem Grund weiß werden."""

    EL = {'type': 'text', 'x': 100, 'y': 500, 'width': 880, 'height': 300,
          'font': 'anton', 'max_size': 160, 'min_size': 80, 'align': 'center',
          'valign': 'middle', 'color': 'stil:auto', 'text': 'HALLO'}

    def _anteile(self, hintergrundfarbe):
        cfg = {'canvas': {'width': 1080, 'height': 1350, 'background': hintergrundfarbe},
               'elements': [dict(self.EL)]}
        bild = png_bild(R.render(None, cfg, {}, brand=STIL_HELL))
        aus = bild.crop((100, 500, 980, 800))
        dunkel = hell = 0
        for r, g, b in aus.getdata():
            if r < 30 and g < 30 and b < 30:
                dunkel += 1
            elif r > 225 and g > 225 and b > 225:
                hell += 1
        return dunkel, hell

    def test_heller_grund_ergibt_schwarze_schrift(self):
        dunkel, hell = self._anteile('#ffffff')
        self.assertGreater(dunkel, 1000, 'auf Weiß wurde nicht schwarz geschrieben')

    def test_dunkler_grund_ergibt_weisse_schrift(self):
        dunkel, hell = self._anteile('#101010')
        self.assertGreater(hell, 1000, 'auf Schwarz wurde nicht weiß geschrieben')

    def test_kastenfarbe_schlaegt_den_bildmittelwert(self):
        """Weißer Kasten auf schwarzem Grund -> die Schrift richtet sich nach dem Kasten."""
        el = dict(self.EL, box={'fill': '#ffffff', 'per_line': True})
        cfg = {'canvas': {'width': 1080, 'height': 1350, 'background': '#000000'},
               'elements': [el]}
        bild = png_bild(R.render(None, cfg, {}, brand=STIL_HELL))
        aus = bild.crop((100, 500, 980, 800))
        dunkel = sum(1 for r, g, b in aus.getdata() if r < 30 and g < 30 and b < 30)
        self.assertGreater(dunkel, 1000, 'im weißen Kasten wurde nicht schwarz geschrieben')

    def test_auto_auch_in_formen(self):
        cfg = {'canvas': {'width': 400, 'height': 400, 'background': '#ffffff'},
               'elements': [{'type': 'shape', 'shape': 'rect', 'x': 50, 'y': 50,
                             'width': 300, 'height': 300, 'fill': 'stil:auto'}]}
        bild = png_bild(R.render(None, cfg, {}, brand=STIL_HELL))
        self.assertEqual(bild.getpixel((200, 200)), (0, 0, 0))
        cfg['canvas']['background'] = '#000000'
        bild = png_bild(R.render(None, cfg, {}, brand=STIL_HELL))
        self.assertEqual(bild.getpixel((200, 200)), (255, 255, 255))

    def test_hintergrund_farbe_im_bereich(self):
        el = {'x': 0, 'y': 0, 'width': 540, 'height': 675}          # oben links: hell
        self.assertEqual(R.hintergrund_farbe_im_bereich(grundbild(), el, (1080, 1350)),
                         '#dcdcc8')
        el2 = {'x': 540, 'y': 675, 'width': 540, 'height': 675}     # unten rechts: schwarz
        self.assertEqual(R.hintergrund_farbe_im_bereich(grundbild(), el2, (1080, 1350)),
                         '#0a0a0a')
        # ohne Bild: die weiße Grundfläche, auf die render() sonst zeichnet
        self.assertEqual(R.hintergrund_farbe_im_bereich(None, el, (1080, 1350)), '#ffffff')
        self.assertEqual(R.hintergrund_farbe_im_bereich('gibtesnicht.png', el, (1080, 1350)),
                         '#ffffff')


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Zeichen-Budget und Passprobe
# ═══════════════════════════════════════════════════════════════════════════════

class TestBudgetUndPassprobe(unittest.TestCase):

    EL = {'type': 'text', 'x': 40, 'y': 100, 'width': 1000, 'height': 240,
          'font': 'anton', 'max_size': 80, 'min_size': 40, 'line_height': 1.15, 'var': 't'}

    def _nachgerechnet(self, el, zeilen):
        """Dieselbe Rechnung, aber von Hand mit PIL: Breite / mittlere Zeichenbreite × Zeilen."""
        font = ImageFont.truetype(R.resolve_font_path(el['font']), el['min_size'])
        probe = R._BUDGET_PROBE
        if el.get('uppercase'):
            probe = probe.upper()
        mittel = font.getlength(probe) / len(probe)
        breite = el['width'] - 0            # kein padding in diesen Fällen
        return int(breite * zeilen / mittel)

    def test_budget_bleibt_unter_der_handrechnung(self):
        """Die Handrechnung ist die Obergrenze: sie rechnet ohne Reserve für den Umbruch.
        Das Budget darf darunter liegen, aber nicht ins Bodenlose."""
        el = dict(self.EL)
        # height 240, min_size 40, line_height 1.15 -> Vorschub 46 px -> 240 // 46 = 5 Zeilen
        budget = R.zeichen_budget(el, (1080, 1350))
        obergrenze = self._nachgerechnet(el, 5)
        self.assertLessEqual(budget, obergrenze)
        self.assertGreater(budget, obergrenze * 0.7)

    def test_budget_haelt_die_passprobe(self):
        """Der Kern des Budgets: wer sich genau an die genannte Zahl hält, muss passen.
        Die Zahl steht so im KI-Prompt und im Vorschlag der Vorlagen-Prüfung."""
        faelle = {
            'fuenf Zeilen': dict(self.EL),
            'zwei Zeilen': dict(self.EL, max_lines=2),
            'eine Zeile': dict(self.EL, max_lines=1),
            'grossbuchstaben': dict(self.EL, uppercase=True),
            'mit padding': dict(self.EL, padding=40),
            'ohne Hoehe': {k: v for k, v in self.EL.items() if k != 'height'},
            'winzige Box': {'type': 'text', 'x': 10, 'y': 10, 'width': 90, 'height': 34,
                            'font': 'anton', 'min_size': 30, 'max_size': 30},
            'Rolle pointe': {'type': 'text', 'x': 40, 'y': 100, 'width': 900,
                             'height': 300, 'role': 'pointe', 'var': 't'},
        }
        for name, el in faelle.items():
            with self.subTest(fall=name):
                n = R.zeichen_budget(el, (1080, 1350))
                ergebnis = R.passt_text(R._budget_probe(n), el, (1080, 1350))
                self.assertTrue(ergebnis['passt'], f'{name}: {n} Zeichen passen nicht')

    def test_budget_folgt_max_lines(self):
        eine = R.zeichen_budget(dict(self.EL, max_lines=1), (1080, 1350))
        zwei = R.zeichen_budget(dict(self.EL, max_lines=2), (1080, 1350))
        fuenf = R.zeichen_budget(dict(self.EL), (1080, 1350))     # Höhe erlaubt 5 Zeilen
        self.assertLess(eine, zwei)
        self.assertLess(zwei, fuenf)
        self.assertLessEqual(zwei, self._nachgerechnet(dict(self.EL, max_lines=2), 2))

    def test_budget_ohne_hoehe_rechnet_mit_drei_zeilen(self):
        el = {k: v for k, v in self.EL.items() if k != 'height'}
        drei = R.zeichen_budget(el, (1080, 1350))
        self.assertLessEqual(drei, self._nachgerechnet(el, R._BUDGET_ZEILEN_OHNE_HOEHE))
        self.assertLess(R.zeichen_budget(dict(el, max_lines=1), (1080, 1350)), drei)
        self.assertLess(drei, R.zeichen_budget(dict(self.EL), (1080, 1350)))

    def test_budget_waechst_mit_der_breite(self):
        schmal = R.zeichen_budget(dict(self.EL, width=500), (1080, 1350))
        breit = R.zeichen_budget(dict(self.EL, width=1000), (1080, 1350))
        self.assertAlmostEqual(breit / float(schmal), 2.0, delta=0.02)

    def test_budget_beruecksichtigt_padding(self):
        ohne = R.zeichen_budget(dict(self.EL), (1080, 1350))
        mit = R.zeichen_budget(dict(self.EL, padding=100), (1080, 1350))
        self.assertLess(mit, ohne)

    def test_budget_kennt_die_rolle(self):
        el = {'type': 'text', 'x': 40, 'y': 100, 'width': 1000, 'height': 240,
              'role': 'klein', 'var': 't'}
        # Rolle 'klein' ist winzig -> es passen viel mehr Zeichen als bei 'pointe'
        self.assertGreater(R.zeichen_budget(el, (1080, 1350)),
                           R.zeichen_budget(dict(el, role='pointe'), (1080, 1350)))

    def test_passt_kurzer_text(self):
        e = R.passt_text('Kurz', self.EL, (1080, 1350))
        self.assertTrue(e['passt'])
        self.assertEqual(e['groesse'], 80)
        self.assertEqual(e['zeilen'], 1)
        self.assertEqual(e['noetige_verkleinerung'], 0.0)

    def test_passt_text_schrumpft_und_meldet_trotzdem_passt(self):
        text = 'Die Umleitung an der Kreuzung bleibt bis Freitag bestehen'
        e = R.passt_text(text, self.EL, (1080, 1350))
        self.assertTrue(e['passt'])
        self.assertLessEqual(e['groesse'], 80)
        self.assertGreaterEqual(e['groesse'], 40)

    def test_zu_langer_text_meldet_noetige_verkleinerung(self):
        el = dict(self.EL, width=300, height=120, max_lines=2)
        text = ('Ein ausgesprochen langer Satz, der in diesem winzigen Kasten '
                'unter keinen Umstaenden vollstaendig Platz findet')
        e = R.passt_text(text, el, (1080, 1350))
        self.assertFalse(e['passt'])
        self.assertGreater(e['noetige_verkleinerung'], 0.0)
        self.assertLess(e['groesse'], el['min_size'])
        # Nachrechnung: bei der gemeldeten Größe steht der Text vollständig,
        # eine Stufe größer nicht mehr.
        self.assertAlmostEqual(e['noetige_verkleinerung'],
                               round(1.0 - e['groesse'] / float(el['min_size']), 3), places=3)
        self.assertTrue(self._steht_vollstaendig(text, el, e['groesse']))
        self.assertFalse(self._steht_vollstaendig(text, el, e['groesse'] + 1))

    def _steht_vollstaendig(self, text, el, groesse):
        probe = dict(el, max_size=groesse, min_size=groesse, fit='shrink')
        m = R.text_metrics(text, probe, (1080, 1350))
        return (''.join(''.join(m['lines']).split()) == ''.join(text.split())
                and m['width'] <= probe['width'] + 0.5
                and len(m['lines']) <= (probe.get('max_lines') or 99)
                and m['height'] <= probe['height'] + 0.5)

    def test_passt_text_kennt_die_rolle(self):
        el = {'type': 'text', 'x': 40, 'y': 100, 'width': 400, 'height': 90, 'var': 't'}
        lang = 'Ein ziemlich langer Satz fuer eine sehr kleine Flaeche im Bild'
        self.assertFalse(R.passt_text(lang, dict(el, role='pointe'), (1080, 1350))['passt'])
        self.assertTrue(R.passt_text(lang, dict(el, role='klein'), (1080, 1350))['passt'])


# ═══════════════════════════════════════════════════════════════════════════════
# 5. stil_dict, Farb- und Schrift-Token
# ═══════════════════════════════════════════════════════════════════════════════

class TestStilDict(unittest.TestCase):

    def test_ohne_angabe_die_vorgaben(self):
        s = R.stil_dict(None)
        for k in R.STIL_SCHLUESSEL:
            self.assertIn(k, s)
        # JEDER Wert muss der Vorgabe entsprechen, nicht nur primary: sonst gilt auf dem
        # Weg ohne Stadt (Vorschau ohne Stadt) eine andere Handschrift als in city_style.
        for k, wert in R.STIL_STANDARD.items():
            self.assertEqual(s[k], wert, f'stil_dict(None)[{k!r}]')
        self.assertEqual(R.stil_dict({}), s)
        self.assertEqual((s['bg'], s['text'], s['font']),
                         (R.STIL_STANDARD['light'], R.STIL_STANDARD['dark'],
                          R.STIL_STANDARD['headline']))

    def test_ohne_stadt_bleiben_die_rollen_unterscheidbar(self):
        """Ohne Stadt-Stil dürfen 'pointe' (Akzent) und 'ort' (Hauptfarbe) nicht
        dieselbe Farbe bekommen – sonst fällt die Vorschau ohne Stadt zusammen."""
        pointe = R.rolle_anwenden({'type': 'text', 'role': 'pointe'}, None, 1350)
        ort = R.rolle_anwenden({'type': 'text', 'role': 'ort'}, None, 1350)
        self.assertNotEqual(R.parse_color(pointe['color'], None),
                            R.parse_color(ort['color'], None))
        klein = R.rolle_anwenden({'type': 'text', 'role': 'klein'}, None, 1350)
        self.assertEqual(R.resolve_font_path(klein['font'], None),
                         R.resolve_font_path(R.STIL_STANDARD['body'], None))

    def test_altes_brand_dict_wird_abgeleitet(self):
        s = R.stil_dict({'bg': '#ffffff', 'text': '#000000', 'accent': '#ff0000',
                         'font': 'Arial'})
        self.assertEqual(s['primary'], '#ff0000')
        self.assertEqual(s['accent'], '#ff0000')
        self.assertEqual(s['dark'], '#000000')
        self.assertEqual(s['light'], '#ffffff')
        self.assertEqual(s['secondary'], '#9e0000')       # abgedunkeltes primary
        self.assertEqual(s['headline'], 'Arial')
        self.assertEqual(s['body'], 'Arial')
        # alte Schlüssel bleiben
        self.assertEqual((s['bg'], s['text'], s['font']), ('#ffffff', '#000000', 'Arial'))

    def test_teilangabe_wird_ergaenzt(self):
        s = R.stil_dict({'primary': '#123456'})
        self.assertEqual(s['primary'], '#123456')
        self.assertEqual(s['accent'], '#123456')
        self.assertEqual(s['light'], R.STIL_STANDARD['light'])
        self.assertEqual(s['bg'], s['light'])

    def test_unsinnige_werte_fallen_auf_vorgaben_zurueck(self):
        s = R.stil_dict({'primary': 'kein-farbwert', 'kasten': 'quatsch',
                         'kontur': 'quatsch', 'schreibweise': 'quatsch', 'radius': 999})
        self.assertEqual(s['primary'], R.STIL_STANDARD['primary'])
        self.assertEqual(s['kasten'], R.STIL_STANDARD['kasten'])
        self.assertEqual(s['kontur'], R.STIL_STANDARD['kontur'])
        self.assertEqual(s['schreibweise'], R.STIL_STANDARD['schreibweise'])
        self.assertEqual(s['radius'], 40)                 # gedeckelt

    def test_ist_wiederholbar(self):
        s = R.stil_dict(STIL_HELL)
        self.assertEqual(s, R.stil_dict(s))

    def test_farbtoken(self):
        for name in ('primary', 'secondary', 'accent', 'dark', 'light'):
            self.assertEqual(R.parse_color('stil:' + name, STIL_HELL),
                             R.parse_color(STIL_HELL[name]))
        self.assertIsNone(R.parse_color('stil:gibtesnicht', STIL_HELL))
        # brand:* bleibt unangetastet
        self.assertEqual(R.parse_color('brand:accent', ALT_BRAND),
                         R.parse_color('#3b82f6'))

    def test_schrifttoken(self):
        self.assertEqual(R.resolve_font_path('stil:headline', STIL_HELL),
                         R.resolve_font_path('anton'))
        self.assertEqual(R.resolve_font_path('stil:body', STIL_HELL),
                         R.resolve_font_path('bold'))
        self.assertEqual(R.font_key_for('stil:headline'), ('stil:headline', True))
        self.assertEqual(R.font_key_for('stil:body'), ('stil:body', True))
        # 'brand' bleibt 'brand'
        self.assertEqual(R.font_key_for('brand'), ('brand', True))

    def test_schrifttoken_ohne_stil_faellt_zurueck(self):
        self.assertTrue(R.resolve_font_path('stil:headline', None).endswith('.ttf'))


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Validierung
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidierung(unittest.TestCase):

    def _cfg(self, el):
        return {'canvas': {'width': 1080, 'height': 1350}, 'elements': [el]}

    def test_bekannte_rolle_ist_erlaubt(self):
        for rolle in R.ROLLEN_NAMEN:
            cfg = self._cfg({'type': 'text', 'x': 0, 'y': 0, 'width': 100,
                             'var': 't', 'role': rolle})
            self.assertEqual(R.validate_config(cfg), [], rolle)

    def test_unbekannte_rolle_wird_gemeldet(self):
        cfg = self._cfg({'type': 'text', 'x': 0, 'y': 0, 'width': 100,
                         'var': 't', 'role': 'schlagzeile'})
        fehler = R.validate_config(cfg)
        self.assertTrue(any('unbekannte Rolle' in f for f in fehler), fehler)

    def test_rolle_an_nicht_text_wird_gemeldet(self):
        cfg = self._cfg({'type': 'shape', 'shape': 'rect', 'x': 0, 'y': 0, 'width': 10,
                         'height': 10, 'fill': '#fff', 'role': 'pointe'})
        fehler = R.validate_config(cfg)
        self.assertTrue(any('nur auf text-Elemente' in f for f in fehler), fehler)

    def test_stil_farben_sind_gueltige_farbwerte(self):
        cfg = self._cfg({'type': 'text', 'x': 0, 'y': 0, 'width': 100, 'var': 't',
                         'color': 'stil:auto', 'stroke': 'stil:dark'})
        self.assertEqual(R.validate_config(cfg), [])

    def test_unbekannte_stil_farbe_wird_gemeldet(self):
        cfg = self._cfg({'type': 'text', 'x': 0, 'y': 0, 'width': 100, 'var': 't',
                         'color': 'stil:knallrot'})
        self.assertTrue(any('ungültige Farbe' in f for f in R.validate_config(cfg)))


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Modell: CityStyle und die Stil-Pakete
# ═══════════════════════════════════════════════════════════════════════════════

class TestCityStyle(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Flask
        cls.app = Flask('test_i1_stil')
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        cls.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        M.db.init_app(cls.app)
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        M.db.create_all()

    @classmethod
    def tearDownClass(cls):
        M.db.session.remove()
        M.db.drop_all()
        cls.ctx.pop()

    def _stadt(self, name, **kw):
        stadt = M.City(name=name, **kw)
        M.db.session.add(stadt)
        M.db.session.commit()
        return stadt

    def test_vorgaben_stimmen_mit_dem_renderer_ueberein(self):
        """models.CITY_STYLE_DEFAULTS und memeos_render.STIL_STANDARD dürfen nicht driften."""
        self.assertEqual(M.CITY_STYLE_DEFAULTS, R.STIL_STANDARD)
        self.assertEqual(M.STIL_SCHREIBWEISEN, R.SCHREIBWEISEN)
        self.assertEqual(M.STIL_KASTEN_ARTEN, R.KASTEN_ARTEN)
        self.assertEqual(M.STIL_KONTUR_ARTEN, R.KONTUR_ARTEN)
        self.assertEqual(M.STIL_WASSERZEICHEN_ECKEN, R.WASSERZEICHEN_ECKEN)

    def test_stadt_ohne_stil_bekommt_einen_abgeleiteten(self):
        stadt = self._stadt('Ohnestil', accent_color='#ff0000', brand_bg='#eeeeee',
                            brand_text_color='#222222', brand_font='Arial')
        s = M.CityStyle.fuer(stadt)
        self.assertEqual(s['primary'], '#ff0000')
        self.assertEqual(s['accent'], '#ff0000')
        self.assertEqual(s['light'], '#eeeeee')
        self.assertEqual(s['dark'], '#222222')
        self.assertEqual(s['secondary'], M._stil_abdunkeln('#ff0000'))
        self.assertEqual(s['headline'], 'Arial')
        # und ist ohne Umweg als brand für render() brauchbar
        self.assertEqual(R.stil_dict(s)['primary'], '#ff0000')

    def test_ohne_stadt(self):
        s = M.CityStyle.fuer(None)
        self.assertEqual(s['primary'], M.CITY_STYLE_DEFAULTS['primary'])

    def test_eigene_zeile_gewinnt(self):
        stadt = self._stadt('Mitstil', accent_color='#ff0000')
        zeile = M.CityStyle.aus_paket(stadt.id, 'nacht')
        M.db.session.add(zeile)
        M.db.session.commit()
        M.db.session.refresh(stadt)
        s = M.CityStyle.fuer(stadt)
        self.assertEqual(s['paket'], 'nacht')
        self.assertEqual(s['primary'], M.STIL_PAKETE['nacht']['primary'])
        self.assertEqual(s['kasten'], 'umriss')
        self.assertIsNotNone(stadt.style)

    def test_genau_eine_zeile_je_stadt(self):
        from sqlalchemy.exc import IntegrityError
        stadt = self._stadt('Doppelt')
        M.db.session.add(M.CityStyle(city_id=stadt.id))
        M.db.session.commit()
        M.db.session.add(M.CityStyle(city_id=stadt.id))
        with self.assertRaises(IntegrityError):
            M.db.session.commit()
        M.db.session.rollback()

    def test_to_dict_hat_alle_schluessel(self):
        stadt = self._stadt('Schluessel')
        zeile = M.CityStyle(city_id=stadt.id)
        M.db.session.add(zeile)
        M.db.session.commit()
        d = zeile.to_dict()
        for k in R.STIL_SCHLUESSEL:
            self.assertIn(k, d, k)
        self.assertEqual(d['font_headline'], d['headline'])
        self.assertEqual(d['font_body'], d['body'])

    def test_unbekanntes_paket(self):
        self.assertIsNone(M.CityStyle.aus_paket(1, 'gibtesnicht'))
        self.assertFalse(M.CityStyle(city_id=1).paket_setzen(''))

    def test_alle_pakete_sind_vollstaendig_und_gueltig(self):
        self.assertGreaterEqual(len(M.STIL_PAKETE), 6)
        for key, p in M.STIL_PAKETE.items():
            with self.subTest(paket=key):
                self.assertTrue(p.get('name'))
                self.assertTrue(p.get('beschreibung'))
                self.assertTrue(p['beschreibung'].endswith('.'))
                for farbe in ('primary', 'secondary', 'accent', 'dark', 'light'):
                    self.assertIsNotNone(R.parse_color(p[farbe]), f'{key}.{farbe}')
                for feld, erlaubt in (('schreibweise', M.STIL_SCHREIBWEISEN),
                                      ('kasten', M.STIL_KASTEN_ARTEN),
                                      ('kontur', M.STIL_KONTUR_ARTEN),
                                      ('wasserzeichen_ecke', M.STIL_WASSERZEICHEN_ECKEN)):
                    self.assertIn(p[feld], erlaubt, f'{key}.{feld}')
                self.assertTrue(0 <= p['radius'] <= 40)
                self.assertIsInstance(p['schatten'], bool)
                for f in ('font_headline', 'font_body'):
                    self.assertTrue(os.path.isfile(R.resolve_font_path(p[f])), f'{key}.{f}')

    def test_pakete_sehen_wirklich_verschieden_aus(self):
        """Dieselbe Vorlage darf nicht in zwei Paketen dasselbe Bild ergeben."""
        el = {'type': 'text', 'x': 60, 'y': 500, 'width': 960, 'height': 300,
              'role': 'ueberschrift', 'align': 'center', 'valign': 'middle',
              'text': 'Die Umleitung bleibt bis Freitag'}
        cfg = {'canvas': {'width': 1080, 'height': 1350}, 'elements': [el]}
        summen = {}
        for key in M.STIL_PAKETE:
            zeile = M.CityStyle.aus_paket(1, key)
            png = R.render(grundbild(), cfg, {}, brand=zeile.to_dict())
            summen[key] = hashlib.md5(png).hexdigest()
        self.assertEqual(len(set(summen.values())), len(summen), summen)

    def test_stil_paket_liste(self):
        liste = M.stil_paket_liste()
        self.assertEqual(len(liste), len(M.STIL_PAKETE))
        self.assertTrue(all(e.get('key') and e.get('beschreibung') for e in liste))


if __name__ == '__main__':
    unittest.main(verbosity=2)
