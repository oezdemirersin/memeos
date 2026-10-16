# -*- coding: utf-8 -*-
"""Tests für die Kostenbremsen.

Der Betreiber will seinen Anthropic-Schlüssel erst eintragen, wenn er sicher ist, dass ein
Fehler sein Konto nicht leeren kann. Diese Tests sind die Antwort darauf: sie prüfen, dass
jede der vier Grenzen allein stoppt, und zwar BEVOR ein Aufruf hinausgeht.

    python3 -m pytest tests/test_ki_bremse.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.update({k: '' for k in (
    'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
    'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
    'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN')})
os.environ.setdefault('MEMEOS_SCHEDULER', '0')
os.environ.setdefault('MEMEOS_WORKERS', '0')
os.environ.setdefault('MEMEOS_OFFLINE', '1')
os.environ.setdefault('DATABASE_URL', 'sqlite://')

import ki_bremse as K        # noqa: E402


class TestVorgaben(unittest.TestCase):
    """Ohne jede Einstellung muss trotzdem eine Grenze gelten."""

    @classmethod
    def setUpClass(cls):
        import app as A
        from models import db
        uri = A.app.config['SQLALCHEMY_DATABASE_URI']
        assert uri == 'sqlite://' or ':memory:' in uri, f'echte Datenbank: {uri}'
        cls.A, cls.db = A, db
        cls.ctx = A.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        from models import AppSettings, AiUsageLog
        self.AppSettings, self.AiUsageLog = AppSettings, AiUsageLog
        for name in K.SCHLUESSEL.values():
            zeile = AppSettings.query.filter_by(key=name).first()
            if zeile is not None:
                self.db.session.delete(zeile)
        AiUsageLog.query.delete()
        self.db.session.commit()
        K.zaehler_zuruecksetzen()

    def tearDown(self):
        self.setUp()

    def setz(self, name, wert):
        self.AppSettings.set(K.SCHLUESSEL[name], str(wert))
        self.db.session.commit()

    def kosten(self, eur, wann=None):
        self.db.session.add(self.AiUsageLog(
            feature='test', model='claude-haiku-4-5', input_tokens=1, output_tokens=1,
            cost_eur=float(eur), created_at=wann or datetime.utcnow()))
        self.db.session.commit()

    # ── Die wichtigste Zusicherung überhaupt ─────────────────────────────
    def test_ohne_einstellung_gilt_trotzdem_eine_grenze(self):
        g = K.grenzen()
        self.assertGreater(g['monat_eur'], 0, 'Monatsbudget unbegrenzt')
        self.assertGreater(g['tag_eur'], 0, 'Tagesbudget unbegrenzt')
        self.assertGreater(g['stunde_aufrufe'], 0, 'Stundengrenze unbegrenzt')
        self.assertFalse(g['notaus'])

    def test_frische_anlage_laesst_aufrufe_zu(self):
        self.assertIsNone(K.pruefen(), 'eine leere Anlage darf nicht gesperrt starten')

    # ── Jede Grenze allein ───────────────────────────────────────────────
    def test_notaus_stoppt_sofort(self):
        self.setz('notaus', '1')
        grund = K.pruefen()
        self.assertIsNotNone(grund)
        self.assertIn('Notaus', grund)

    def test_tagesbudget_stoppt(self):
        self.setz('tag', '1.00')
        self.assertIsNone(K.pruefen())
        self.kosten(1.00)
        grund = K.pruefen()
        self.assertIsNotNone(grund)
        self.assertIn('Tagesbudget', grund)

    def test_tagesbudget_zaehlt_gestern_nicht_mit(self):
        self.setz('tag', '1.00')
        self.kosten(5.00, datetime.utcnow() - timedelta(days=1, hours=1))
        self.assertIsNone(K.pruefen(), 'Kosten von gestern sperren heute')

    def test_monatsbudget_stoppt(self):
        self.setz('tag', '0')          # Tagesgrenze ausdrücklich aus
        self.setz('monat', '2.00')
        self.kosten(2.50, datetime.utcnow().replace(day=1, hour=1))
        grund = K.pruefen()
        self.assertIsNotNone(grund)
        self.assertIn('Monatsbudget', grund)

    def test_stundengrenze_stoppt_eine_schleife(self):
        """Die eigentliche Sicherung gegen einen Fehler: ein Monatsbudget merkt nichts
        davon, dass hundert Aufrufe in zehn Minuten hinausgehen."""
        self.setz('stunde', '5')
        for i in range(5):
            self.assertIsNone(K.pruefen(), f'schon bei Aufruf {i} gesperrt')
            K.aufruf_buchen('test')
        grund = K.pruefen()
        self.assertIsNotNone(grund, 'Stundengrenze greift nicht')
        self.assertIn('Stundengrenze', grund)

    def test_stundengrenze_loest_sich_wieder(self):
        self.setz('stunde', '2')
        K.aufruf_buchen(); K.aufruf_buchen()
        self.assertIsNotNone(K.pruefen())
        # Eine Stunde später zählen die alten Aufrufe nicht mehr mit.
        with K._sperre:
            K._aufrufe.clear()
            K._aufrufe.append(datetime.utcnow() - timedelta(hours=2))
            K._aufrufe.append(datetime.utcnow() - timedelta(hours=2))
        self.assertIsNone(K.pruefen())

    # ── Einstellungen, die Unsinn enthalten ──────────────────────────────
    def test_unsinn_faellt_auf_die_vorgabe_zurueck(self):
        for wert in ('viel', '', '   ', 'NaN', 'nan', 'inf', '-inf', 'Infinity', '-5'):
            self.setz('tag', wert)
            with self.subTest(wert=wert):
                self.assertEqual(K.grenzen()['tag_eur'], K.VORGABE_TAG_EUR,
                                 f'{wert!r} schaltet die Grenze ab')

    def test_komma_statt_punkt(self):
        self.setz('tag', '1,50')
        self.assertAlmostEqual(K.grenzen()['tag_eur'], 1.50)

    def test_tippfehler_wird_gedeckelt(self):
        # „1000" statt „10" soll nicht alle Bremsen aushebeln.
        self.setz('tag', '99999')
        self.assertLessEqual(K.grenzen()['tag_eur'], K.MAX_TAG_EUR)

    def test_null_heisst_unbegrenzt_und_wird_gemeldet(self):
        self.setz('tag', '0')
        self.assertEqual(K.grenzen()['tag_eur'], 0.0)
        warnungen = K.auskunft()['warnungen']
        self.assertTrue(any('Tagesbudget' in w for w in warnungen), warnungen)

    # ── Auskunft ─────────────────────────────────────────────────────────
    def test_auskunft_ist_vollstaendig(self):
        self.setz('tag', '3.00')
        self.kosten(1.20)
        a = K.auskunft()
        self.assertAlmostEqual(a['tag']['verbraucht_eur'], 1.20, places=4)
        self.assertAlmostEqual(a['tag']['rest_eur'], 1.80, places=4)
        self.assertFalse(a['gesperrt'])
        self.assertIsNone(a['grund'])
        for feld in ('notaus', 'monat', 'tag', 'stunde', 'warnungen', 'vorgaben'):
            self.assertIn(feld, a)

    def test_schaetzung_rechnet_mit_echten_preisen(self):
        s = K.schaetzung(100, eingabe_tokens=2000, ausgabe_tokens=600)
        self.assertEqual(s['aufrufe'], 100)
        self.assertGreater(s['kosten_eur'], 0)
        # Doppelt so viele Aufrufe kosten doppelt so viel.
        s2 = K.schaetzung(200, eingabe_tokens=2000, ausgabe_tokens=600)
        self.assertAlmostEqual(s2['kosten_eur'], 2 * s['kosten_eur'], places=3)
        self.assertEqual(K.schaetzung(0)['kosten_eur'], 0)

    def test_pruefen_haelt_die_reihenfolge_ein(self):
        """Der Notaus darf nicht erst eine Datenbankabfrage kosten."""
        self.setz('notaus', '1')
        self.setz('tag', '0.01')
        self.kosten(9.99)
        self.assertIn('Notaus', K.pruefen())


if __name__ == '__main__':
    unittest.main(verbosity=2)
