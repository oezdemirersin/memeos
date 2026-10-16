# -*- coding: utf-8 -*-
"""Die Kostenbremse an den echten Aufrufwegen – mit einer Attrappe statt des SDK.

Diese Tests sind die Antwort auf die Frage „kann ein Fehler mein Konto leeren?". Sie
zählen Aufrufe, statt Code zu lesen. Kein einziger geht ins Netz.

    python3 -m pytest tests/test_kostenbremse_wege.py
"""
import os
import sys
import threading
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.update({k: '' for k in (
    'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
    'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
    'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN')})
os.environ.setdefault('MEMEOS_SCHEDULER', '0')
os.environ.setdefault('MEMEOS_WORKERS', '0')
os.environ.setdefault('MEMEOS_OFFLINE', '1')
os.environ.setdefault('DATABASE_URL', 'sqlite://')

import ki_bremse as K          # noqa: E402


class Attrappe:
    """Ein SDK-Ersatz, der mitzählt und nie ins Netz geht."""

    def __init__(self, ein=1000, aus=300, wirft=None, nach_antwort=None):
        self.aufrufe = 0
        self.ein, self.aus = ein, aus
        self.wirft, self.nach_antwort = wirft, nach_antwort
        self.messages = self

    def __call__(self, *a, **kw):
        return self

    def create(self, **kw):
        self.aufrufe += 1
        if self.wirft:
            raise self.wirft
        if self.nach_antwort:
            # Das Modell HAT geantwortet und Anthropic hat abgerechnet – erst danach
            # bricht die Verbindung. Genau der teure Fall.
            raise self.nach_antwort
        class Block:
            type, text = 'text', '{"fit_score": 50, "reasoning": "ok", "vars": {}}'
        class Usage:
            input_tokens, output_tokens = self.ein, self.aus
            cache_creation_input_tokens = cache_read_input_tokens = 0
        class Antwort:
            content, usage, stop_reason = [Block()], Usage(), 'end_turn'
        return Antwort()


