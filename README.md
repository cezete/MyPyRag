# MyPyRag

Python 3.12 alapú, újraindítható dokumentum-ingestion. Ez a verzió kizárólag
az első lépcsőt valósítja meg: `RECEIVED → CONVERTING → CONVERTED`.
A forrás változatlanul megmarad; a Docling natív JSON-ja kerül mentésre és
DoclingDocumentként visszatöltésre. A `CHUNKS` üres, a dokumentum az `IN` alatt
marad. Nincs Ollama- vagy Qdrant-hívás.

## Telepítés Windows alatt (PowerShell)

Előfeltétel: Python 3.12 és [uv](https://docs.astral.sh/uv/getting-started/installation/).
A fejlesztéshez használt uv: 0.11.26. A parancsokat a repository gyökerében futtasd.

```powershell
uv sync --locked --python 3.12 --cache-dir .uv-cache
Copy-Item .env.example .env
.\.venv\Scripts\mypyrag.exe status
.\.venv\Scripts\mypyrag.exe watch
.\.venv\Scripts\mypyrag.exe process .\docs\IN\sample.md
```

Ha már van `.env`, őrizd meg, ne másold rá újra a példát.

## Telepítés Linux alatt

Előfeltétel: Python 3.12 és uv. PDF-feldolgozásnál az OCR/backend rendszerkönyvtárai
is szükségesek lehetnek; lásd a [Docling telepítési dokumentációját](https://docling-project.github.io/docling/getting_started/installation/).

```bash
uv sync --locked --python 3.12 --cache-dir .uv-cache
cp -n .env.example .env
./.venv/bin/mypyrag status
./.venv/bin/mypyrag watch
./.venv/bin/mypyrag process ./docs/IN/sample.md
```

A `uv sync` létrehozza a projektspecifikus `.venv` környezetet. Aktiválás nem
szükséges. A `.venv` binárisainak közvetlen futtatása nem igényel csomagletöltést.
A verziókezelt `uv.lock` a tranzitív függőségeket, platformfeltételeket és
csomag-hasheket is rögzíti. Python: `>=3.12,<3.13`. Docling: `>=2.70,<3`, mert
a 2.x API-családra épülünk. A lock ténylegesen **Docling 2.126.0** és
**docling-core 2.95.0** verziót rögzít; a tartomány nem okoz automatikus frissítést.
A fejlesztői eszközök szintén lockolva vannak. Tudatos frissítés: `uv lock --upgrade`,
majd `uv sync --locked` és az összes alábbi ellenőrzés. A lockfájlt a pyprojecttel
együtt kell commitolni.

## Konfiguráció és CLI

A `.env.example` minden beállítást felsorol. A környezeti változók felülírják
a `.env` értékeit. A `.env` helye és minden relatív konfigurált könyvtár alapja
az aktuális munkakönyvtár, vagy az explicit `--base-dir`:

```text
mypyrag --base-dir /srv/MyPyRag watch
mypyrag process <file>
mypyrag status
```

A `process` fájlargumentuma a shell aktuális könyvtárához képest értendő.
A `watch` Ctrl+C-vel leállítható. Exit code: 0 siker (a felismert duplikátum is),
1 konfigurációs, fájl-, konverziós vagy zárolási hiba, Ctrl+C esetén 130,
hibás CLI-szintaxisnál 2. A `status` egyedi dokumentumlistát és állapotösszesítést
ír ki, sérült manifestet és befejezetlen átvételt is jelez. Nem indít Doclingot.

Csak `MYPYRAG_MAX_STAGE=CONVERTED` használható. A séma ismeri a `CHUNKED` és
`INDEXED` értékeket, de induláskor jelzi, hogy még nem implementáltak.
Az Ollama/Qdrant mezők típusosan jelen vannak; URL-szintaxist ellenőrzünk,
szolgáltatáselérhetőséget nem. A hiányzó értékek alapértéket kapnak; az explicit
üres érték, hibás szám, negatív intervallum és ismeretlen állapot hiba.

## Dokumentumok és átvétel

Az új fájlok a `docs/IN` közvetlen gyökerébe kerülnek. Támogatott kiterjesztések:
`.pdf`, `.docx`, `.html`, `.htm`, `.md`, `.markdown`, kis-/nagybetű függetlenül.
Bővítéskor a `converter.FORMATS` táblát és az adapter engedélyezett formátumait
együtt kell módosítani. A manifest MIME mezője a formátumtáblából származó
besorolás; a tartalom tényleges értelmezését a Docling végzi.

A watcher csak normál fájlokat vesz át, ponttal kezdődő fájlokat és szimbolikus
linkeket nem. Méretet és nanomásodperces módosítási időt figyel több polling
cikluson keresztül; nem várakozik külön minden fájlra. Újraindításkor a stabilitási
megfigyelés újrakezdődik. A `process` kész inputot jelent, kihagyja a várakozást,
de hash- és változásellenőrzést végez. Ne hagyd a bemásoló programot az átvett
forrást tovább írni; ideális esetben ponttal kezdődő ideiglenes névvel másolj,
majd a teljes másolatot nevezd át az `IN` gyökerében.

```text
docs/IN/manual--a1b2c3d4/
├── source/manual.pdf
├── JSON/document.json
├── CHUNKS/
└── manifest.json
```

A teljes SHA-256 a dokumentumazonosító. A név a tisztított stemből és 8 hash-karakterből
áll; könyvtárnév-ütközésnél további véletlen suffix véd a felülírástól.
Az `IN`, `DONE`, `ERROR` manifestjeiben teljes hash alapján keresünk duplikátumot.
**A duplikált nyers fájl változatlanul a helyén marad**, és figyelmeztetés készül
(watch módban ciklusonként). Szükség szerint kézzel helyezd át az `IN` gyökeréből.
Második manifest és konverzió nem készül.

A könyvtárak legyenek különállók, ne legyenek egymás alatt, és ugyanazon a
fájlrendszeren legyenek. A `process` forrása is ugyanazon a fájlrendszeren legyen;
egyébként előbb másold az `IN` alá. A program nem alkalmaz nem atomi, meghajtók
közötti mozgatást. Ugyanahhoz az adathalmazhoz ugyanazt az `IN`/`DONE`/`ERROR`
konfigurációt használd. Az `IN/.mypyrag.lock` OS által kezelt zár ciklusonként
és `process` műveletenként kizárja a párhuzamos írást; processzhalál után feloldódik.
Helyi fájlrendszerre terveztük; hálózati fájlrendszeren nincs ellenőrizve.

## Megszakítás és hibák

A `receipt.json` átvételi napló a forrásmozgatás előtt jön létre. A következő
ciklus ebből helyreállítja a manifestet, akár a mozgatás előtt, akár utána szakadt
meg a folyamat. Hiányzó/megváltozott forrás esetén mindent megőrzünk és naplózunk;
az ilyen félkész átvétel kézi ellenőrzést igényel. Átvétel előtti hiba sem törli
az inputot.

Manifestmentés: ideiglenes fájl ugyanabban a könyvtárban, flush, fsync, lezárás,
atomi replace; POSIX alatt könyvtár-fsync is. A Docling JSON ideiglenes fájlba
készül, DoclingDocumentként visszatöltjük, majd atomi replace történik.
A JSON a képeket beágyazva menti, nem Markdown/HTML köztes reprezentációból készül.
A „veszteségmentes” a DoclingDocument natív szerializálását jelenti, nem azt,
hogy a konverter a forrás minden vizuális részletét felismeri.

A folytatás alapja a `last_successful_state`. Félbeszakadt `CONVERTING` újrafut;
a `CONVERTED` nem konvertálódik újra. Sikeres állapot után a JSON kézi törlését
vagy sérülését ez a verzió nem javítja automatikusan.

Konverziós hiba és nem támogatott típus esetén a teljes munkakönyvtár az `ERROR`
alá kerül. A manifest megőrzi az utolsó sikeres állapotot, rövid hibaüzenetet,
hibás fokozatot és a sikertelen kísérletek számát (`attempt_count`). A teljes
stack trace a standard logging kimenetén látható. Áthelyezési hiba után a következő
ciklus újra megpróbálja az `ERROR` állapotú könyvtár áthelyezését.
Érvénytelen manifestet nem írunk felül: naplózzuk és helyben hagyjuk. Ilyenkor
az új regisztráció a biztonságos duplikációellenőrzés hiánya miatt elutasított,
de az érvényes, már átvett dokumentumok tovább feldolgozhatók.

Automatikus `retry-errors` és `resume` CLI még nincs. Az ERROR tartalma és a
manifest alkalmas lesz a későbbi retry implementációra. A megszakításból
visszamaradt `.tmp` fájlok nem számítanak sikeres outputnak.

## Ellenőrzés

Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest -m integration
```

Linux:

```bash
./.venv/bin/python -m pytest
./.venv/bin/python -m ruff check .
./.venv/bin/python -m mypy
./.venv/bin/python -m pytest -m integration
```

A normál tesztek fake adaptert, valódi DoclingDocument JSON-sémát és pytest
ideiglenes könyvtárakat használnak; nem töltenek modelleket, nincs szolgáltatáshívás.
A külön integrációs tesztek apró MD, MARKDOWN, HTM és DOCX inputot konvertálnak
valódi Doclinggal. A `.markdown` alias változatlan bájtokkal, `.md` nevű adatfolyamként
kerül a Doclinghoz; az eredeti fájlnév a dokumentum origin mezőjében megmarad.
A PDF első konverziója modellletöltést igényelhet; offline szerverre előre
telepítsd a szükséges modelleket a Docling dokumentációja szerint.

## Gradle

A `build.gradle` vékony vezérlőréteg: `setup`, `test`, `run`, `verify`,
`cleanGenerated`. A `verify` lintet, mypy-t és unit teszteket futtat.
A `cleanGenerated` csak a projekt gyökérbeli `test-output` könyvtárát kezeli;
a felhasználói `docs` könyvtárakat nem érinti, átirányított útvonalat elutasít.

A fejlesztőgépen Java 21 volt elérhető, Gradle nem. Emiatt nincs kézzel készített
vagy ellenőrizetlen wrapper. Ellenőrzött Gradle 8.14.3 telepítése után a wrapper
szabályosan így generálható (a generált wrapper fájljait később verziókezeld):

```text
gradle wrapper --gradle-version 8.14.3 --distribution-type bin
```

Ezután Windows: `.\gradlew.bat setup`, `.\gradlew.bat test`, `.\gradlew.bat run`,
`.\gradlew.bat verify`, `.\gradlew.bat cleanGenerated`.
Linux: `chmod +x gradlew`, majd `./gradlew setup`, `./gradlew test`, `./gradlew run`,
`./gradlew verify`, `./gradlew cleanGenerated`.
Kövesd a [Gradle hivatalos útmutatóját](https://docs.gradle.org/current/userguide/gradle_wrapper.html),
és ellenőrizd a disztribúció SHA-256 értékét. A Gradle-taskok ezen a gépen nem
voltak futtathatók; a Python-parancsok közvetlenül használhatók.

## Komponensek és következő lépcső

- `config.py`: típusos `.env` séma, következetes útvonalak.
- `model.py`: verziózott manifest, állapotok, sikeres fokozatok sorrendje.
- `storage.py`: hash, stabilitás, útvonal-ellenőrzés, atomi JSON-tárolás.
- `converter.py`: injektálható adapter; Docling-függőség elkülönítve.
- `pipeline.py`: átvétel, inventory, duplikáció, folytatás, hibakezelés.
- `cli.py`: watch/process/status és exit code-ok.

A második lépcső a `CONVERTED` állapotnál kapcsolódhat be, a natív JSON
visszatöltésével. A manifest chunkmezői és a `CHUNKS` könyvtár elő vannak készítve;
HybridChunker, embedding és Qdrant kód nincs ebben a változatban.

A `.gitignore` kizárja az alapértelmezett adatkönyvtárak teljes tartalmát a
`.gitkeep` kivételével, valamint a `.env`, `.venv`, log, build és cache fájlokat.
Egyedi adatkönyvtárat a repositoryn kívül használj, vagy előbb külön ignoráld.
Felhasználói inputot, JSON-t és feldolgozási eredményt ne adj Githez.
