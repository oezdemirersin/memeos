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
        # Gleiche Farbe, nur in Kleinschreibung – das Modul sichert Normalisierung zu.
        for marke, logo in MARKEN:
            self.assertEqual(H.palette(marke, logo)['primary'], marke.lower())
            self.assertEqual(H.hex_rgb(H.palette(marke, logo)['primary']),
                             H.hex_rgb(marke))

    def test_schreibweise_wird_vereinheitlicht(self):
        # '#FFD700' und '#ffd700' sind dieselbe Farbe und müssen dieselbe Palette ergeben,
        # sonst sehen zwei Städte verschieden aus, weil jemand Großbuchstaben getippt hat.
        self.assertEqual(H.palette('#FFD700'), H.palette('#ffd700'))
        self.assertEqual(H.palette('  #FFD700  ')['primary'], '#ffd700')

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

    def test_der_dunkle_grund_traegt_IMMER_helle_schrift(self):
        """Das ist die eine Zusicherung, die über den ganzen Farbkreis halten muss – auch
        für Marken, die niemand vorhergesehen hat. Feste Zahl 7,0, nicht die Konstante des
        geprüften Moduls."""
        schlimmste = (99.0, None)
        for i in range(36):
            for hell in (0.10, 0.25, 0.45, 0.65, 0.85):
                for sat in (0.0, 0.3, 0.6, 0.95):
                    marke = H._aus_hls(i / 36, hell, sat)
                    p = H.palette(marke)
                    k = H.kontrast(p['light'], p['dark'])
                    if k < schlimmste[0]:
                        schlimmste = (k, marke)
        self.assertGreaterEqual(schlimmste[0], 7.0,
                                f'Marke {schlimmste[1]} ergibt nur {schlimmste[0]:.2f}:1 '
                                f'zwischen hellem Text und dunklem Grund')

    def test_unbunte_marke_bekommt_keinen_erfundenen_farbton(self):
        """Grau, Schwarz und Weiß HABEN keinen Farbton. colorsys liefert dafür 0.0 – das
        ist Rot, aber es ist keine Aussage. Wer daraus Sättigung erzeugt, erfindet der
        Stadt einen Rotstich, den ihr Logo nie hatte."""
        for marke in ('#000000', '#1a1a1a', '#303030', '#808080', '#c0c0c0', '#ffffff'):
            p = H.palette(marke)
            with self.subTest(marke=marke):
                for key in ('secondary', 'dark', 'light', 'accent'):
                    r, g, b = H.hex_rgb(p[key])
                    self.assertEqual((r, g), (g, b) and (r, g),
                                     f'{key}={p[key]} ist nicht neutral')
                    self.assertTrue(r == g == b, f'{key}={p[key]} hat einen Farbstich')

    def test_unbunte_marken_bekommen_nicht_alle_dasselbe(self):
        paletten = {tuple(sorted(H.palette(m).items()))
                    for m in ('#000000', '#1a1a1a', '#303030', '#808080', '#c0c0c0')}
        self.assertEqual(len(paletten), 5, 'zwei unbunte Marken ergeben dieselbe Palette')

    def test_pruefe_palette_sagt_die_wahrheit(self):
        # Eine helle Bestandsmarke hat nichts zu beanstanden …
        self.assertEqual(H.pruefe_palette(H.palette('#3db8ff', '#1a4fcc')), [])
        # … eine sehr dunkle schon: ihre Markenfarbe verschwindet auf dem eigenen Grund.
        hinweise = H.pruefe_palette(H.palette('#101010'))
        self.assertTrue(hinweise)
        self.assertTrue(any('Markenfarbe' in h for h in hinweise), hinweise)
        # Eine unbunte Marke bekommt den Hinweis zur Pointe.
        self.assertTrue(any('unbunt' in h for h in H.pruefe_palette(H.palette('#808080'))))
        self.assertEqual(H.pruefe_palette({}), ['Keine lesbare Markenfarbe.'])

    def test_pointe_knallt_und_verschmilzt_nicht(self):
        for marke, logo in MARKEN:
            p = H.palette(marke, logo)
            with self.subTest(marke=marke):
                # Feste Zahl, NICHT H.POINTE_MIN_KONTRAST: holte der Test seine Schwelle
                # aus dem geprüften Modul, wanderte sie mit und könnte nie fehlschlagen.
                self.assertGreaterEqual(H.kontrast(p['accent'], p['dark']), 2.8)
                # Die Pointe muss auch knallen, nicht nur Kontrast haben.
                sat = H._hls(p['accent'])[2]
                self.assertGreater(sat, 0.65, f"Pointe {p['accent']} ist zu blass")
                # Die Pointe folgt der Helligkeit der Marke (so sind auch die eingebauten
                # Pakete gebaut) – eine feste Helligkeit wäre etwas anderes.
                hell_a, hell_m = H._hls(p['accent'])[1], H._hls(p['primary'])[1]
                erwartet = max(0.42, min(0.60, hell_m))
                self.assertAlmostEqual(hell_a, erwartet, delta=0.03,
                                       msg=f"Pointe {p['accent']} folgt der Marke nicht")
                # deutlich anderer Farbton als die Hauptfarbe, sonst keine eigene Stimme
                h_p, h_a = H._hls(p['primary'])[0], H._hls(p['accent'])[0]
                abstand = abs(((h_a - h_p) + 0.5) % 1.0 - 0.5) * 360
                self.assertGreater(abstand, 100, f'nur {abstand:.0f}° Abstand')

    def test_ohne_zweitfarbe_geht_es_auch(self):
        for marke, _ in MARKEN:
            p = H.palette(marke)
            with self.subTest(marke=marke):
                self.assertEqual(p['primary'], marke.lower())
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

    def test_jede_anmutung_sieht_wirklich_anders_aus(self):
        """Die Kernzusage des ganzen Moduls. Dass zwei Anmutungen sich in ihren Feldern
        unterscheiden, heißt noch nicht, dass man den Unterschied SIEHT – eine Tabelle, in
        der nur die Eckenrundung variiert, wäre technisch eindeutig und optisch wertlos.
        Geprüft wird deshalb am fertigen Bild."""
        import hashlib
        import meme_muster as MU
        cfg = MU.muster_config('zitat')
        werte = {'city_name': 'Musterstadt', 'spruch': 'Hier kennt jeder jeden.',
                 'city_handle': '@probe'}
        summen = {}
        for i in range(len(H.ANMUTUNGEN)):
            stil = R.stil_dict(H.handschrift('#3db8ff', '#1a4fcc', i))
            png = R.render(None, cfg, werte, brand=stil)
            summen.setdefault(hashlib.md5(png).hexdigest(), []).append(i)
        doppelt = {k: v for k, v in summen.items() if len(v) > 1}
        self.assertFalse(doppelt,
                         'Diese Anmutungen ergeben dasselbe Bild: '
                         + '; '.join(str(v) for v in doppelt.values()))

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

    def test_geometrie_entscheidet_wirklich(self):
        """Der Kern des Ganzen: die Entfernung bestimmt die Zuteilung, nicht die Position
        in der Liste.

        Mit zehn Städten und zwanzig Anmutungen kommt schon durch das Reihum-Starten
        0,1,2,…,9 heraus – ein Test mit so wenigen Städten bliebe auch dann grün, wenn man
        die Nachbarschaftsrechnung ersatzlos entfernte. Hier stehen deshalb zwei weit
        getrennte Haufen, deren Mitglieder in der Namensfolge ABWECHSELN: wer stur
        durchnummeriert, gibt zwei Städten DESSELBEN Haufens dieselbe Nummer."""
        n = len(H.ANMUTUNGEN)
        # Namen S00…S(2n-1); gerade Nummern liegen im Haufen bei Frankfurt, ungerade
        # bei Berlin. In der Namensfolge wechseln sich die Haufen also ab.
        staedte = []
        for i in range(2 * n):
            if i % 2 == 0:
                staedte.append((i, f'S{i:02d}', 50.11 + i * 0.001, 8.68))
            else:
                staedte.append((i, f'S{i:02d}', 52.52 + i * 0.001, 13.40))
        koord = {s[0]: (s[2], s[3]) for s in staedte}

        # Gegenprobe zuerst: stures Durchnummerieren MUSS hier scheitern, sonst beweist
        # der eigentliche Test nichts.
        naiv = {s[0]: i % n for i, s in enumerate(sorted(staedte, key=lambda s: s[1]))}
        naiv_kollision = [(a[1], b[1]) for i, a in enumerate(staedte) for b in staedte[i + 1:]
                          if naiv[a[0]] == naiv[b[0]]
                          and H.entfernung_km(koord[a[0]], koord[b[0]]) <= H.NACHBAR_KM]
        self.assertTrue(naiv_kollision,
                        'Der Aufbau taugt nicht: schon stures Durchnummerieren wäre hier '
                        'konfliktfrei, der Test könnte eine Funktion ohne Geometrie nicht '
                        'von einer mit unterscheiden.')

        # Und jetzt die echte Zuteilung: kein Nachbarpaar teilt sich eine Anmutung.
        z = H.anmutungen_verteilen(staedte)
        for i, a in enumerate(staedte):
            for b in staedte[i + 1:]:
                if H.entfernung_km(koord[a[0]], koord[b[0]]) > H.NACHBAR_KM:
                    continue
                with self.subTest(a=a[1], b=b[1]):
                    self.assertNotEqual(z[a[0]], z[b[0]])

        # In jedem Haufen stecken genau n Städte – also muss jede Anmutung genau einmal
        # vorkommen.
        for name, ids in (('Frankfurt', [s[0] for s in staedte if s[0] % 2 == 0]),
                          ('Berlin', [s[0] for s in staedte if s[0] % 2 == 1])):
            self.assertEqual(sorted(z[i] for i in ids), list(range(n)),
                             f'Haufen {name} verteilt nicht jede Anmutung genau einmal')

    def test_entfernung_schlaegt_die_reihenfolge(self):
        """Zwei Städte am selben Ort müssen sich unterscheiden, zwei weit entfernte dürfen
        gleich sein – auch wenn sie in der Liste direkt nebeneinanderstehen."""
        n = len(H.ANMUTUNGEN)
        eng = [(i, f'S{i:02d}', 50.0, 8.0) for i in range(n + 3)]
        z = H.anmutungen_verteilen(eng)
        # Alle am selben Ort: jede verfügbare Anmutung muss benutzt werden, bevor sich
        # eine wiederholt.
        self.assertEqual(len(set(z.values())), n)

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


