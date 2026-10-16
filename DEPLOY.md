# MemeOS auf Render

Was hier steht, ist nachgemessen: an einem frischen `git clone` mit leerer Platte und
dem echten Startbefehl, nicht aus der Erinnerung.

---

## 1. Vorher wissen

**Ein frischer Deploy ist fast leer.** `instance/` steht in `.gitignore`, auf Render wird
dort eine leere Platte eingehängt. Mitgebracht wird nur, was im Repository liegt: der Code,
17 Schriften, und beim ersten Start legt die App 20 Städte aus dem Seed an. Nicht dabei:

| | frischer Deploy | hier lokal |
|---|---|---|
| Städte | 20 (Seed) | 20 |
| Instagram-Handles | 0 | 10 |
| Vorlagen | 0 | 7 |
| Bilddateien (`uploads`) | 0 | 27 |
| Stadt-Handschriften | 0 | 20 |
| Stadtwissen | 0 | 10 |
| Beiträge | 0 | 6 |

Alles aus der rechten Spalte kommt über das Paket in Schritt 4 hinüber.

---

## 2. Dienst anlegen

Render liest `render.yaml` selbst, wenn du das Repository als **Blueprint** verbindest –
dann stimmen Startbefehl, Platte und Gesundheitsprüfung von allein.
Im Dashboard: *New* → *Blueprint* → Repository wählen.

Diese Werte musst du selbst eintragen (in `render.yaml` stehen sie als `sync: false`,
Render fragt sie beim Anlegen ab):

| Variable | Wert | Pflicht |
|---|---|---|
| `ADMIN_USERNAME` | dein Benutzername fürs Dashboard | **ja** |
| `ADMIN_PASSWORD` | ein langes Passwort | **ja** |
| `ANTHROPIC_API_KEY` | der Schlüssel aus `.env` Zeile 6 | für alles mit KI |
| `BASE_URL` | die Render-URL, z. B. `https://memeos.onrender.com` | für Telegram-Knöpfe |
| `CLOUDINARY_URL` | aus dem Cloudinary-Konto | zum Posten |
| `RAPIDAPI_KEY`, `DEEPSEEK_API_KEY`, `CANVA_*`, `CONTENT_OS_*` | optional | nein |

`SECRET_KEY` erzeugt Render selbst (`generateValue: true`) – nicht eintragen.
Die **`.env` wird auf Render nicht gelesen**; sie steht in `.gitignore` und geht gar nicht
mit. Alles, was dort steht und drüben gebraucht wird, muss ins Render-Dashboard.

Telegram wird **nicht** über Umgebungsvariablen gesetzt, sondern im Dashboard unter
*Einstellungen → Telegram*.

---

## 3. Erster Start prüfen

Nach dem Deploy:

1. `https://<dein-dienst>.onrender.com/ping` muss `ok` zurückgeben.
2. Einloggen mit `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
3. *Einstellungen → Selbsttest*: **0 kritische Befunde**. Warnungen sind an dieser Stelle
   normal (keine Vorlagen, kein Stadtwissen – das kommt erst mit Schritt 4).

Im Log steht beim ersten Start für jeden Tagesjob *„Nachholsperre"*. Das ist richtig so:
sonst feuern nach einem Deploy am Nachmittag alle neun Tagesjobs auf einmal.

---

## 4. Inhalt hinüberbringen

```bash
python3 tools/deploy_paket.py
```

Das schreibt ein Archiv nach `instance/exports/` und druckt die Befehle für genau dein
Paket aus. Es enthält die Datenbank (über die sqlite3-Backup-Schnittstelle gesichert, nicht
kopiert – im WAL-Modus steht der letzte Schreibvorgang sonst noch daneben), die
Hintergrundbilder, Aufkleber und eigenen Schriften. Mit `--mit-renders` kommen auch die
fertigen Beitragsbilder mit; ohne sie verlieren Entwürfe im Vorrat ihr Bild, dafür ist das
Paket viel kleiner.

Dann, mit dem SSH-Befehl aus der Render-Oberfläche (*Connect* → *SSH*):

```bash
scp instance/exports/memeos_deploy_<stempel>.tar.gz \
    srv-xxxxxxxxxxxx@ssh.frankfurt.render.com:/opt/render/project/src/instance/
