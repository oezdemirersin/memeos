# -*- coding: utf-8 -*-
"""Tests für das Aufräumen im Render-Ordner.

Eine Löschroutine, die zu viel greift, kostet Bilder offener Beiträge. Diese Tests legen
genau die Fälle an, in denen sie das tun könnte, und schauen ihr auf die Finger.

    python3 -m pytest tests/test_aufraeumen.py
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.update({k: '' for k in (
    'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
    'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
    'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN')})
os.environ.update({'MEMEOS_SCHEDULER': '0', 'MEMEOS_WORKERS': '0', 'MEMEOS_OFFLINE': '1',
                   'DATABASE_URL': 'sqlite://'})

import app as A              # noqa: E402
import render_queue as RQ    # noqa: E402
from models import db, City, MemePost   # noqa: E402


class TestOrdnerAufraeumen(unittest.TestCase):
    """cleanup_orphan_renders darf nur die eigenen Erzeugnisse anfassen."""

    @classmethod
    def setUpClass(cls):
        assert A.app.config['SQLALCHEMY_DATABASE_URI'] in ('sqlite://',) or \
            ':memory:' in A.app.config['SQLALCHEMY_DATABASE_URI'], \
            'Test würde gegen eine echte Datenbank laufen'
        cls.ctx = A.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.stadt = City(name='Musterstadt', state='Hessen')
        db.session.add(cls.stadt)
        db.session.commit()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='renders_')
        self._echt = RQ._render_dir
        RQ._render_dir = lambda: self.tmp
        MemePost.query.delete()
        db.session.commit()

    def tearDown(self):
        RQ._render_dir = self._echt
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def datei(self, name, alter_tage=100):
        pfad = os.path.join(self.tmp, name)
        with open(pfad, 'wb') as f:
            f.write(b'x' * 32)
        wann = time.time() - alter_tage * 86400
        os.utime(pfad, (wann, wann))
        return name

    def vorhanden(self):
        return set(os.listdir(self.tmp))

    # ── Was weg darf ─────────────────────────────────────────────────────────
    def test_wirklich_verwaiste_datei_verschwindet(self):
        self.datei('render_alt.png')
        weg = RQ.cleanup_orphan_renders(max_age_days=14)
        self.assertEqual(weg, ['render_alt.png'])
        self.assertNotIn('render_alt.png', self.vorhanden())

    def test_junge_dateien_bleiben(self):
        self.datei('frisch.png', alter_tage=1)
        self.assertEqual(RQ.cleanup_orphan_renders(max_age_days=14), [])
        self.assertIn('frisch.png', self.vorhanden())

    # ── Was bleiben MUSS ─────────────────────────────────────────────────────
    def test_bild_eines_beitrags_bleibt(self):
        self.datei('benutzt.png')
        db.session.add(MemePost(city_id=self.stadt.id, title='Probe', post_type='feed',
                                status='entwurf', image_path='benutzt.png',
                                image_url='/renders/benutzt.png'))
        db.session.commit()
        self.assertEqual(RQ.cleanup_orphan_renders(max_age_days=14), [])
        self.assertIn('benutzt.png', self.vorhanden())

    def test_beitrag_nur_mit_image_url_bleibt(self):
        """POST /api/vorrat legt Beiträge an, bei denen allein image_url gefüllt ist.
        Zählte nur image_path, wäre deren Bild nach zwei Wochen weg."""
        self.datei('nur_url.png')
        db.session.add(MemePost(city_id=self.stadt.id, title='Nur URL', post_type='feed',
                                status='entwurf', image_url='/renders/nur_url.png'))
        db.session.commit()
        self.assertEqual(RQ.cleanup_orphan_renders(max_age_days=14), [])
        self.assertIn('nur_url.png', self.vorhanden())

    def test_externe_bild_url_stoert_nicht(self):
        """Zeigt image_url auf Cloudinary, darf der Ordner trotzdem aufgeräumt werden."""
        self.datei('verwaist.png')
        db.session.add(MemePost(city_id=self.stadt.id, title='Extern', post_type='feed',
                                status='entwurf',
                                image_url='https://res.cloudinary.com/x/bild.png'))
        db.session.commit()
        self.assertEqual(RQ.cleanup_orphan_renders(max_age_days=14), ['verwaist.png'])

    def test_karussell_folien_bleiben(self):
        """Der teuerste Fehler: nur die erste Folie steht in image_url, die anderen vier
        hängen in carousel_paths. Wer nur image_url prüft, löscht vier Fünftel des Beitrags."""
        import json
        folien = [self.datei(f'slide_{i}.png') for i in range(1, 6)]
        db.session.add(MemePost(city_id=self.stadt.id, title='Karussell', post_type='carousel',
                                status='entwurf', image_url='/renders/slide_1.png',
                                carousel_paths=json.dumps(folien)))
        db.session.commit()
        self.assertEqual(RQ.cleanup_orphan_renders(max_age_days=14), [])
        self.assertTrue(set(folien) <= self.vorhanden(), self.vorhanden())

    def test_gitkeep_bleibt(self):
        """'.gitkeep' hält den Ordner im Repository – es ist kein Render-Erzeugnis."""
        self.datei('.gitkeep')
        self.assertEqual(RQ.cleanup_orphan_renders(max_age_days=0), [])
        self.assertIn('.gitkeep', self.vorhanden())

    def test_fremde_dateien_bleiben(self):
        for name in ('notizen.txt', 'bericht.pdf', '.DS_Store', 'skript.py'):
            self.datei(name)
        self.assertEqual(RQ.cleanup_orphan_renders(max_age_days=0), [])
        self.assertEqual(len(self.vorhanden()), 4)

    def test_unterordner_bleiben_unberuehrt(self):
        os.makedirs(os.path.join(self.tmp, 'archiv'))
        self.datei(os.path.join('archiv', 'alt.png'))
        self.assertEqual(RQ.cleanup_orphan_renders(max_age_days=0), [])
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, 'archiv')))

    def test_trockenlauf_loescht_nichts(self):
        self.datei('weg.png')
        gefunden = RQ.cleanup_orphan_renders(max_age_days=14, dry_run=True)
        self.assertEqual(gefunden, ['weg.png'])
        self.assertIn('weg.png', self.vorhanden())

    def test_kaputte_carousel_paths_stoppen_alles(self):
        """Sicherheitsnetz: ist auch nur ein Eintrag unlesbar, wird gar nichts gelöscht."""
        self.datei('irgendwas.png')
        db.session.add(MemePost(city_id=self.stadt.id, title='Kaputt', post_type='carousel',
                                status='entwurf', carousel_paths='{kein json'))
        db.session.commit()
        self.assertEqual(RQ.cleanup_orphan_renders(max_age_days=14), [])
        self.assertIn('irgendwas.png', self.vorhanden())

    def test_grossgeschriebene_endung_zaehlt_auch(self):
        self.datei('ALT.PNG')
        self.assertEqual(RQ.cleanup_orphan_renders(max_age_days=14), ['ALT.PNG'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
