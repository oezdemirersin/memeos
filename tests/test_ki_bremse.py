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

    def test_schaetzung_rechnet_wie_die_abrechnung(self):
        """Eine Schätzung, die anders rechnet als die Buchung, ist eine zweite Wahrheit.
        Die erste Fassung lag um den Faktor 5000 daneben – darum wird hier gegen die
        Funktion geprüft, die tatsächlich bucht, UND gegen eine feste Größenordnung."""
        from app import _ai_cost, AI_MODEL_ANTHROPIC
        s = K.schaetzung(20, eingabe_tokens=2000, ausgabe_tokens=600)
        echt = 20 * _ai_cost(AI_MODEL_ANTHROPIC, 2000, 600)
        self.assertAlmostEqual(s['kosten_eur'], echt, places=5)
        # Größenordnung festnageln: 20 kleine Aufrufe kosten Cent, nicht Hunderte Euro.
        self.assertLess(s['kosten_eur'], 0.20, s)
        self.assertGreater(s['kosten_eur'], 0.0)
        s2 = K.schaetzung(40, eingabe_tokens=2000, ausgabe_tokens=600)
        self.assertAlmostEqual(s2['kosten_eur'], 2 * s['kosten_eur'], places=4)
        self.assertEqual(K.schaetzung(0)['kosten_eur'], 0)

    def test_schaetzung_nimmt_das_modell_das_wirklich_laeuft(self):
        from app import AI_MODEL_ANTHROPIC
        self.assertEqual(K.schaetzung(1)['modell'], AI_MODEL_ANTHROPIC)
        teuer = K.schaetzung(1, modell='claude-opus-5')
        guenstig = K.schaetzung(1, modell='claude-haiku-4-5')
        self.assertGreater(teuer['kosten_eur'], guenstig['kosten_eur'])

    def test_pruefen_haelt_die_reihenfolge_ein(self):
        """Der Notaus darf nicht erst eine Datenbankabfrage kosten."""
        self.setz('notaus', '1')
        self.setz('tag', '0.01')
        self.kosten(9.99)
        self.assertIn('Notaus', K.pruefen())


class TestAbrechnungsfenster(unittest.TestCase):
    """Bremse und Kostenseite müssen dasselbe Fenster benutzen."""

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
        from models import AiUsageLog
        self.AiUsageLog = AiUsageLog
        AiUsageLog.query.delete()
        self.db.session.commit()

    tearDown = setUp

    def kosten(self, eur, wann):
        self.db.session.add(self.AiUsageLog(
            feature='t', model='claude-haiku-4-5', input_tokens=1, output_tokens=1,
            cost_eur=float(eur), created_at=wann))
        self.db.session.commit()

    def test_bremse_und_kostenseite_zeigen_dasselbe(self):
        from datetime import datetime, timedelta
        jetzt = datetime.utcnow()
        monatsanfang = jetzt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        self.kosten(1.00, monatsanfang + timedelta(hours=1))
        # Eine Zeile aus dem Vormonat darf nicht mitzählen.
        self.kosten(9.99, monatsanfang - timedelta(hours=1))
        # Und eine mit einem Zeitstempel in der Zukunft auch nicht.
        self.kosten(5.00, monatsanfang + timedelta(days=40))
        self.assertAlmostEqual(self.A._ai_cost_this_month(), 1.00, places=4)

    def test_kurs_bremst_eher_zu_frueh_als_zu_spaet(self):
        # Anthropic rechnet in USD. Ein zu niedriger Kurs liesse die Bremse zu spät
        # greifen – deshalb mindestens Parität.
        self.assertGreaterEqual(self.A._USD_EUR, 1.0)

    def test_unbekanntes_modell_kostet_den_teuersten_satz(self):
        teuerste = max(p['output'] for p in self.A.MODEL_PRICES.values())
        self.assertGreaterEqual(self.A.MODEL_PRICE_DEFAULT['output'], teuerste,
                                'ein unbekanntes Modell wird zu billig gerechnet')


if __name__ == '__main__':
    unittest.main(verbosity=2)
