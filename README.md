# MyPyRag

Újraindítható dokumentumfeldolgozó és szemantikus kereső Python 3.12-höz. A
Docling veszteségmentes JSON-t készít, a strukturált chunkok embeddingje pedig
egyetlen PostgreSQL 17 + pgvector katalógusba kerül. Qdrantot a projekt jelenleg
nem használ.

## Feldolgozási folyamat

```text
forrásfájl
  → RECEIVED → CONVERTING → CONVERTED
  → CHUNKING → CHUNKED
  → INDEXING → INDEXED
  → TXT-takarítás → DONE
```

`MYPYRAG_MAX_STAGE` értéke `CONVERTED`, `CHUNKED` vagy `INDEXED` lehet. Az első
két érték mellett az alkalmazás nem igényel adatbázis- vagy Ollama-kapcsolatot.
`INDEXED` a teljes folyamatot `DONE` állapotig viszi.

A manifest `universe` mezője a szemantikai témakör. Nem azonos a
`document_type` mezővel, amely a forrásformátumot (`pdf`, `html`, `md`, …)
jelöli. Automatikus ingestionnél az universe egyetlen igazságforrása a
`docs/IN` alatti könyvtárfa:

```text
docs/IN/retro/history.pdf       → retro
docs/IN/retro/c64/manual.htm    → retro.c64
docs/IN/java/spring/guide.pdf   → java.spring
```

A könyvtárszegmens mintája `[a-z0-9][a-z0-9_-]*`: csak kisbetűs ASCII,
szám, kötőjel és aláhúzás használható. Egy szegmens legfeljebb 32, a teljes
pontozott érték 128 karakter, az alapértelmezett maximális mélység 8. Ezek a
korlátok rendre `MYPYRAG_UNIVERSE_SEGMENT_MAX_LENGTH`,
`MYPYRAG_UNIVERSE_MAX_LENGTH` és `MYPYRAG_UNIVERSE_MAX_DEPTH` alatt állíthatók.
A korábbi `MYPYRAG_ALLOWED_UNIVERSES` változó elavult és nincs hatása.

Universe-könyvtárak létrehozása Linuxon:

```bash
mkdir -p docs/IN/retro/c64 docs/IN/retro/zx docs/IN/java/spring
```

PowerShellben:

```powershell
New-Item -ItemType Directory -Force `
  docs/IN/retro/c64, docs/IN/retro/zx, docs/IN/java/spring
