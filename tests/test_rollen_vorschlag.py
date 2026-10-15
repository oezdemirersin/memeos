# -*- coding: utf-8 -*-
"""Tests für den Rollen-Vorschlag.

Eine Vorlage mit festen Farben sieht in jeder Stadt gleich aus. Rollen lösen das – aber
nur, wenn beim Übernehmen auch die festen Werte abgegeben werden. Genau das wird hier
geprüft, und zwar am fertigen Bild: dieselbe Vorlage, derselbe Text, vier Städte.

    python3 -m pytest tests/test_rollen_vorschlag.py
"""
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.update({k: '' for k in (
    'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
    'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
    'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN')})
os.environ.setdefault('MEMEOS_SCHEDULER', '0')
os.environ.setdefault('MEMEOS_WORKERS', '0')
os.environ.setdefault('MEMEOS_OFFLINE', '1')
os.environ.setdefault('DATABASE_URL', 'sqlite://')

import memeos_render as R          # noqa: E402
import stadt_handschrift as H      # noqa: E402


# Eine Vorlage, wie sie aus Canva kommt: alles fest verdrahtet, nichts stadtabhängig.
FESTE_VORLAGE = {
    'canvas': {'width': 1080, 'height': 1350, 'background': '#2b2b2b'},
    'elements': [
        {'id': 'oben', 'type': 'text', 'text': 'POV: DU MUSST KURZ DURCH',
         'x': 60, 'y': 60, 'width': 960, 'height': 160, 'font': 'anton',
         'max_size': 90, 'min_size': 40, 'color': '#ffffff',
         'stroke': '#0a008d', 'stroke_width': 6},
        {'id': 'unten', 'type': 'text', 'var': 'problem_place',
         'fallback': 'den Hauptbahnhof',
         'x': 60, 'y': 1050, 'width': 960, 'height': 200, 'font': 'anton',
         'max_size': 110, 'min_size': 50, 'color': '#ffe01b'},
        {'id': 'wz', 'type': 'text', 'var': 'city_handle', 'fallback': '@stadtseite',
         'x': 620, 'y': 1285, 'width': 420, 'height': 46, 'font': 'bold',
         'max_size': 30, 'min_size': 20, 'color': '#ffffff', 'opacity': 0.7},
    ],
}


