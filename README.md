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
jelöli. Új dokumentum alapértéke `n.a.`, ezért indexelés előtt engedélyezett
universe-t kell választani:

```powershell
uv run mypyrag set-universe docs/IN/<document-directory> retro
```

A parancs `CONVERTED`, `CHUNKED`, illetve javítható `ERROR` állapotban használható,
és csak a manifestet módosítja atomikusan. `INDEXING`, `INDEXED` és `DONE`
állapotban universe-váltáshoz újraindexelési folyamat szükséges. A lista
kisbetűsre normalizált és környezeti változóban állítható:

```dotenv
MYPYRAG_ALLOWED_UNIVERSES=retro,minecraft,java,python,personal_notes
```

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
uv run mypyrag process path/to/file.pdf
uv run mypyrag watch
uv run mypyrag status
uv run mypyrag resume docs/ERROR/<document-directory>
uv run mypyrag retry-errors
```

## Szemantikus keresés

```powershell
uv run mypyrag search "How does RAMTAS initialize memory?" --universe retro --limit 5
uv run mypyrag search "How does RAMTAS initialize memory?" --universe retro --json
```

A query ugyanazzal a modellel és normalizálással kap embeddinget. Az universe
szűrés és a cosine distance szerinti rendezés paraméterezett SQL-ben történik;
nem töltődnek le a vektorok Pythonba. A megjelenített similarity képlete
`1 - cosine_distance`. A `--json` stabil találati objektumokat ad a későbbi
RAG/MCP réteg számára. Ez a réteg már használhatja a `SearchService` interfészt;
LLM-válaszgenerálás, reranking, MCP és webes felület még nincs implementálva.

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

Ezekhez a szolgáltatásokat és titkokat környezeti változóban kell megadni. A
normál tesztfutás fake embedding providert és fake repositoryt használ; lefedi a
teljes `CHUNKED → DONE` utat anélkül, hogy külső adatot módosítana.

## Ismert korlátok

- Egyetlen, rögzített 768 dimenziós embeddingmodell és cosine metrika támogatott.
- Nincs párhuzamos chunkgeneráció vagy történeti indexverzió.
- Nincs reranker, válaszgenerálás, MCP/Continue integráció vagy admin UI.
- A PostgreSQL elérése Windowsról kézzel fenntartott SSH-tunnelt igényel.
