# -*- coding: utf-8 -*-
"""Tests für stadt_handschrift – die Ableitung einer Handschrift aus der Markenfarbe.

Läuft ohne Netz, ohne Datenbankdatei und ohne die Anwendung.

    python3 -m pytest tests/test_handschrift.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stadt_handschrift as H     # noqa: E402
import memeos_render as R         # noqa: E402


# Echte Markenfarben aus dem Bestand – kein ausgedachtes Testmaterial.
MARKEN = [('#3db8ff', '#1a4fcc'), ('#FFD700', '#1a3a6e'), ('#60a5fa', '#1e3a8a'),
          ('#f87171', '#991b1b'), ('#fbbf24', '#78350f'), ('#fb923c', '#9a3412'),
          ('#34d399', '#064e3b'), ('#e3b33c', '#123128'), ('#4ade80', '#14532d')]


class TestFarbe(unittest.TestCase):

    def test_hex_rgb_vertraegt_alles(self):
        self.assertEqual(H.hex_rgb('#fff'), (255, 255, 255))
        self.assertEqual(H.hex_rgb('#FF0000'), (255, 0, 0))
        for murks in (None, '', 'rot', '#xyzxyz', '#12', 42):
            self.assertEqual(H.hex_rgb(murks), (0, 0, 0), repr(murks))

    def test_kontrast_kennt_seine_raender(self):
        self.assertAlmostEqual(H.kontrast('#000000', '#ffffff'), 21.0, places=1)
        self.assertAlmostEqual(H.kontrast('#808080', '#808080'), 1.0, places=2)
        # symmetrisch
        self.assertAlmostEqual(H.kontrast('#123456', '#abcdef'),
                               H.kontrast('#abcdef', '#123456'), places=6)

    def test_deckt_sich_mit_dem_renderer(self):
        # Beide rechnen WCAG – liefen sie auseinander, würde die Prüfung etwas anderes
        # messen als die Ableitung vorher zugesichert hat.
        for a, b in (('#3db8ff', '#050b17'), ('#FFD700', '#111111'), ('#f87171', '#faf2f2')):
            self.assertAlmostEqual(H.kontrast(a, b), R.kontrast(a, b), places=6)
            self.assertAlmostEqual(H.leuchtdichte(a), R.leuchtdichte(a), places=6)


class TestPalette(unittest.TestCase):

    def test_markenfarbe_bleibt_unangetastet(self):
        for marke, logo in MARKEN:
            self.assertEqual(H.palette(marke, logo)['primary'], marke)

    def test_fuenf_gueltige_farben(self):
        for marke, logo in MARKEN:
            p = H.palette(marke, logo)
            with self.subTest(marke=marke):
                self.assertEqual(set(p), {'primary', 'secondary', 'accent', 'dark', 'light'})
                for key, wert in p.items():
                    self.assertIsNotNone(R.parse_color(wert), f'{key}={wert!r}')

    def test_hell_und_dunkel_tragen_einander(self):
        for marke, logo in MARKEN:
            p = H.palette(marke, logo)
            with self.subTest(marke=marke):
                # Heller Text auf dunklem Grund ist der häufigste Fall überhaupt.
                self.assertGreater(H.kontrast(p['light'], p['dark']), 12.0)
                # Die Hauptfarbe muss als Ortszeile auf dem dunklen Grund lesbar sein.
                self.assertGreater(H.kontrast(p['primary'], p['dark']), 4.5)

    def test_pointe_knallt_und_verschmilzt_nicht(self):
        for marke, logo in MARKEN:
            p = H.palette(marke, logo)
            with self.subTest(marke=marke):
                self.assertGreaterEqual(H.kontrast(p['accent'], p['dark']),
                                        H.POINTE_MIN_KONTRAST)
                # deutlich anderer Farbton als die Hauptfarbe, sonst keine eigene Stimme
                h_p, h_a = H._hls(p['primary'])[0], H._hls(p['accent'])[0]
                abstand = abs(((h_a - h_p) + 0.5) % 1.0 - 0.5) * 360
                self.assertGreater(abstand, 100, f'nur {abstand:.0f}° Abstand')

    def test_ohne_zweitfarbe_geht_es_auch(self):
        for marke, _ in MARKEN:
            p = H.palette(marke)
            with self.subTest(marke=marke):
                self.assertEqual(p['primary'], marke)
                self.assertGreater(H.kontrast(p['light'], p['dark']), 12.0)

    def test_unlesbare_farbe_gibt_nichts(self):
        for murks in (None, '', 'blau', '#xyz', 'rgb(1,2,3)', 123):
            self.assertEqual(H.palette(murks), {}, repr(murks))
            self.assertEqual(H.handschrift(murks), {}, repr(murks))

    def test_dieselbe_eingabe_dasselbe_ergebnis(self):
        for marke, logo in MARKEN:
            self.assertEqual(H.palette(marke, logo), H.palette(marke, logo))

    def test_verschiedene_marken_verschiedene_paletten(self):
        paletten = {H.palette(m, l)['accent'] for m, l in MARKEN}
        # Frankfurt und Kaiserslautern teilen sich die Markenfarbe im Bestand – bei neun
        # Marken bleiben deshalb mindestens sieben verschiedene Pointen übrig.
        self.assertGreaterEqual(len(paletten), 7, paletten)


class TestHandschrift(unittest.TestCase):

    def test_enthaelt_palette_und_anmutung(self):
        h = H.handschrift('#3db8ff', '#1a4fcc', 0)
        for key in ('primary', 'accent', 'dark', 'light', 'headline', 'body',
                    'schreibweise', 'kasten', 'radius', 'kontur', 'schatten',
                    'wasserzeichen_ecke'):
            self.assertIn(key, h)

    def test_anmutung_bricht_um_statt_zu_scheitern(self):
        a = H.handschrift('#3db8ff', None, 0)
        b = H.handschrift('#3db8ff', None, len(H.ANMUTUNGEN))
        self.assertEqual(a['headline'], b['headline'])
        self.assertEqual(H.handschrift('#3db8ff', None, -1)['headline'],
                         H.ANMUTUNGEN[-1]['headline'])

    def test_jede_anmutung_ist_vom_renderer_verstanden(self):
        for i, a in enumerate(H.ANMUTUNGEN):
            with self.subTest(anmutung=i):
                self.assertIn(a['schreibweise'], R.SCHREIBWEISEN)
                self.assertIn(a['kasten'], R.KASTEN_ARTEN)
                self.assertIn(a['kontur'], R.KONTUR_ARTEN)
                self.assertIsInstance(a['schatten'], bool)

    def test_anmutungen_sind_untereinander_verschieden(self):
        kern = [(a['headline'], a['body'], a['schreibweise'], a['kasten'], a['kontur'])
                for a in H.ANMUTUNGEN]
        self.assertEqual(len(kern), len(set(kern)), 'zwei Anmutungen sind gleich')

    def test_eine_handschrift_rendert(self):
        import meme_muster as MU
        stil = R.stil_dict(H.handschrift('#3db8ff', '#1a4fcc', 2))
        png = R.render(None, MU.muster_config('zitat'),
                       {'city_name': 'Musterstadt', 'spruch': 'Probe',
                        'city_handle': '@probe'}, brand=stil)
        self.assertGreater(len(png), 1000)


class TestVerteilung(unittest.TestCase):

    # Echte Koordinaten – der Rhein-Main-Raum ist der dichte Fall, auf den es ankommt.
    STAEDTE = [(1, 'Darmstadt', 49.8728, 8.6512), (2, 'Frankfurt', 50.1109, 8.6821),
               (3, 'Wiesbaden', 50.0782, 8.2397), (4, 'Mainz', 49.9929, 8.2473),
               (5, 'Mannheim', 49.4875, 8.4660), (6, 'Heidelberg', 49.3988, 8.6724),
               (7, 'Offenbach', 50.1055, 8.7611), (8, 'Hanau', 50.1328, 8.9166),
               (9, 'Kaiserslautern', 49.4401, 7.7491), (10, 'Braunschweig', 52.2689, 10.5268)]

    def test_nachbarn_sehen_verschieden_aus(self):
        z = H.anmutungen_verteilen(self.STAEDTE)
        koord = {s[0]: (s[2], s[3]) for s in self.STAEDTE}
        for i, a in enumerate(self.STAEDTE):
            for b in self.STAEDTE[i + 1:]:
                if H.entfernung_km(koord[a[0]], koord[b[0]]) > H.NACHBAR_KM:
                    continue
                with self.subTest(a=a[1], b=b[1]):
                    self.assertNotEqual(z[a[0]], z[b[0]])

    def test_jede_stadt_bekommt_etwas_gueltiges(self):
        z = H.anmutungen_verteilen(self.STAEDTE)
        self.assertEqual(set(z), {s[0] for s in self.STAEDTE})
        for wert in z.values():
            self.assertIn(wert, range(len(H.ANMUTUNGEN)))

    def test_dieselbe_eingabe_dieselbe_zuteilung(self):
        a = H.anmutungen_verteilen(self.STAEDTE)
        b = H.anmutungen_verteilen(list(reversed(self.STAEDTE)))
        self.assertEqual(a, b, 'die Reihenfolge der Liste darf nichts ändern')

    def test_ohne_koordinaten_gilt_niemand_als_nachbar(self):
        ohne = [(i, f'Stadt {i}', None, None) for i in range(5)]
        z = H.anmutungen_verteilen(ohne)
        self.assertEqual(len(z), 5)
        self.assertEqual(len(set(z.values())), 5, 'trotzdem verschieden vergeben')

    def test_mehr_staedte_als_anmutungen(self):
        # Alle am selben Ort: die Tabelle reicht nicht, es darf trotzdem nicht krachen.
        eng = [(i, f'Stadt {i:02d}', 50.0, 8.0) for i in range(len(H.ANMUTUNGEN) + 5)]
        z = H.anmutungen_verteilen(eng)
        self.assertEqual(len(z), len(eng))
        self.assertEqual(len(set(z.values())), len(H.ANMUTUNGEN), 'alle Anmutungen genutzt')

    def test_leere_eingabe(self):
        self.assertEqual(H.anmutungen_verteilen([]), {})

    def test_entfernung(self):
        # Frankfurt–Offenbach sind knapp 7 km, Frankfurt–Braunschweig gut 230 km.
        self.assertLess(H.entfernung_km((50.1109, 8.6821), (50.1055, 8.7611)), 10)
        self.assertGreater(H.entfernung_km((50.1109, 8.6821), (52.2689, 10.5268)), 200)
        for murks in ((None, 8.0), ('x', 'y'), (50.0, None)):
            self.assertGreater(H.entfernung_km(murks, (50.0, 8.0)), 1e8, repr(murks))


class TestSchriftauskunft(unittest.TestCase):

    def test_zaehlt_nur_baubare_anmutungen(self):
        alle = {a['headline'] for a in H.ANMUTUNGEN} | {a['body'] for a in H.ANMUTUNGEN}
        self.assertEqual(H.schriftpaare_moeglich(alle), len(H.ANMUTUNGEN))
        self.assertEqual(H.schriftpaare_moeglich([]), 0)
        self.assertEqual(H.schriftpaare_moeglich(None), 0)
        nur_eine = H.schriftpaare_moeglich(['anton'])
        self.assertTrue(0 < nur_eine < len(H.ANMUTUNGEN), nur_eine)

    def test_die_eingebauten_schriften_reichen_fuer_alle_anmutungen(self):
        vorhanden = [f['key'] for f in R.list_fonts()]
        self.assertEqual(H.schriftpaare_moeglich(vorhanden), len(H.ANMUTUNGEN),
                         'eine Anmutung nennt eine Schrift, die es nicht gibt')


if __name__ == '__main__':
    unittest.main(verbosity=2)