class BremseTest(unittest.TestCase):

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
        # Der Gleichzeitigkeits-Test arbeitet mit Fäden auf derselben Datenbank im
        # Arbeitsspeicher und lässt die Sitzung in unklarem Zustand zurück. Jeder Test
        # fängt deshalb mit einer frischen an.
        self.db.session.remove()
        self.AppSettings, self.AiUsageLog = AppSettings, AiUsageLog
        for name in K.SCHLUESSEL.values():
            z = AppSettings.query.filter_by(key=name).first()
            if z is not None:
                self.db.session.delete(z)
        AiUsageLog.query.delete()
        self.db.session.commit()
        K.zaehler_zuruecksetzen()
        K.vormerkungen_zuruecksetzen()
        self._key = self.A.ANTHROPIC_API_KEY
        self.A.ANTHROPIC_API_KEY = 'sk-ant-api03-' + 'x' * 80
        self._sdk = self.A.anthropic.Anthropic

    def tearDown(self):
        self.A.ANTHROPIC_API_KEY = self._key
        self.A.anthropic.Anthropic = self._sdk
        try:
            self.db.session.rollback()
        except Exception:
            pass
        self.db.session.remove()

    def setz(self, name, wert):
        self.AppSettings.set(K.SCHLUESSEL[name], str(wert))
        self.db.session.commit()

    def kosten(self, eur):
        self.db.session.add(self.AiUsageLog(
            feature='vorlast', model='claude-haiku-4-5', input_tokens=1, output_tokens=1,
            cost_eur=float(eur), created_at=datetime.utcnow()))
        self.db.session.commit()

    def attrappe(self, **kw):
        a = Attrappe(**kw)
        self.A.anthropic.Anthropic = lambda *x, **y: a
        return a

    # ── Der Notaus hält wirklich alles an ────────────────────────────────
    def test_notaus_laesst_keinen_aufruf_hinaus(self):
        a = self.attrappe()
        self.setz('notaus', '1')
        raw, fehler = self.A._anthropic_complete([], 'probe', 200, 'test')
        self.assertEqual(a.aufrufe, 0, 'trotz Notaus ging ein Aufruf hinaus')
        self.assertIn('Notaus', fehler)

    def test_tagesbudget_haelt_an(self):
        a = self.attrappe()
        self.setz('tag', '0.50')
        self.kosten(0.60)
        raw, fehler = self.A._anthropic_complete([], 'probe', 200, 'test')
        self.assertEqual(a.aufrufe, 0)
        self.assertIn('Tagesbudget', fehler)

    def test_stundengrenze_haelt_eine_schleife_an(self):
        a = self.attrappe()
        self.setz('stunde', '3')
        for _ in range(10):
            self.A._anthropic_complete([], 'probe', 200, 'test')
        self.assertEqual(a.aufrufe, 3, f'{a.aufrufe} Aufrufe trotz Grenze 3')

    # ── Der teuerste Fall: bezahlt, aber nicht gebucht ───────────────────
    def test_abbruch_nach_der_antwort_wird_gebucht(self):
        """Bricht die Verbindung NACH der Antwort ab, ist der Aufruf bezahlt. Wird er mit
        0 verbucht, rückt die Bremse nie vor und dieselbe Schleife läuft weiter."""
        a = self.attrappe(nach_antwort=ConnectionError('Verbindung weg'))
        self.setz('tag', '0')
        self.setz('monat', '0')
        self.setz('stunde', '0')
        for _ in range(5):
            self.A._anthropic_complete([], 'x' * 4000, 800, 'test')
        self.assertEqual(a.aufrufe, 5)
        zeilen = self.AiUsageLog.query.filter(
            self.AiUsageLog.feature.like('%abbruch%')).all()
        self.assertEqual(len(zeilen), 5, 'abgebrochene Aufrufe wurden nicht gebucht')
        self.assertGreater(sum(z.cost_eur for z in zeilen), 0)

    def test_abbruch_treibt_die_bremse_vor(self):
        a = self.attrappe(nach_antwort=ConnectionError('weg'))
        self.setz('tag', '0.05')
        for _ in range(40):
            self.A._anthropic_complete([], 'x' * 8000, 2000, 'test')
        self.assertLess(a.aufrufe, 40,
                        'die Bremse hat trotz gebuchter Abbrüche nie gegriffen')

    def test_dauerhafter_fehler_wird_nicht_gebucht(self):
        """Ein 401 kostet nichts – er darf das Budget nicht belasten."""
        class Abgelehnt(Exception):
            status_code = 401
        Abgelehnt.__name__ = 'AuthenticationError'
        a = self.attrappe(wirft=Abgelehnt('kein Zugang'))
        self.A._anthropic_complete([], 'probe', 200, 'test')
        self.assertEqual(a.aufrufe, 1)
        self.assertEqual(self.AiUsageLog.query.count(), 0,
                         'ein abgelehnter Schlüssel wurde als Kosten gebucht')

    # ── Gleichzeitigkeit ─────────────────────────────────────────────────
    def test_acht_gleichzeitige_laufen_nicht_alle_durch(self):
        """Ohne Reservierung prüfen alle acht, bevor einer gebucht hat – und alle acht
        gehen hinaus. Gemessen wurden dabei 8,82 EUR bei einem 5-EUR-Budget.

        Geprüft wird hier ki_bremse.platz_nehmen() unmittelbar, mit festgehaltenen
        Kostenzahlen statt Datenbankabfragen: acht Fäden auf derselben SQLite im
        Arbeitsspeicher beschädigen deren Anweisungs-Zwischenspeicher und liessen danach
        jedes weitere Testmodul auflaufen. Die Reservierung ist ohnehin reine
        Arbeitsspeicher-Logik – genau sie wird hier gemessen."""
        self.setz('monat', '5.00')
        self.setz('tag', '0')
        self.setz('stunde', '0')
        grenzen = K.grenzen()
        echte_monatskosten, echte_tageskosten = K.kosten_monat, K.kosten_tag
        echte_grenzen = K.grenzen
        K.kosten_monat = lambda: 4.99          # haarscharf unter der Grenze
        K.kosten_tag = lambda: 0.0
        K.grenzen = lambda: grenzen
        try:
            K.zaehler_zuruecksetzen()
            K.vormerkungen_zuruecksetzen()
            start = threading.Barrier(8)
            durch = []
            sperre = threading.Lock()

            def lauf():
                start.wait()
                # 0,50 EUR je Aufruf – zwei davon reissen die Grenze.
                grund, marke = K.platz_nehmen('test', 0.50)
                if grund is None:
                    with sperre:
                        durch.append(marke)

            faeden = [threading.Thread(target=lauf) for _ in range(8)]
            for f in faeden:
                f.start()
            for f in faeden:
                f.join()
            self.assertLessEqual(len(durch), 2,
                                 f'{len(durch)} von 8 Aufrufen gingen trotz 4,99 von '
                                 f'5,00 EUR durch')
            self.assertGreaterEqual(len(durch), 1, 'gar keiner durfte – zu streng')
        finally:
            K.kosten_monat, K.kosten_tag, K.grenzen = (echte_monatskosten,
                                                       echte_tageskosten, echte_grenzen)
            K.vormerkungen_zuruecksetzen()

    def test_vormerkung_wird_wieder_freigegeben(self):
        """Eine Vormerkung, die niemand auflöst, blockiert sonst bis zum Verfall."""
        K.vormerkungen_zuruecksetzen()
        grund, marke = K.platz_nehmen('test', 1.0)
        self.assertIsNone(grund)
        self.assertIsNotNone(marke)
        self.assertGreater(K._vormerkungen_summe(), 0)
        K.abrechnen(marke)
        self.assertEqual(K._vormerkungen_summe(), 0)
        K.abrechnen(None)          # darf nicht krachen
        K.abrechnen(marke)         # doppelt auflösen auch nicht

    def test_jeder_ausgang_loest_die_vormerkung_auf(self):
        """Egal ob Erfolg, Abbruch oder abgelehnter Schlüssel – nach dem Aufruf darf
        nichts reserviert bleiben."""
        for name, kw in (('Erfolg', {}),
                         ('Abbruch', {'nach_antwort': ConnectionError('weg')})):
            with self.subTest(fall=name):
                K.vormerkungen_zuruecksetzen()
                self.attrappe(**kw)
                self.A._anthropic_complete([], 'probe', 200, 'test')
                self.assertEqual(K._vormerkungen_summe(), 0,
                                 f'{name}: Vormerkung blieb stehen')

    # ── Der Trending-Job ─────────────────────────────────────────────────
    def test_trending_job_respektiert_die_bremse(self):
        import scheduler as S
        from models import City
        a = self.attrappe()
        for i in range(5):
            self.db.session.add(City(name=f'Bremsstadt {i}', state='Hessen',
                                     rss_url='https://example.invalid/feed'))
        self.db.session.commit()
        self.setz('notaus', '1')
        ergebnis = S.job_trending()
        self.assertEqual(a.aufrufe, 0, f'{a.aufrufe} Aufrufe trotz Notaus: {ergebnis}')
        self.assertIn('übersprungen', ergebnis)
        for c in City.query.filter(City.name.like('Bremsstadt%')).all():
            self.db.session.delete(c)
        self.db.session.commit()