```

A watcher rekurzív, de nem követ symlinket vagy külön mountot, kihagyja a
rejtett/technikai fájlokat, és a közvetlen `manifest.json` vagy `receipt.json`
fájlt tartalmazó work directory teljes fáját már bejárás előtt prune-olja. Így
a saját `source`, `JSON` és `CHUNKS` eredményei nem válhatnak új inputtá. Az
`IN` gyökerébe tett fájl besorolatlan: helyben marad, és csak egyszeri
figyelmeztetést kap változatlan állapot mellett.

Teljes automatikus példa:

```bash
mkdir -p docs/IN/retro/c64
cp c64_memory_map.htm docs/IN/retro/c64/
MYPYRAG_MAX_STAGE=INDEXED uv run mypyrag watch
```

Az első manifest már `universe = retro.c64` értékkel készül, majd a pipeline
emberi beavatkozás nélkül halad `DONE` állapotig. Az explicit `process` ugyanígy
származtat universe-t, ha a fájl érvényes IN-útvonalon van. IN-en kívüli vagy
közvetlenül az IN gyökerében megadott fájlnál a kompatibilitási `n.a.` marad.

A `set-universe` megmarad régi vagy külső dokumentumok javítására:

```bash
uv run mypyrag set-universe docs/IN/<document-directory> retro.c64
```

Csak akkor fogadja el az értéket, ha a megfelelő `docs/IN/retro/c64` normál,
nem symlink könyvtár létezik. `CONVERTED`, `CHUNKED`, illetve javítható `ERROR`
állapotban használható; `INDEXING`, `INDEXED` és `DONE` után újraindexelés kell.

## Telepítés és konfiguráció

Windows és Linux alatt is szükséges a Python 3.12 és az `uv`. A Docling helyi
futtatásához Java 21 ajánlott. A Gradle csak vékony vezérlőréteg; Gradle wrapper
nincs a repositoryban.

```powershell
uv sync --locked --python 3.12 --cache-dir .uv-cache
Copy-Item .env.example .env
```

Linuxon a második parancs `cp .env.example .env`. Az `.env` Git által figyelmen
kívül marad; valódi jelszó soha ne kerüljön az `.env.example` fájlba.

A Nagypapin futó adatbázis célja:

```dotenv
MYPYRAG_POSTGRES_HOST=127.0.0.1
MYPYRAG_POSTGRES_PORT=5436
MYPYRAG_POSTGRES_DATABASE=mypyrag
MYPYRAG_POSTGRES_USER=mypyrag_app
MYPYRAG_POSTGRES_PASSWORD=<helyi-titok>
MYPYRAG_POSTGRES_SCHEMA=mypyrag
MYPYRAG_POSTGRES_SSLMODE=disable
```

A `127.0.0.1:5436` csak Nagypapin közvetlenül használható. Windowsról külön
terminálban tarts fenn SSH-tunnelt; a DB-portot ne nyisd ki a LAN-ra:

```powershell
plink.exe -batch -agent -N -P 2227 -L 5436:127.0.0.1:5436 cztadmin@192.168.3.4
```

Az alkalmazás ekkor szintén `127.0.0.1:5436`-hoz kapcsolódik. Ha a helyi 5436
foglalt, válassz másik helyi tunnelportot, és csak
`MYPYRAG_POSTGRES_PORT` értékét igazítsd hozzá.

Az embedding szolgáltatás alapkonfigurációja:

```dotenv
MYPYRAG_OLLAMA_URL=http://192.168.3.32:11434
MYPYRAG_EMBEDDING_MODEL=nomic-embed-text:latest
MYPYRAG_EMBEDDING_VECTOR_SIZE=768
MYPYRAG_EMBEDDING_MODEL_DIGEST=0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f
MYPYRAG_EMBEDDING_TIMEOUT_SECONDS=120
MYPYRAG_EMBEDDING_BATCH_SIZE=16
```

Induláskor az adapter ellenőrzi az Ollamát, a modell nevét és konfigurált
digestjét, majd egy kontroll embeddinggel a 768 dimenziót és a véges értékeket.

## Adatbázis és migráció

A verziózott SQL migrációk a `src/mypyrag/migrations` csomagban vannak. A
migráció tranzakciós, advisory lockkal sorosított, biztonságosan idézett
sémanévvel dolgozik, és külön `schema_migrations` táblában tartja a verziót.
A `vector` extension telepítése infrastruktúra-feladat; az alkalmazás csak
ellenőrzi.

```powershell
uv run mypyrag db migrate
uv run mypyrag db status
```

Telepített Gradle esetén ugyanez:

```text
gradle dbMigrate
gradle dbStatus
```

A `documents` tábla a dokumentumkatalógus és az indexelési metaadatok gazdája.
A `chunks` tábla determinisztikus UUID-ket, normalizált embeddingbemenetet,
metaadatokat és `vector(768)` értékeket tárol. A chunkok universe szerint
szűrhetők; a vektorindex HNSW és `vector_cosine_ops` operator class-t használ.

Minden dokumentum frissítése rövid tranzakció: document UPSERT, kizárólag az
adott `document_id` régi chunkjainak törlése, új chunkok beszúrása, majd
darabszám- és universe-ellenőrzés. Az embeddingek már a tranzakció előtt
elkészülnek. Újrachunkoláskor nincs generációverziózás: siker után csak az
aktuális készlet marad. Hiba rollbacket okoz, így a korábbi készlet érintetlen.

Adatbázis-migráció előtt az üzemeltető felelőssége a mentés. Az alkalmazás nem
módosít Docker compose fájlt, PostgreSQL főverziót, volume-ot vagy más projekt
adatbázisát.

## Artifactok, újraindítás és hibakezelés

Az indexelő kizárólag a `CHUNKS/*.json` fájlokat tekinti kanonikus bemenetnek.
Ellenőrzi az azonosítókat, UUID-ket, hézagmentes indexeket, SHA-256 hasheket,
fingerprintet, típust és darabszámot még hálózati hívás előtt.

Az eredeti `embedding_text` nem változik a JSON-ban. A DB-be kerülő változatban
csak a sortörések normalizálódnak (`CRLF → LF`, majd `CR → LF`). Az eredeti
`embedding_text_sha256` mellett külön `embedding_input_sha256` készül a
normalizált UTF-8 szövegből.

Sikeres DB-commit után lesz a manifest `INDEXED`. Ezután a redundáns chunk TXT-k
törlődnek, míg a chunk JSON-ok, a Docling JSON és az eredeti forrás megmaradnak,
majd a könyvtár `docs/DONE` alá kerül. Megszakítás után az ismétlés idempotens:
`INDEXED` állapotból nincs új embedding, csak a takarítás és mozgatás fejeződik
be. Dokumentumhiba az adott elemet `ERROR` alá izolálja.

```powershell
uv run mypyrag process docs/IN/retro/c64/manual.pdf
uv run mypyrag watch
uv run mypyrag status
uv run mypyrag resume docs/ERROR/<document-directory>
uv run mypyrag retry-errors
```

## Szemantikus keresés

```powershell
uv run mypyrag search "How does RAMTAS initialize memory?" --universe retro --limit 5
uv run mypyrag search "How does RAMTAS initialize memory?" --universe retro --json
uv run mypyrag list-universes
uv run mypyrag list-universes --json
```

A query ugyanazzal a modellel és normalizálással kap embeddinget. Az universe
szintaxisát ellenőrzi, de nem függ az aktuális IN könyvtárfától. Az indexelt
universe-ok katalógusa a PostgreSQL, ezért egy inboxból később eltávolított
universe továbbra is kereshető. A `list-universes` universe-onként dokumentum-
és chunkszámot ad közvetlenül a DB-ből, Ollama-hívás nélkül. A szűrés és a
cosine distance szerinti rendezés paraméterezett SQL-ben történik;
nem töltődnek le a vektorok Pythonba. A megjelenített similarity képlete
`1 - cosine_distance`. A `--json` stabil találati objektumokat ad a későbbi
RAG/MCP réteg számára. Ez a réteg már használhatja a `SearchService` interfészt;
LLM-válaszgenerálás, MCP és webes felület nincs ebben a rétegben.

### Kétlépcsős keresés és reranking

A közös `SearchService` előbb universe- és útvonalszűréssel kér vektoros
jelölteket, majd a service indulásakor egyszer betöltött valódi cross-encoderrel
újrarendezi őket. A logikai beállítások környezeti megfeleltetése:

| Logikai beállítás | Környezeti kulcs | Alapérték |
| --- | --- | --- |
| `search.candidate_top_k` | `MYPYRAG_SEARCH_CANDIDATE_TOP_K` | `20` |
| `search.result_top_k` | a kompatibilis `MYPYRAG_SEARCH_DEFAULT_LIMIT` | `5` |
| végső limit felső korlátja | `MYPYRAG_SEARCH_MAX_LIMIT` | `50` |
| `search.rerank.enabled` | `MYPYRAG_SEARCH_RERANK_ENABLED` | `true` |
| `search.rerank.model` | `MYPYRAG_SEARCH_RERANK_MODEL` | `cross-encoder/ms-marco-MiniLM-L6-v2` |
| `search.rerank.device` | `MYPYRAG_SEARCH_RERANK_DEVICE` | `cpu` |
| `search.rerank.batch_size` | `MYPYRAG_SEARCH_RERANK_BATCH_SIZE` | `8` |
| inference konkurencia | `MYPYRAG_SEARCH_RERANK_MAX_CONCURRENCY` | `1` |
| PyTorch CPU-szálak | `MYPYRAG_SEARCH_RERANK_THREADS` | `2` |

A számlálók pozitív egészek, és az alapértelmezett végső limit nem lehet nagyobb
a jelöltkeretnél. Az eddigi explicit HTTP `top_k` és CLI `--limit` továbbra is a
végső találatszámot kéri a meglévő maximumig. Az effektív jelöltkeret
`max(MYPYRAG_SEARCH_CANDIDATE_TOP_K, kért végső limit)`. Kikapcsoláshoz állítsd
`MYPYRAG_SEARCH_RERANK_ENABLED=false` értékre; ilyenkor modell sem töltődik be,
a vektorsorrend marad, és a `rerank_score` értéke `null`.

A modellinput az eredeti chunk `text` értéke és az egyszer szereplő,
deduplikált `structural_path`/cím kontextus. A chunk törzse áll elöl, a rövid
kontextus utána, így jobbra csonkolásnál a törzs élvez elsőbbséget. A kliensnek visszaadott `text` mindig
az eredeti, nem a tokenizer által levágott változat. A tényleges maximális
bemenethosszt a betöltött tokenizer adja (a kiindulási modellnél jellemzően 512
token); a tokenizer `longest_first` szabállyal csonkolja a `(query, dokumentum)`
párt, így a jellemzően rövidebb query megmarad. A service naplózza
a tényleges maximumot és keresésenként a csonkolt párok számát.

A service szinkron FastAPI handlerét a framework worker threadben futtatja, így
a CPU inference nem blokkolja az async event loopot. Processzenként egy modell,
alapból egyidejűleg egy inference és két PyTorch CPU-szál használható; ha a
konkurencialimit foglalt, a kérés 503 hibát kap korlátlan belső sor helyett. Ez a
2 magos HP és a párhuzamos worker számára konzervatív kiindulás, helyszíni
méréssel módosítható.

Az első bekapcsolt indulás a Hugging Face-ről letölti a modellt a szokásos
helyi cache-be. Offline újraindítás akkor működik, ha ez a cache megmaradt (a
felesleges hálózati próbák elkerülésére `HF_HUB_OFFLINE=1` is beállítható), vagy
ha `MYPYRAG_SEARCH_RERANK_MODEL` egy előre letöltött helyi modellkönyvtárra
mutat. Betöltési hiba megszakítja a service indulását; inference-hibánál nincs
néma visszaesés vektorsorrendre. A worker nem importálja és nem inicializálja a
rerankert.

A válaszban a korábbi `score` továbbra is `1 - cosine_distance`. A külön
`rerank_score` a cross-encoder nyers értéke, lehet negatív vagy 1-nél nagyobb;
a két score nem azonos skála és nem százalék. Bekapcsolva a lista
`rerank_score` szerint rendezett, ezért a régi `score` nem feltétlenül monoton.
A felső `metadata` tartalmazza a `rerank_applied` jelzőt, az effektív limiteket,
a darabszámokat, a truncation számlálót, a modellt, valamint az embedding,
retrieval, reranking és teljes időt. INFO naplóba nem kerül dokumentumszöveg;
DEBUG szinten a méréshez elérhető a chunk-azonosítók előtti/utáni sorrendje.

A kiindulási MS MARCO MiniLM modell angol lekérdezésekre és angol C64-anyagra
baseline. Magyar, illetve magyar→angol keresési minőséget ez a változtatás nem
igazol, és automatikus fordítást nem végez. A reranker csak a vektoros jelöltek
közül választ: ha azok között nincs magyarázó chunk, nem tud ilyet előállítani.

## Külön worker és HTTP service

CR-002 óta a hosszú ingestion és a keresési API két önálló processz. Először
futtasd a 2-es adatbázis-migrációt, majd indítsd őket külön terminálból:

```powershell
uv run mypyrag db migrate
uv run mypyrag-worker
uv run mypyrag-service
```

A modul formájú indítás is támogatott: `python -m mypyrag.worker` és
`python -m mypyrag.service`. A service alapértelmezetten a biztonságos
`127.0.0.1:8765` címen figyel; LAN-eléréshez állítsd be a
`MYPYRAG_SERVICE_HOST=0.0.0.0` értéket és csak a 8765-ös HTTP portot engedd át.
A PostgreSQL maradjon loopbackre kötve. A service indulásához kötelező a
`MYPYRAG_API_TOKEN`.

```powershell
curl http://127.0.0.1:8765/health
curl.exe -X POST http://127.0.0.1:8765/search `
  -H "Authorization: Bearer $env:MYPYRAG_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{"query":"What is CHROUT?","universe":"retro.c64","top_k":6}'
curl.exe http://127.0.0.1:8765/universes `
  -H "Authorization: Bearer $env:MYPYRAG_API_TOKEN"
curl.exe "http://127.0.0.1:8765/sources?status=ready" `
  -H "Authorization: Bearer $env:MYPYRAG_API_TOKEN"
```

A `/health` nyilvános; a `/search`, `/universes` és `/sources` bearer tokent
igényel. A dokumentumkatalógus `queued`, `processing`, `ready` és `failed`
állapotot tárol. A keresési SQL a chunkokat a dokumentumtáblához kapcsolja, és
kizárólag `ready` dokumentumokat enged át, így a worker többórás feldolgozása
közben a service a már kész tartalomból továbbra is válaszol.

## Távoli MCP a Continue Agent számára

A külön `mypyrag-mcp` (vagy `mypyrag mcp`) folyamat Streamable HTTP-n a
`search_docs(query, universe)`, `get_rag_status()` és az opcionális
`web_search(query, max_results)` eszközt szolgáltatja. Csak
a meglévő HTTP service-t hívja; nem tölt embedding- vagy rerankermodellt, és nem
kapcsolódik közvetlenül a PostgreSQL-hez. A Nagypapi systemd- és Continue-
beállításainak teljes, megismételhető leírása: [docs/mcp-deployment.md](docs/mcp-deployment.md).
A Continue bizonyítékkeresési szabálya: [docs/continue-web-search.md](docs/continue-web-search.md).
Módosított fájlnál a korábbi `ready` revízió a teljes feldolgozás alatt
kereshető marad; a sikeres új indexre váltás és a régi revízió eltávolítása
egyetlen adatbázis-tranzakcióban történik.

## Ellenőrzés

Az alapértelmezett tesztcsomag nem igényel hálózatot, valódi PostgreSQL-t,
Ollamát vagy Docling-modellletöltést:

```powershell
uv run ruff check .
uv run mypy
uv run pytest
```

Az `integration` markerű tesztek külön, explicit konfigurált környezetben
futtathatók:

```powershell
uv run pytest -m integration
```

A valódi cross-encoder külön smoke tesztje (modellletöltéssel vagy meglévő
cache-ből) célzottan futtatható:

```powershell
$env:MYPYRAG_RUN_RERANKER_SMOKE="1"
uv run pytest -m integration tests/test_reranking.py
```

A 20→5, 50→5 és 100→5 összehasonlításhoz állítsd rendre a
`MYPYRAG_SEARCH_CANDIDATE_TOP_K` értékét, indítsd újra a service-t, és ugyanazon
query/universe mellett rögzítsd a válasz `metadata.timings_ms` mezőit és a DEBUG
előtti/utáni chunk-sorrendet. A 20→5 marad az alapérték, amíg a HP-n mért
eredmények más beállítást nem indokolnak.

Ezekhez a szolgáltatásokat és titkokat környezeti változóban kell megadni. A
normál tesztfutás fake embedding providert és fake repositoryt használ; lefedi a
teljes `CHUNKED → DONE` utat anélkül, hogy külső adatot módosítana.

## Ismert korlátok

- Egyetlen, rögzített 768 dimenziós embeddingmodell és cosine metrika támogatott.
- Nincs párhuzamos chunkgeneráció vagy történeti indexverzió.
- Nincs reranker, válaszgenerálás, MCP/Continue integráció vagy admin UI.
- A PostgreSQL elérése Windowsról kézzel fenntartott SSH-tunnelt igényel.