class TestAbleitungsRouten(unittest.TestCase):
    """Die beiden Endpunkte – vor allem ihre Ränder.

    Sie hatten bis zur Gegenprüfung keinen einzigen Test; der Riegel gegen die
    Vorgabefarbe, der verhindert, dass zehn Städte identisch werden, war ungedeckt."""

    @classmethod
    def setUpClass(cls):
        os.environ.update({k: '' for k in (
            'ANTHROPIC_API_KEY', 'CLOUDINARY_URL', 'CANVA_CLIENT_ID', 'CANVA_CLIENT_SECRET',
            'CANVA_REFRESH_TOKEN', 'CONTENT_OS_URL', 'CONTENT_OS_KEY', 'RAPIDAPI_KEY',
            'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN')})
        os.environ.setdefault('MEMEOS_SCHEDULER', '0')
        os.environ.setdefault('MEMEOS_WORKERS', '0')
        os.environ.setdefault('MEMEOS_OFFLINE', '1')
        os.environ.setdefault('DATABASE_URL', 'sqlite://')
        import app as A
        from models import db, City, CityStyle
        uri = A.app.config['SQLALCHEMY_DATABASE_URI']
        assert uri == 'sqlite://' or ':memory:' in uri, f'echte Datenbank: {uri}'
        cls.A, cls.db, cls.City, cls.CityStyle = A, db, City, CityStyle
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

    def tearDown(self):
        for s in self._meine:
            zeile = self.CityStyle.query.filter_by(city_id=s.id).first()
            if zeile is not None:
                self.db.session.delete(zeile)
            self.db.session.delete(s)
        self.db.session.commit()

    def stadt(self, name, farbe='#3db8ff', **rest):
        s = self.City(name=name, state='Hessen', accent_color=farbe, **rest)
        self.db.session.add(s)
        self.db.session.commit()
        self._meine.append(s)
        return s

    def ableiten(self, s, **rumpf):
        return self.c.post(f'/api/stil/stadt/{s.id}/ableiten', json=rumpf, headers=self.H)

    # ── Der Riegel gegen die Vorgabefarbe ────────────────────────────────
    def test_vorgabefarbe_gilt_nicht_als_marke(self):
        s = self.stadt('Riegelstadt', farbe='#3b82f6')
        r = self.ableiten(s)
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIn('Vorgabe', (r.get_json() or {}).get('error', ''))

    def test_vorgabefarbe_auch_in_grossbuchstaben_und_mit_leerzeichen(self):
        for farbe in ('#3B82F6', '  #3b82f6 ', '#3b82F6'):
            s = self.stadt(f'Riegel {farbe.strip()}', farbe=farbe)
            with self.subTest(farbe=farbe):
                self.assertEqual(self.ableiten(s).status_code, 400)

    def test_vorgabefarbe_im_rumpf_gilt_trotzdem(self):
        # Wer sie ausdrücklich schickt, meint sie.
        s = self.stadt('Ausdruecklich', farbe='#3b82f6')
        r = self.ableiten(s, marke='#3b82f6')
        self.assertEqual(r.status_code, 200, r.get_json())

    # ── Schalter ─────────────────────────────────────────────────────────
    def test_speichern_als_zeichenkette_false_schreibt_nicht(self):
        s = self.stadt('Schalterstadt', farbe='#ff0000')
        for wert in ('false', 'nein', '0', 'aus', 'off', False):
            with self.subTest(wert=wert):
                r = self.ableiten(s, speichern=wert)
                self.assertEqual(r.status_code, 200, r.get_json())
                self.assertFalse((r.get_json() or {}).get('gespeichert'), wert)
                self.assertIsNone(self.CityStyle.query.filter_by(city_id=s.id).first())

    def test_speichern_als_zeichenkette_true_schreibt(self):
        s = self.stadt('Schreibstadt', farbe='#ff0000')
        r = self.ableiten(s, speichern='ja')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue((r.get_json() or {}).get('gespeichert'))
        self.assertIsNotNone(self.CityStyle.query.filter_by(city_id=s.id).first())

    def test_unsinniger_schalter_ergibt_400(self):
        s = self.stadt('Unsinnstadt', farbe='#ff0000')
        self.assertEqual(self.ableiten(s, speichern='vielleicht').status_code, 400)

    # ── Ränder, die vorher 500 ergaben ───────────────────────────────────
    def test_rumpf_der_kein_objekt_ist(self):
        s = self.stadt('Rumpfstadt', farbe='#ff0000')
        r = self.c.post(f'/api/stil/stadt/{s.id}/ableiten', json=[1, 2, 3], headers=self.H)
        self.assertEqual(r.status_code, 400, r.data[:200])

    def test_zahlen_sonderwerte(self):
        s = self.stadt('Zahlenstadt', farbe='#ff0000')
        for wert in (float('inf'), float('nan'), 'viel'):
            with self.subTest(wert=wert):
                self.assertEqual(self.ableiten(s, anmutung=wert).status_code, 400)
        for wert in (float('nan'), float('inf'), 0, -5, 'viel'):
            with self.subTest(radius=wert):
                r = self.c.post('/api/stil/ableiten', json={'radius_km': wert}, headers=self.H)
                self.assertEqual(r.status_code, 400, f'radius_km={wert}: {r.get_json()}')

    # ── Paketfarbe ist keine Markenfarbe ─────────────────────────────────
    def test_zweitfarbe_eines_pakets_wird_nicht_als_marke_gelesen(self):
        s = self.stadt('Paketstadt', farbe='#ff0000')
        zeile = self.CityStyle.aus_paket(s.id, 'boulevard')   # secondary #a88b00
        self.db.session.add(zeile)
        self.db.session.commit()
        r = self.ableiten(s)
        st = (r.get_json() or {}).get('stil', {})
        # Aus der Marke allein gerechnet – NICHT mit dem Goldton des Pakets.
        self.assertEqual(st, H.handschrift('#ff0000', None, (r.get_json() or {})['anmutung']))
        self.assertNotEqual(st.get('secondary', '').lower(), '#a88b00')

    def test_eigene_farbe_gewinnt_gegen_die_rechnung(self):
        """Der Betreiber waehlt die Markenfarben selbst. Die Ableitung darf nur Luecken
        fuellen – was von Hand gesetzt ist, bleibt."""
        s = self.stadt('Handstadt', farbe='#ff0000')
        zeile = self.CityStyle(city_id=s.id, accent='#123456', font_headline='bangers')
        self.db.session.add(zeile)
        self.db.session.commit()

        r = self.ableiten(s, speichern=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.db.session.expire_all()
        zeile = self.CityStyle.query.filter_by(city_id=s.id).first()
        self.assertEqual(zeile.accent, '#123456', 'eigene Akzentfarbe wurde überschrieben')
        self.assertEqual(zeile.font_headline, 'bangers', 'eigene Schrift wurde überschrieben')
        # Die Lücken sind gefüllt worden.
        self.assertEqual(zeile.primary, '#ff0000')
        self.assertTrue(zeile.dark)

    def test_alles_ersetzen_raeumt_auch_eigenes_weg(self):
        s = self.stadt('Ersetzstadt', farbe='#ff0000')
        zeile = self.CityStyle(city_id=s.id, accent='#123456')
        self.db.session.add(zeile)
        self.db.session.commit()
        r = self.ableiten(s, speichern=True, alles_ersetzen=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.db.session.expire_all()
        self.assertNotEqual(
            self.CityStyle.query.filter_by(city_id=s.id).first().accent, '#123456')

    def test_vorschau_nennt_was_stehen_bleibt(self):
        s = self.stadt('Bleibtstadt', farbe='#ff0000')
        self.db.session.add(self.CityStyle(city_id=s.id, accent='#123456'))
        self.db.session.commit()
        js = self.ableiten(s).get_json() or {}
        self.assertIn('accent', js.get('bleibt_stehen', []), js)
        self.assertTrue(js.get('nur_fehlende'))

    def test_hinweise_kommen_mit(self):
        s = self.stadt('Dunkelstadt', farbe='#101010')
        js = self.ableiten(s).get_json() or {}
        self.assertTrue(js.get('hinweise'), js)


class TestZusammenspielMitPaketen(unittest.TestCase):
    """Eigene Handschrift und fertiges Paket dürfen sich nicht gegenseitig auslöschen.

    Eine abgeleitete Handschrift trägt paket == ''. Das heißt „will kein Paket", nicht
    „hat noch keins" – wer die beiden verwechselt, löscht mit einem Klick auf
    „Stile verteilen" jede Markenfarbe aus dem Bestand."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('MEMEOS_SCHEDULER', '0')
        os.environ.setdefault('MEMEOS_WORKERS', '0')
        os.environ.setdefault('MEMEOS_OFFLINE', '1')
        os.environ.setdefault('DATABASE_URL', 'sqlite://')
        import app as A
        import studio_bp as SB
        from models import db, City, CityStyle
        uri = A.app.config['SQLALCHEMY_DATABASE_URI']
        assert uri == 'sqlite://' or ':memory:' in uri, f'echte Datenbank: {uri}'
        cls.A, cls.SB, cls.db, cls.City, cls.CityStyle = A, SB, db, City, CityStyle
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

    def tearDown(self):
        for s in self._meine:
            z = self.CityStyle.query.filter_by(city_id=s.id).first()
            if z is not None:
                self.db.session.delete(z)
            self.db.session.delete(s)
        self.db.session.commit()

    def stadt(self, name, farbe, lat=50.0, lon=8.0):
        s = self.City(name=name, state='Hessen', accent_color=farbe, lat=lat, lon=lon)
        self.db.session.add(s)
        self.db.session.commit()
        self._meine.append(s)
        return s

    def test_verteilen_laesst_eigene_handschriften_stehen(self):
        s = self.stadt('Eigenstadt', '#ff0000')
        r = self.c.post(f'/api/stil/stadt/{s.id}/ableiten',
                        json={'speichern': True}, headers=self.H)
        self.assertEqual(r.status_code, 200, r.get_json())
        zeile = self.CityStyle.query.filter_by(city_id=s.id).first()
        vorher = (zeile.primary, zeile.font_headline, zeile.paket)
        self.assertEqual(vorher[2], '')

        r = self.c.post('/api/stil/verteilen', json={}, headers=self.H)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.db.session.expire_all()
        zeile = self.CityStyle.query.filter_by(city_id=s.id).first()
        self.assertEqual((zeile.primary, zeile.font_headline, zeile.paket), vorher,
                         'Stile verteilen hat die eigene Handschrift überschrieben')

    def test_haken_bedeutet_in_beiden_routen_dasselbe(self):
        s = self.stadt('Hakenstadt', '#00ff00')
        zeile = self.CityStyle.aus_paket(s.id, 'boulevard')
        self.db.session.add(zeile)
        self.db.session.commit()
        # „Nur Städte ohne eigenen Stil" muss eine Stadt MIT Stil-Zeile überspringen –
        # egal ob die Zeile ein Paket trägt oder eine eigene Handschrift.
        r = self.c.post('/api/stil/ableiten',
                        json={'nur_ohne_stil': True}, headers=self.H)
        js = r.get_json() or {}
        self.assertIn(s.name, js.get('uebersprungen', []), js)
        self.assertNotIn(s.name, [x['city'] for x in js.get('staedte', [])])

    def test_ohne_koordinaten_zaehlt_das_bundesland(self):
        # Direkt nach dem Aufsetzen hat keine Stadt lat/lon. Ohne Rückfall wäre dann
        # niemand Nachbar und die Verteilung fiele auf die Namensfolge zurück.
        hessen = [(i, f'H{i:02d}', None, None, 'Hessen') for i in range(4)]
        bayern = [(10 + i, f'B{i:02d}', None, None, 'Bayern') for i in range(4)]
        z = H.anmutungen_verteilen(hessen + bayern)
        self.assertEqual(len({z[s[0]] for s in hessen}), 4, 'Hessen nicht unterschieden')
        self.assertEqual(len({z[s[0]] for s in bayern}), 4, 'Bayern nicht unterschieden')

    def test_nachbau_vorschau_zeigt_auch_eigene_handschriften(self):
        import vision_import_bp as V
        gemacht = []
        for i, farbe in enumerate(('#ff0000', '#00ff00', '#0000ff')):
            s = self.stadt(f'Vorschaustadt {i}', farbe, lat=50.0 + i, lon=8.0 + i)
            self.c.post(f'/api/stil/stadt/{s.id}/ableiten',
                        json={'speichern': True}, headers=self.H)
            gemacht.append(s.name)
        auswahl = V._staedte_fuer_vorschau(anzahl=3)
        eigene = [c.name for c in auswahl if not V._stadt_paket(c)]
        self.assertTrue(eigene, f'Vorschau zeigt nur Paketstädte: '
                                f'{[c.name for c in auswahl]}')

    def test_neue_stadt_mit_marke_bekommt_gleich_eine_handschrift(self):
        r = self.c.post('/api/cities',
                        json={'name': 'Neustadt mit Marke', 'state': 'Hessen',
                              'accent_color': '#ff6600'}, headers=self.H)
        self.assertEqual(r.status_code, 201, r.get_json())
        js = r.get_json() or {}
        s = self.City.query.get(js['id'])
        self._meine.append(s)
        self.assertTrue(js.get('handschrift'), js)
        zeile = self.CityStyle.query.filter_by(city_id=s.id).first()
        self.assertIsNotNone(zeile, 'keine Stil-Zeile angelegt')
        self.assertEqual(zeile.primary, '#ff6600')
        self.assertEqual(zeile.paket, '')

    def test_neue_stadt_ohne_marke_bekommt_keine(self):
        for rumpf in ({'name': 'Neustadt ohne Marke', 'state': 'Hessen'},
                      {'name': 'Neustadt Vorgabe', 'state': 'Hessen',
                       'accent_color': '#3b82f6'}):
            with self.subTest(rumpf=rumpf):
                r = self.c.post('/api/cities', json=rumpf, headers=self.H)
                self.assertEqual(r.status_code, 201, r.get_json())
                js = r.get_json() or {}
                s = self.City.query.get(js['id'])
                self._meine.append(s)
                self.assertIsNone(js.get('handschrift'))
                self.assertIsNone(self.CityStyle.query.filter_by(city_id=s.id).first())


if __name__ == '__main__':
    unittest.main(verbosity=2)