class BedienungTest(BremseTest):
    """Kann der Betreiber der Bremse trauen, ohne den Quelltext zu lesen?"""

    def client(self):
        self.A.app.config['TESTING'] = True
        c = self.A.app.test_client()
        with c.session_transaction() as s:
            s['logged_in'] = True
            s['csrf_token'] = 'tt'
        return c, {'X-CSRF-Token': 'tt'}

    def test_notaus_ist_ueber_die_einstellungen_schaltbar(self):
        c, H = self.client()
        r = c.post('/api/settings', json={'ki_aus': '1'}, headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(K.notaus_an())
        self.assertIsNotNone(K.pruefen())
        r = c.post('/api/settings', json={'ki_aus': '0'}, headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(K.notaus_an())

    def test_budget_laesst_sich_nicht_lautlos_abschalten(self):
        c, H = self.client()
        for wert, grund in (('-3', 'negativ'), ('0,002', 'unter einem Cent'),
                            ('inf', 'unendlich'), ('', 'leer'), ('viel', 'keine Zahl')):
            with self.subTest(wert=wert):
                r = c.post('/api/settings', json={'ai_budget_month_eur': wert}, headers=H)
                self.assertEqual(r.status_code, 400, f'{wert!r} ({grund}) ging durch: '
                                                     f'{r.get_json()}')
        # Eine ausdrückliche 0 ist erlaubt – sie ist eine Entscheidung.
        self.assertEqual(c.post('/api/settings', json={'ai_budget_month_eur': '0'},
                                headers=H).status_code, 200)

    def test_tages_und_stundengrenze_sind_einstellbar(self):
        c, H = self.client()
        r = c.post('/api/settings',
                   json={'ki_budget_tag_eur': '3,50', 'ki_aufrufe_pro_stunde': '40'},
                   headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        g = K.grenzen()
        self.assertAlmostEqual(g['tag_eur'], 3.50)
        self.assertEqual(g['stunde_aufrufe'], 40)
        for feld, wert in (('ki_budget_tag_eur', '-1'), ('ki_budget_tag_eur', 'nan'),
                           ('ki_aufrufe_pro_stunde', '999999')):
            with self.subTest(feld=feld, wert=wert):
                self.assertEqual(c.post('/api/settings', json={feld: wert},
                                        headers=H).status_code, 400)

    def test_auskunft_nennt_rest_und_schaetzung(self):
        c, H = self.client()
        c.post('/api/settings', json={'ai_budget_month_eur': '10,00',
                                      'ki_budget_tag_eur': '4,00'}, headers=H)
        js = c.get('/api/ki-bremse?aufrufe=100').get_json() or {}
        self.assertIn('schaetzung', js)
        self.assertGreater(js['schaetzung']['kosten_eur'], 0)
        self.assertIn('hinweis', js['schaetzung'])
        self.assertIsNotNone(js['tag']['rest_eur'])
        self.assertIn('reicht_heute', js)

    def test_anzeiger_wird_rot_wenn_die_bremse_greift(self):
        c, H = self.client()
        self.assertEqual(self.A._ki_stand_kurz()['farbe'], 'green')
        c.post('/api/settings', json={'ki_aus': '1'}, headers=H)
        stand = self.A._ki_stand_kurz()
        self.assertEqual(stand['farbe'], 'red', stand)
        self.assertIn('gestoppt', stand['text'])

    def test_anzeiger_warnt_wenn_eine_bremse_aus_ist(self):
        c, H = self.client()
        c.post('/api/settings', json={'ki_budget_tag_eur': '0'}, headers=H)
        stand = self.A._ki_stand_kurz()
        self.assertEqual(stand['farbe'], 'yellow', stand)
        self.assertIn('Bremse teils aus', stand['text'])

    def test_stapel_wird_abgelehnt_statt_gruen_gemeldet(self):
        """Ein Stapel, den die Bremse ohnehin sofort stoppt, darf nicht als Erfolg
        gemeldet werden – vorher hiess es „20 Aufträge in der Queue", und alle zwanzig
        scheiterten danach einzeln."""
        from models import City, MemeTemplate
        c, H = self.client()
        stadt = City(name='Stapelstadt', state='Hessen')
        vorlage = MemeTemplate(name='Stapelvorlage', render_type='pil',
                               pil_config='{"elements": []}', required_vars='[]',
                               active=True)
        self.db.session.add_all([stadt, vorlage])
        self.db.session.commit()
        try:
            c.post('/api/settings', json={'ki_aus': '1'}, headers=H)
            r = c.post('/api/render/batch',
                       json={'template_id': vorlage.id, 'city_ids': [stadt.id]},
                       headers=H)
            self.assertEqual(r.status_code, 409, r.get_json())
            js = r.get_json() or {}
            self.assertEqual(js.get('created'), 0)
            self.assertIn('Notaus', js.get('error', ''))
        finally:
            c.post('/api/settings', json={'ki_aus': '0'}, headers=H)
            self.db.session.delete(stadt)
            self.db.session.delete(vorlage)
            self.db.session.commit()


if __name__ == '__main__':
    unittest.main(verbosity=2)
