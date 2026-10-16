# -*- coding: utf-8 -*-
"""Tests für die nächtliche Datensicherung (scheduler.job_sicherung).

Eine Sicherung, die man nicht geprüft hat, ist keine. Zwei Arten, still zu versagen:

* Die Datenbank wird kopiert statt gesichert. Im WAL-Modus steht der zuletzt geschriebene
  Teil noch in memeos.db-wal – die Kopie ist dann ein Stand von vorgestern.
* Das Abräumen greift zu weit und nimmt Dateien mit, die ihm nicht gehören.

    python3 -m pytest tests/test_sicherung.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.update({k: '' for k in (
    'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
    'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
    'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN')})
os.environ.setdefault('MEMEOS_SCHEDULER', '0')
os.environ.setdefault('MEMEOS_WORKERS', '0')
os.environ.setdefault('MEMEOS_OFFLINE', '1')

import scheduler as S            # noqa: E402


def _quelldatenbank(pfad, wert, wal_offen=False):
    """Eine Datenbank im WAL-Modus. Mit wal_offen=True bleibt der letzte Schreibvorgang
    im -wal stehen; die Verbindung wird zurückgegeben und muss offen bleiben, sonst
    arbeitet SQLite beim Schliessen das -wal doch noch ein."""
    c = sqlite3.connect(pfad)
    c.execute('pragma journal_mode=wal')
    if wal_offen:
        c.execute('pragma wal_autocheckpoint=0')
    c.execute('create table if not exists stadt (name text)')
    c.execute('delete from stadt')
    c.execute('insert into stadt values (?)', (wert,))
    c.commit()
    if wal_offen:
        return c
    c.execute('pragma wal_checkpoint(truncate)')
    c.commit()
    c.close()
    return pfad


def _lies(pfad):
    c = sqlite3.connect(pfad)
    try:
        return c.execute('select name from stadt').fetchone()[0]
    finally:
        c.close()


class TestSicherungsJob(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='sicherung_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.exports = os.path.join(self.tmp, 'exports')
        os.makedirs(self.exports)
        self.quelle = os.path.join(self.tmp, 'memeos.db')
        self.vermerkt = {}

        attrappe = types.SimpleNamespace(
            _EXPORT_DIR=self.exports,
            _sqlite_datei=lambda: self.quelle if os.path.exists(self.quelle) else None)
        self._alt_appmod, self._alt_set = S._appmod, S._set
        S._appmod = lambda: attrappe
        S._set = lambda k, v: self.vermerkt.__setitem__(k, v)
        self.addCleanup(self._zurueck)

    def _zurueck(self):
        S._appmod, S._set = self._alt_appmod, self._alt_set

    def _ordner(self):
        return os.path.join(self.exports, S.SICHERUNG_ORDNER)

    def _sicherungen(self):
        o = self._ordner()
        return sorted(n for n in os.listdir(o)) if os.path.isdir(o) else []

    def test_nimmt_den_stand_aus_dem_wal_mit(self):
        """Der schärfste Test: der letzte Schreibvorgang steht nur im -wal.

        Ein Kopieren der Datei brächte hier 'Darmstadt' mit statt 'Braunschweig'."""
        _quelldatenbank(self.quelle, 'Darmstadt')
        offen = _quelldatenbank(self.quelle, 'Braunschweig', wal_offen=True)
        self.addCleanup(offen.close)
        self.assertGreater(os.path.getsize(self.quelle + '-wal'), 0, 'Aufbau misslungen')

        ergebnis = S.job_sicherung()

        dateien = self._sicherungen()
        self.assertEqual(len(dateien), 1, f'{ergebnis}')
        self.assertEqual(_lies(os.path.join(self._ordner(), dateien[0])), 'Braunschweig')

    def test_es_bleiben_die_sieben_juengsten(self):
        _quelldatenbank(self.quelle, 'Mainz')
        o = self._ordner()
        os.makedirs(o, exist_ok=True)
        for tag in range(1, 11):
            with open(os.path.join(o, f'memeos_202601{tag:02d}_0330.db'), 'w') as fh:
                fh.write('alt')

        S.job_sicherung()

        dateien = self._sicherungen()
        self.assertEqual(len(dateien), S.SICHERUNG_BEHALTEN,
                         f'es sollten {S.SICHERUNG_BEHALTEN} uebrig sein, da sind: {dateien}')
        self.assertNotIn('memeos_20260101_0330.db', dateien, 'die aelteste liegt noch da')
        self.assertIn('memeos_20260110_0330.db', dateien, 'eine junge wurde weggeraeumt')

    def test_fremde_dateien_werden_nicht_angefasst(self):
        """Abgeräumt wird nur, was dieser Job selbst angelegt hat."""
        _quelldatenbank(self.quelle, 'Kassel')
        o = self._ordner()
        os.makedirs(o, exist_ok=True)
        fremd = ['vorrat_export.zip', 'notizen.txt', 'handgemachte_sicherung.db',
                 'memeos_wichtig.txt']
        for n in fremd:
            with open(os.path.join(o, n), 'w') as fh:
                fh.write('x')
        for tag in range(1, 11):
            with open(os.path.join(o, f'memeos_202601{tag:02d}_0330.db'), 'w') as fh:
                fh.write('alt')

        S.job_sicherung()

        da = self._sicherungen()
        for n in fremd:
            self.assertIn(n, da, f'{n} wurde weggeraeumt, obwohl es dem Job nicht gehoert')

    def test_zeitpunkt_wird_vermerkt(self):
        """Ohne diesen Vermerk meldet der Selbsttest weiter „keine Sicherung eingerichtet"."""
        _quelldatenbank(self.quelle, 'Trier')
        S.job_sicherung()
        self.assertIn('last_backup_at', self.vermerkt)
        self.assertTrue(self.vermerkt['last_backup_at'])

    def test_fehlschlag_hinterlaesst_keine_halbe_sicherung(self):
        """Eine abgebrochene Sicherung darf nicht als vollwertige mitzaehlen."""
        with open(self.quelle, 'w') as fh:
            fh.write('das ist keine Datenbank')

        ergebnis = S.job_sicherung()

        self.assertTrue(ergebnis.startswith('Fehler'), ergebnis)
        self.assertEqual([n for n in self._sicherungen() if n.endswith('.db')], [],
                         'es liegt eine unvollstaendige Sicherung herum')
        self.assertEqual([n for n in self._sicherungen() if n.endswith('.teil')], [],
                         'die Zwischendatei wurde nicht aufgeraeumt')
        self.assertNotIn('last_backup_at', self.vermerkt,
                         'ein Fehlschlag darf nicht als erfolgte Sicherung vermerkt werden')

    def test_abgebrochene_sicherung_sieht_nicht_wie_eine_fertige_aus(self):
        """Die Zwischendatei ist kein Schmuck: stirbt der Prozess mitten im Schreiben,
        darf das Bruchstück nicht unter dem Namen einer fertigen Sicherung liegen –
        sonst zählt es beim Abräumen als vollwertig mit und verdrängt eine echte.

        Der Abbruch wird hier an der Stelle nachgestellt, an der die fertige Kopie
        ihren endgültigen Namen bekommt, und zwar mit KeyboardInterrupt: das ist das,
        was ein abgewürgter Prozess auslöst, und es läuft an einem `except Exception`
        vorbei – genau wie ein echter Abbruch an den Aufräumzeilen vorbeiliefe.
        """
        _quelldatenbank(self.quelle, 'Aachen')
        echt = os.replace

        def abbruch(*a, **k):
            raise KeyboardInterrupt('Prozess abgewuergt')

        os.replace = abbruch
        self.addCleanup(lambda: setattr(os, 'replace', echt))
        with self.assertRaises(KeyboardInterrupt):
            S.job_sicherung()
        os.replace = echt

        fertige = [n for n in self._sicherungen() if n.endswith('.db')]
        self.assertEqual(fertige, [],
                         f'das Bruchstueck liegt als fertige Sicherung herum: {fertige}')

    def test_ohne_sqlite_datenbank_kein_fehler(self):
        os.remove(self.quelle) if os.path.exists(self.quelle) else None
        ergebnis = S.job_sicherung()
        self.assertIn('SQLite', ergebnis)
        self.assertEqual(self._sicherungen(), [])


