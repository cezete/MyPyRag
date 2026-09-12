# MyPyRag

Python 3.12 alapú, újraindítható dokumentum-ingestion és strukturált chunkolás:
`RECEIVED → CONVERTING → CONVERTED → CHUNKING → CHUNKED`. A forrás változatlanul
megmarad; a chunkolás kizárólag a veszteségmentesen mentett `JSON/document.json`
`DoclingDocument` objektummá történő visszatöltése után indul. A dokumentum az
`IN` alatt marad. Nincs embedding-, Ollama-, PostgreSQL- vagy Qdrant-hívás.

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

`MYPYRAG_MAX_STAGE=CONVERTED` a natív JSON mentése után megáll;
`MYPYRAG_MAX_STAGE=CHUNKED` ugyanabban a futásban továbbhalad a chunk artifactokig.
Az `INDEXED` ismert, de szándékosan nem implementált állapot, ezért konfigurációs
hibát ad. A chunker jelenleg csak `hybrid` lehet, a tokenlimit 1–2048 közötti.
A második lépcső ajánlott `.env` beállításai (explicit `.env` nélkül a kompatibilis
kódalapérték továbbra is `CONVERTED`):

```dotenv
MYPYRAG_MAX_STAGE=CHUNKED
MYPYRAG_CHUNKER=hybrid
MYPYRAG_CHUNK_MAX_TOKENS=512
MYPYRAG_CHUNK_TOKENIZER=nomic-ai/nomic-embed-text-v1.5
```