class TestRollenVorschlag(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import app as A
        import studio_bp as SB
        from models import db, City, CityStyle, MemeTemplate
        uri = A.app.config['SQLALCHEMY_DATABASE_URI']
        assert uri == 'sqlite://' or ':memory:' in uri, f'echte Datenbank: {uri}'
        cls.A, cls.SB, cls.db = A, SB, db
        cls.City, cls.CityStyle, cls.MemeTemplate = City, CityStyle, MemeTemplate
        cls.ctx = A.app.app_context()
        cls.ctx.push()
        db.create_all()
        A.app.config['TESTING'] = True
        cls.c = A.app.test_client()
        with cls.c.session_transaction() as s:
            s['logged_in'] = True
            s['csrf_token'] = 'tt'
        cls.H = {'X-CSRF-Token': 'tt'}

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        self._meine = []
        self.vorlage = self.MemeTemplate(
            name='Rollenprobe', render_type='pil',
            pil_config=json.dumps(FESTE_VORLAGE), required_vars='[]', active=True)
        self.db.session.add(self.vorlage)
        self.db.session.commit()

    def tearDown(self):
        self.db.session.delete(self.vorlage)
        for s in self._meine:
            z = self.CityStyle.query.filter_by(city_id=s.id).first()
            if z is not None:
                self.db.session.delete(z)
            self.db.session.delete(s)
        self.db.session.commit()

    def stadt(self, name, farbe, lat, lon):
        s = self.City(name=name, state='Hessen', accent_color=farbe, lat=lat, lon=lon,
                      instagram_handle=name.lower())
        self.db.session.add(s)
        self.db.session.commit()
        self._meine.append(s)
        return s

    def vorschlag(self, **rumpf):
        return self.c.post(f'/api/studio/{self.vorlage.id}/rollen-vorschlag',
                           json=rumpf, headers=self.H)

    # ── Was vorgeschlagen wird ───────────────────────────────────────────
    def test_erkennt_die_drei_rollen(self):
        js = self.vorschlag().get_json() or {}
        nach_id = {v['element_id']: v for v in js['vorschlaege']}
        self.assertEqual(nach_id['oben']['rolle'], 'memezeile')
        self.assertEqual(nach_id['unten']['rolle'], 'pointe')
        self.assertEqual(nach_id['wz']['rolle'], 'wasserzeichen')
        for v in js['vorschlaege']:
            self.assertTrue(v['grund'], v)

    def test_stadtname_wird_zur_ortszeile(self):
        cfg = json.loads(json.dumps(FESTE_VORLAGE))
        cfg['elements'][0]['var'] = 'city_name'
        js = self.vorschlag(config=cfg).get_json() or {}
        self.assertEqual(js['vorschlaege'][0]['rolle'], 'ort')

    def test_vorhandene_rolle_bleibt(self):
        cfg = json.loads(json.dumps(FESTE_VORLAGE))
        cfg['elements'][0]['role'] = 'ueberschrift'
        js = self.vorschlag(config=cfg).get_json() or {}
        self.assertEqual(js['vorschlaege'][0]['rolle'], 'ueberschrift')
        self.assertEqual(js['vorschlaege'][0]['aendert'], [])

    # ── Vorschau ändert nichts ───────────────────────────────────────────
    def test_ohne_speichern_bleibt_die_vorlage_unberuehrt(self):
        vorher = self.vorlage.pil_config
        js = self.vorschlag().get_json() or {}
        self.assertFalse(js['gespeichert'])
        self.db.session.expire_all()
        self.assertEqual(self.MemeTemplate.query.get(self.vorlage.id).pil_config, vorher)

    # ── Das Entscheidende: die Wirkung ───────────────────────────────────
    def test_uebernehmen_gibt_die_festen_werte_ab(self):
        r = self.vorschlag(speichern=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.db.session.expire_all()
        cfg = json.loads(self.MemeTemplate.query.get(self.vorlage.id).pil_config)
        for el in cfg['elements']:
            with self.subTest(element=el['id']):
                self.assertTrue(el.get('role'), 'keine Rolle gesetzt')
                for feld in ('color', 'stroke', 'font', 'max_size', 'opacity'):
                    self.assertNotIn(feld, el,
                                     f'{feld} steht noch fest da und schlägt die Rolle')

    def test_aus_einem_design_werden_vier(self):
        """Der ganze Zweck: dieselbe Vorlage, derselbe Text, vier Städte – vorher ein
        Bild, hinterher vier verschiedene."""
        staedte = [self.stadt(n, f, la, lo) for n, f, la, lo in (
            ('Rotstadt', '#e11d48', 50.0, 8.0), ('Blaustadt', '#2563eb', 51.5, 9.5),
            ('Gruenstadt', '#16a34a', 53.0, 11.0), ('Gelbstadt', '#eab308', 48.0, 12.5))]

        def summen():
            cfg = json.loads(self.MemeTemplate.query.get(self.vorlage.id).pil_config)
            out = []
            for s in staedte:
                png = R.render(None, cfg, {'problem_place': 'den Hauptbahnhof',
                                           'city_handle': '@probe'},
                               brand=self.A._city_brand(s))
                out.append(hashlib.md5(png).hexdigest())
            return out

        vorher = summen()
        self.assertEqual(len(set(vorher)), 1,
                         'Die Ausgangsvorlage sollte überall gleich aussehen')
        self.assertEqual(self.vorschlag(speichern=True).status_code, 200)
        self.db.session.expire_all()
        nachher = summen()
        self.assertEqual(len(set(nachher)), 4,
                         f'nur {len(set(nachher))} verschiedene Designs in 4 Städten')

    # ── Ränder ───────────────────────────────────────────────────────────
    def test_unbekannte_vorlage(self):
        r = self.c.post('/api/studio/999999/rollen-vorschlag', json={}, headers=self.H)
        self.assertEqual(r.status_code, 404)

    def test_ohne_csrf(self):
        r = self.c.post(f'/api/studio/{self.vorlage.id}/rollen-vorschlag', json={})
        self.assertIn(r.status_code, (400, 403))

    def test_kaputter_rumpf(self):
        r = self.c.post(f'/api/studio/{self.vorlage.id}/rollen-vorschlag',
                        json=[1, 2], headers=self.H)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.vorschlag(speichern='vielleicht').status_code, 400)

    def test_vorlage_ohne_textelemente(self):
        js = self.vorschlag(config={'canvas': {'width': 1080, 'height': 1080},
                                    'elements': []}).get_json() or {}
        self.assertEqual(js['vorschlaege'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
