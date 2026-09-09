# -*- coding: utf-8 -*-
"""Tests für die Muster – die fertigen Vorlagen aus meme_muster.py.

Läuft ohne Netz, ohne Datenbankdatei und ohne die Anwendung.

    python3 -m pytest tests/test_muster.py
"""
import os
import sys
import unittest

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import meme_muster as MU        # noqa: E402
import memeos_render as R       # noqa: E402
import models as M              # noqa: E402


def grundbild(w=1080, h=1350):
    """Untergrund mit hellen und dunklen Feldern – deckt beide Fälle für stil:auto ab."""
    im = Image.new('RGB', (w, h), (60, 90, 120))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w, h // 3), fill=(225, 225, 210))
    d.rectangle((0, 2 * h // 3, w, h), fill=(15, 15, 20))
    return im


WERTE = dict(MU.BEISPIELE)
WERTE.update({
    'oben': 'Wenn du sagst du kommst gleich', 'unten': 'und dann noch zweimal duschst',
    'pointe': 'und der Bus kommt trotzdem nicht', 'spruch': 'Hier kennt jeder jeden.',
    'wenn': 'Wenn du pünktlich sein willst', 'dann': 'aber der Ring dich anders sieht',
    'punkt1': 'Der Ring ist dicht', 'punkt2': 'Der Bahnhof ist laut',
    'punkt3': 'Die Rolltreppe ist kaputt',
    'label_oben': 'Was du erwartest', 'label_unten': 'Was du bekommst',
})


class TestKatalog(unittest.TestCase):

    def test_schluessel_sind_eindeutig(self):
        keys = [m['key'] for m in MU.MUSTER]
        self.assertEqual(len(keys), len(set(keys)), keys)

    def test_jedes_muster_ist_vollstaendig(self):
        for m in MU.MUSTER:
            with self.subTest(muster=m['key']):
                for feld in ('key', 'name', 'info', 'format', 'variablen', 'els'):
                    self.assertTrue(m.get(feld) not in (None, '', []), feld)
                self.assertIn(m['format'], MU.FORMATE)
                self.assertTrue(m['els'], 'keine Elemente')

    def test_liste_deckt_sich_mit_der_tabelle(self):
        liste = MU.muster_liste()
        self.assertEqual([e['key'] for e in liste], [m['key'] for m in MU.MUSTER])
        for e, m in zip(liste, MU.MUSTER):
            self.assertEqual(e['elemente'], len(m['els']))
            self.assertEqual((e['masse']['width'], e['masse']['height']),
                             MU.FORMATE[m['format']])

    def test_unbekannter_schluessel_gibt_nichts_zurueck(self):
        self.assertIsNone(MU.muster_holen('gibtsnicht'))
        self.assertIsNone(MU.muster_config('gibtsnicht'))
        self.assertIsNone(MU.muster_masse('gibtsnicht'))
        self.assertEqual(MU.muster_elemente('gibtsnicht', 1080, 1080), [])
        self.assertEqual(MU.muster_variablen('gibtsnicht'), [])

    def test_holen_gibt_eine_kopie(self):
        # Wer am Ergebnis herumschreibt, darf den Katalog nicht verändern.
        a = MU.muster_holen('zitat')
        a['name'] = 'kaputt'
        a['els'].clear()
        b = MU.muster_holen('zitat')
        self.assertEqual(b['name'], 'Zitatkarte')
        self.assertTrue(b['els'])


class TestConfig(unittest.TestCase):

    def test_jedes_muster_ergibt_eine_gueltige_config(self):
        for m in MU.MUSTER:
            with self.subTest(muster=m['key']):
                self.assertEqual(R.validate_config(MU.muster_config(m['key'])), [])

    def test_element_ids_sind_eindeutig(self):
        for m in MU.MUSTER:
            ids = [el['id'] for el in MU.muster_config(m['key'])['elements']]
            with self.subTest(muster=m['key']):
                self.assertEqual(len(ids), len(set(ids)), ids)

    def test_masse_werden_uebernommen(self):
        cfg = MU.muster_config('oben_unten', 800, 800)
        self.assertEqual((cfg['canvas']['width'], cfg['canvas']['height']), (800, 800))
        # Anteile, keine festen Pixel: bei halber Leinwand halbe Werte
        gross = MU.muster_elemente('oben_unten', 1080, 1350)
        klein = MU.muster_elemente('oben_unten', 540, 675)
        for a, b in zip(gross, klein):
            self.assertAlmostEqual(a['width'] / 2, b['width'], delta=1)

    def test_keine_festen_farben(self):
        # Ein Hexwert im Muster hieße: alle Städte sehen an dieser Stelle gleich aus.
        def pruefe(wert, wo):
            if isinstance(wert, str) and wert.startswith('#'):
                self.fail(f'{wo}: feste Farbe {wert!r}')
            if isinstance(wert, dict):
                for k, v in wert.items():
                    pruefe(v, f'{wo}.{k}')
            if isinstance(wert, list):
                for i, v in enumerate(wert):
                    pruefe(v, f'{wo}[{i}]')
        for m in MU.MUSTER:
            pruefe(MU.muster_config(m['key']), m['key'])

    def test_wasserzeichen_ecke_kommt_aus_dem_stil(self):
        links = MU.muster_elemente('oben_unten', 1080, 1350, 'unten_links')
        rechts = MU.muster_elemente('oben_unten', 1080, 1350, 'unten_rechts')
        wz_l = [e for e in links if e.get('var') == 'city_handle'][0]
        wz_r = [e for e in rechts if e.get('var') == 'city_handle'][0]
        self.assertLess(wz_l['x'], wz_r['x'])
        # 'keins' lässt es ganz weg
        ohne = MU.muster_elemente('oben_unten', 1080, 1350, 'keins')
        self.assertEqual([e for e in ohne if e.get('var') == 'city_handle'], [])

    def test_variablen_umfassen_auch_die_im_text(self):
        # 'POV: Du wohnst in {city_name}' steht als Text, nicht als var
        self.assertIn('city_name', MU.muster_variablen('pov'))
        self.assertIn('pointe', MU.muster_variablen('pov'))
        self.assertIn('city_name', MU.muster_variablen('liste'))


class TestRendern(unittest.TestCase):
    """Jedes Muster muss in jedem Stilpaket ein lesbares Bild ergeben."""

    def test_alle_muster_in_allen_paketen(self):
        for paket in M.STIL_PAKETE:
            stil = R.stil_dict(M.CityStyle.aus_paket(1, paket).to_dict())
            for m in MU.MUSTER:
                cfg = MU.muster_config(m['key'],
                                       wasserzeichen_ecke=stil['wasserzeichen_ecke'])
                bild = grundbild(cfg['canvas']['width'], cfg['canvas']['height'])
                with self.subTest(paket=paket, muster=m['key']):
                    png = R.render(bild, cfg, WERTE, brand=stil)
                    self.assertGreater(len(png), 1000)

    def test_stilpakete_ergeben_verschiedene_bilder(self):
        import hashlib
        for key in ('oben_unten', 'zitat', 'liste'):
            cfg = MU.muster_config(key)
            summen = set()
            for paket in M.STIL_PAKETE:
                stil = R.stil_dict(M.CityStyle.aus_paket(1, paket).to_dict())
                bild = grundbild(cfg['canvas']['width'], cfg['canvas']['height'])
                summen.add(hashlib.md5(R.render(bild, cfg, WERTE, brand=stil)).hexdigest())
            with self.subTest(muster=key):
                self.assertEqual(len(summen), len(M.STIL_PAKETE),
                                 f'{key}: nur {len(summen)} verschiedene Bilder')

    def test_kein_text_faellt_still_weg(self):
        # Jedes Text-Element hat entweder festen Text oder einen Rückfallwert – sonst
        # rendert die Vorlage bei fehlendem Wert kommentarlos eine leere Fläche.
        for m in MU.MUSTER:
            for el in MU.muster_config(m['key'])['elements']:
                if el.get('type') != 'text':
                    continue
                with self.subTest(muster=m['key'], element=el['id']):
                    self.assertTrue(el.get('text') or el.get('fallback'),
                                    f'{el.get("var")} ohne Text und ohne Rückfallwert')


if __name__ == '__main__':
    unittest.main(verbosity=2)