Az Ollama/Qdrant mezők továbbra is típusosan jelen vannak, de ebben a lépcsőben
csak a szintaxisukat ellenőrizzük, a szolgáltatásokat nem érjük el.
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
├── CHUNKS/0001.txt
├── CHUNKS/0001.json
└── manifest.json
```

## Strukturált chunkolás

A normál szöveget a Docling 2.x `HybridChunker` dolgozza fel. A beadott tokenizer
a `nomic-ai/nomic-embed-text-v1.5` Hugging Face tokenizer, amely megfelel a későbbi
`nomic-embed-text:latest` modell tokenizálásának. Első éles használatkor a tokenizer
kis konfigurációs fájljait a Hugging Face kliensnek elérhetővé kell tenni vagy előre
cache-elni; a unit tesztek lokális fake tokenizert használnak. A HybridChunker a
tokenizer által mért, kontextussal együtt számított legfeljebb 512 tokent tartja.
A limit konfigurálható és a 2048 tokenes célkontextusnál nem lehet nagyobb.

Minden chunk két szövegréteget tartalmaz:

- `text`: a chunk saját, ember által olvasható tartalma;
- `embedding_text`: a Docling `contextualize()` által hozzáadott dokumentumcím/
  címsorútvonal és a tartalom, tábláknál pedig címsorok, képaláírás, kulcsmezők és
  az `oszlop: érték` sorreprezentáció. A `.txt` pontosan ezt tárolja, záró sortörés
  hozzáadása nélkül.

A `document_id`, `chunk_id`, indexek, hashértékek, útvonalak, verziók és időbélyegek
csak JSON metadata; nem kerülnek az embedding szövegébe. Oldalszámok és Docling
item reference-ek külön mezőben maradnak. A `structural_path` jelenleg a Docling
által megbízhatóan visszaadott címsorútvonal.

### Táblázatok

A HybridChunker táblachunkjai nem kerülnek közvetlenül a kimenetbe. A program a
Docling `TableData.table_cells` koordinátáiból, row/column spanekből és
`column_header` jelölésekből rekonstruálja a rácsot, így ugyanaz a tartalom nem
duplikálódik. Minden logikai adatsor külön `table_row` chunk, benne olvasható
`Oszlop: érték` párokkal. Ha nincs Docling által jelölt fejléc, nem találunk ki
nevet: `column_1`, `column_2`, … semleges jelölés készül, és
`header_reconstructed=false` kerül a metadata-ba.

A kulcsmező-felismerés kis-/nagybetűtől és aláhúzástól függetlenül az address,
identifier, name, function, command, opcode, register, code, number és id
oszlopneveket keresi. Function/command/name esetén egy pont előtti egyszavas
szimbólumot (például `SCINIT`) őriz kulcsként; más esetben a teljes értéket.
Túl hosszú sor csak saját mezőhatárain belül, végső esetben egyetlen mező értékének
szóhatárain darabolódik. Minden részben ismétlődik a strukturális kontextus és a
kulcsmező, valamint `part_index`/`part_count` készül. Egyetlen, önmagában a limitnél
hosszabb oszthatatlan token vagy a kötelező ismételt kontextus kivételesen túllépheti
a határt; adatot nem csonkítunk.

### Azonosítók és fingerprint

A `chunk_index` egyalapú, hézagmentes és a kimeneti sorrenddel azonos. A fájlnév
legalább négyjegyű, nagyobb darabszámnál automatikusan szélesebb. A `chunk_id`
determinista UUID5, rögzített MyPyRag névtérben, ebből a névalapból:

```text
document_id : chunking_fingerprint : chunk_index : text_sha256
```

A fingerprint a rendezett, tömör UTF-8 JSON-nal kanonizált chunkernévből,
implementációs sémaverzióból, maximális tokenszámból, tokenizerazonosítóból,
kontextusszerializálási módból és táblastratégia-verzióból számított SHA-256.
A `text_sha256` és `embedding_text_sha256` az adott mező pontos UTF-8 bájtjainak
hashértéke.

### Atomi kimenet

A teljes készlet dokumentumon belüli egyedi staging könyvtárban készül. A program
visszaolvassa az összes JSON-t, ellenőrzi a sémát, indexeket, párokat, hashértékeket,
ürességet és azonosító-egyediséget, majd könyvtárszintű cserével publikálja. Meglévő
készlet ideiglenes backupból visszaállítható, ha a publikálás megszakad. A manifest
csak ezután kap `CHUNKED`, fingerprint és ellenőrzött `chunk_count` értéket. Azonos
bemenet/configuráció az időbélyegen kívül azonos szemantikai tartalmat és ID-kat ad.

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
a `CONVERTED` nem konvertálódik újra, hanem `CHUNKING` felé halad, ha a maximum
`CHUNKED`. Félbeszakadt chunkolás teljes új staging készlettel fut újra. Hiányzó,
sérült vagy nem visszatölthető natív JSON dokumentumszintű `CHUNKING` hibát ad.

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

A normál tesztek fake adaptert/tokenizert, valódi DoclingDocument JSON-sémát és
pytest ideiglenes könyvtárakat használnak; nem töltenek modelleket, nincs hálózati
vagy szolgáltatáshívás. Külön ellenőrzik a Hybrid kontextust, táblareonstrukciót,
azonosítókat, hashértékeket, idempotenciát és megszakított publikálást.
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
- `model.py`: verziózott manifest és a `CHUNKING`/`CHUNKED` átmenetek.
- `storage.py`: hash, stabilitás, útvonal-ellenőrzés, atomi JSON-tárolás.
- `converter.py`: injektálható adapter; Docling-függőség elkülönítve.
- `chunker.py`: Hybrid adapter, táblareonstrukció, fingerprint/UUID5 és atomi csomag.
- `pipeline.py`: átvétel, konverzió, chunkolás, folytatás és dokumentumszintű hibák.
- `cli.py`: watch/process/status és exit code-ok.

A chunk JSON a harmadik lépcső kanonikus bemenete. A PostgreSQL később nem oszt új
`document_id` vagy `chunk_id` értéket; PostgreSQL és Qdrant ugyanazt a most képzett
azonosítót használja. A Qdrant point ID közvetlenül lehet a UUID-kompatibilis
`chunk_id`, payloadja pedig majd a kereséshez/szűréshez szükséges metadata
denormalizált részhalmazát kapja. Az adminisztratív metadata PostgreSQL-ben marad,
a technikai azonosítók pedig továbbra sem részei az embedding szövegnek.

Ismert korlát: a strukturális útvonal a Docling által felismert címsorokra épül;
hibás forrásfelismerést nem próbál saját dokumentumparserrel javítani. Táblafejlécet
csak explicit Docling-jelölésből fogad el. PostgreSQL, Qdrant, embedding, DONE-ba
mozgatás és automatikus ERROR-retry a következő lépcsők feladata.

A `.gitignore` kizárja az alapértelmezett adatkönyvtárak teljes tartalmát a
`.gitkeep` kivételével, valamint a `.env`, `.venv`, log, build és cache fájlokat.
Egyedi adatkönyvtárat a repositoryn kívül használj, vagy előbb külön ignoráld.
Felhasználói inputot, JSON-t és feldolgozási eredményt ne adj Githez.