```

```bash
ssh srv-xxxxxxxxxxxx@ssh.frankfurt.render.com
cd /opt/render/project/src/instance && tar xzf memeos_deploy_<stempel>.tar.gz && rm memeos_deploy_<stempel>.tar.gz
```

Das Auspacken legt alles unter `_paket/` ab und fasst nichts an. Danach in der
Render-Oberfläche **Manual Deploy → Restart service**.

**Warum der Umweg über den Neustart:** eingespielt wird beim Start, bevor die App die
Datenbank öffnet. Tauscht man `memeos.db` unter einem laufenden gunicorn aus, bleibt das
`-wal` der alten Datenbank daneben liegen – SQLite prüft nicht, ob es dazugehört, und
spielt es beim nächsten Start auf die neue Datei. Still und ohne Fehlermeldung.

Gegenprobe danach:
* Im Log: `Deploy-Paket eingespielt: N Datei(en), Datenbank getauscht`
* Vorlagenliste zeigt die Vorlagen aus der Tabelle oben
* *Einstellungen → Selbsttest*: 0 kritisch

Die bisherige Datenbank wird nicht gelöscht, sondern als
`memeos.db.vor_paket_<zeitstempel>` beiseitegelegt – samt ihrem `-wal`, also vollständig
wiederherstellbar.

---

## 5. Kostenbremsen scharf stellen

Sie kommen mit dem Paket mit und stehen auf:

| Bremse | Vorgabe | Einstellung |
|---|---|---|
| Monatsbudget | 10 € | `ai_budget_month_eur` |
| Tagesbudget | 2 € | `ki_budget_tag_eur` |
| Aufrufe je Stunde | 120 | `ki_aufrufe_pro_stunde` |
| KI-Notaus | aus | `ki_aus` |

Zu sehen und zu verstellen unter *Einstellungen → KI-Kosten*; der Stand steht auch unter
`GET /api/ki-bremse`. Greift eine davon, laufen auch die Automatik-Jobs nicht mehr, die KI
kosten – und der Knopf *„Jetzt ausführen"* ebenso wenig.

`MEMEOS_USD_EUR` steht auf `1.00`, also ohne Umrechnung. Die Anthropic-Rechnung kommt in
Dollar; die Grenze „10 €" ist damit in Wahrheit 10 $ und greift früher, nicht später. Wer
einen Kurs eintragen will, tut das in `render.yaml` oder im Render-Dashboard.

---

## 6. Sicherungen

Der Tagesjob `sicherung` läuft um 03:30 und legt eine Kopie der Datenbank nach
`instance/exports/sicherungen`; die sieben jüngsten bleiben liegen.

Das hilft gegen Fehlgriffe im Betrieb, **nicht gegen den Verlust der Platte** – die Kopie
liegt auf derselben. Für eine Kopie ausser Haus gibt es *Einstellungen → Daten-Backup → „Datenbank
sichern"* (`/api/backup/db`).

---

## Was schon einmal schiefging

* **`gunicorn` ohne `--bind`** – Render meldet „no open ports detected", obwohl der Prozess
  läuft. `startCommand` in `render.yaml` sticht das `Procfile`; dort fehlte es.
* **Falsche Pillow-Version** – `requirements.txt` stand auf 10.4.0, geprüft wurde alles mit
  11.3.0. Schriftkanten kommen aus FreeType, die Bilder werden messbar andere. Jetzt
  festgenagelt.
* **Gesundheitsprüfung auf `/`** – das liegt hinter dem Login und antwortet mit 302, Render
  will eine 200. Deshalb `/ping`.
* **Die Datenbank von Hand tauschen** – siehe Schritt 4.
