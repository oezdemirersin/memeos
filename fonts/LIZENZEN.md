# Schriften in diesem Ordner – Lizenzen und Quellen

Alle neu aufgenommenen Google-Fonts sind statische TrueType-Dateien (keine variablen Fonts;
einzige Ausnahme aus dem Altbestand ist `bold.ttf`, siehe Hinweis unten),
geladen aus dem Repo <https://github.com/google/fonts> (Zweig `main`). Die Lizenztexte liegen im
jeweiligen Quellordner des Repos (`OFL.txt` bzw. `LICENSE.txt`).

Die Schlüssel entsprechen `memeos_render._BUILTIN_FONTS`; der Dateiname ist der Schlüssel plus `.ttf`.

| Schrift            | Schlüssel           | Datei                   | Lizenz        | Quelle (google/fonts, Zweig main) |
|--------------------|---------------------|-------------------------|---------------|-----------------------------------|
| Anton              | `anton`             | `anton.ttf`             | SIL OFL 1.1   | https://github.com/google/fonts/tree/main/ofl/anton |
| Oswald (variabel)  | `bold`              | `bold.ttf`              | SIL OFL 1.1   | https://github.com/google/fonts/tree/main/ofl/oswald (Oswald[wght].ttf, siehe Hinweis) |
| Arial Rounded Bold | `arial_rounded_bold`| `arial_rounded_bold.ttf`| proprietär    | siehe Hinweis unten |
| Bebas Neue         | `bebas_neue`        | `bebas_neue.ttf`        | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/bebasneue/BebasNeue-Regular.ttf |
| Archivo Black      | `archivo_black`     | `archivo_black.ttf`     | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/archivoblack/ArchivoBlack-Regular.ttf |
| Poppins Black      | `poppins_black`     | `poppins_black.ttf`     | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Black.ttf |
| Poppins SemiBold   | `poppins_semibold`  | `poppins_semibold.ttf`  | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-SemiBold.ttf |
| Titan One          | `titan_one`         | `titan_one.ttf`         | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/titanone/TitanOne-Regular.ttf |
| Lilita One         | `lilita_one`        | `lilita_one.ttf`        | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/lilitaone/LilitaOne-Regular.ttf |
| Bangers            | `bangers`           | `bangers.ttf`           | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/bangers/Bangers-Regular.ttf |
| Luckiest Guy       | `luckiest_guy`      | `luckiest_guy.ttf`      | Apache 2.0    | https://raw.githubusercontent.com/google/fonts/main/apache/luckiestguy/LuckiestGuy-Regular.ttf |
| Permanent Marker   | `permanent_marker`  | `permanent_marker.ttf`  | Apache 2.0    | https://raw.githubusercontent.com/google/fonts/main/apache/permanentmarker/PermanentMarker-Regular.ttf |
| Kalam Bold         | `kalam_bold`        | `kalam_bold.ttf`        | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/kalam/Kalam-Bold.ttf |
| Alfa Slab One      | `alfa_slab_one`     | `alfa_slab_one.ttf`     | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/alfaslabone/AlfaSlabOne-Regular.ttf |
| Passion One Black  | `passion_one_black` | `passion_one_black.ttf` | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/passionone/PassionOne-Black.ttf |
| Rubik Mono One     | `rubik_mono_one`    | `rubik_mono_one.ttf`    | SIL OFL 1.1   | https://raw.githubusercontent.com/google/fonts/main/ofl/rubikmonoone/RubikMonoOne-Regular.ttf |
| Oswald Bold        | `oswald_bold`       | `oswald_bold.ttf`       | SIL OFL 1.1   | https://raw.githubusercontent.com/googlefonts/OswaldFont/main/fonts/ttf/Oswald-Bold.ttf (Lizenz: OFL.txt im selben Repo) |

Lizenztexte:

- SIL Open Font License 1.1: `OFL.txt` im jeweiligen Ordner unter `ofl/` (z. B.
  https://raw.githubusercontent.com/google/fonts/main/ofl/bebasneue/OFL.txt). Die Schriften dürfen
  mit der Anwendung ausgeliefert und in gerenderten Bildern frei verwendet werden; ein Verkauf der
  Schriftdateien allein ist nicht erlaubt.
- Apache License 2.0: `LICENSE.txt` im jeweiligen Ordner unter `apache/` (z. B.
  https://raw.githubusercontent.com/google/fonts/main/apache/luckiestguy/LICENSE.txt).

## Hinweis zu bold.ttf (Oswald)

`bold.ttf` ist die variable Datei `Oswald[wght].ttf` (Achse wght 200..700, Standard 400).
Pillow rendert von variablen Fonts die Standard-Instanz, hier also Oswald **Regular**, nicht
Bold. Der Schlüssel `bold` bleibt mit dieser Datei bestehen, damit alle vorhandenen Vorlagen und
Stadt-Stile unverändert aussehen; im Studio heißt er deshalb "Oswald". Die echte Bold-Instanz
liegt statisch als `oswald_bold.ttf` (Schlüssel `oswald_bold`, Upstream-Build des
Oswald-Projekts, ebenfalls SIL OFL 1.1).

## Hinweis zu arial_rounded_bold.ttf

`arial_rounded_bold.ttf` (Arial Rounded MT Bold) ist eine proprietäre Schrift von Monotype und
keine freie Schrift. Sie bleibt im Ordner, weil bestehende Vorlagen den Schlüssel
`arial_rounded_bold` verwenden. Nicht entfernen; beim Weitergeben der Anwendung an Dritte ist die
Lizenzlage dieser einen Datei gesondert zu prüfen.

## Prüfung neuer Dateien

Jede neue Schrift vor dem Eintragen so prüfen: `file fonts/<datei>` muss "TrueType Font data"
melden, und `ImageFont.truetype(pfad, 40)` muss ohne Ausnahme laden. Keine variablen Fonts
(`[wght]`-Dateien) verwenden: Pillow rendert davon nur die Standard-Instanz der Achse (bei
Oswald Regular 400), eine bestimmte Stärke wie Bold ist über den Schlüssel nicht wählbar.