class TestZeitstempelKommtAn(unittest.TestCase):
    """Der Vermerk des Jobs muss von der Stelle lesbar sein, die ihn auswertet.

    Beim ersten Lauf auf dem Probe-Deploy war er das nicht: der Job legt den Zeitpunkt
    mit Berliner Offset ab, der Selbsttest rechnete ihn gegen eine Zeit ohne Zone –
    TypeError, und statt „letzte Sicherung: …" stand dort „Check abgestürzt"."""

    def test_selbsttest_liest_den_vermerk_mit_zeitzone(self):
        import selftest_bp as ST
        text = ST._lesbares_datum(S.now_berlin().isoformat())
        self.assertIn('(heute)', text)
        self.assertNotIn('abgest', text)

    def test_selbsttest_liest_auch_vermerke_ohne_zeitzone(self):
        """Ältere Schlüssel stehen ohne Offset – die dürfen nicht kaputtgehen."""
        import selftest_bp as ST
        from datetime import datetime
        text = ST._lesbares_datum(datetime.utcnow().isoformat())
        self.assertIn('(heute)', text)


class TestImJobRegister(unittest.TestCase):
    """Ein Job, der nirgends eingetragen ist, läuft nie."""

    def test_sicherung_ist_ein_taeglicher_job(self):
        self.assertIn('sicherung', S.JOB_BY_KEY)
        self.assertIn('sicherung', S.DAILY_JOBS)
        self.assertIn('sicherung', S._JOB_FUNCS)

    def test_sicherung_kostet_keine_ki_und_braucht_kein_telegram(self):
        j = S.JOB_BY_KEY['sicherung']
        self.assertFalse(j['costs_ai'], 'sonst haelt die Kostenbremse die Sicherung an')
        self.assertFalse(j['needs_telegram'])
        self.assertTrue(j['default_enabled'], 'eine Sicherung, die man erst einschalten '
                                              'muss, ist bei den meisten nie an')


if __name__ == '__main__':
    unittest.main()
