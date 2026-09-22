# Távoli MyPyRag MCP telepítés

Az MCP egy külön, modell- és adatbázis-kapcsolat nélküli folyamat. A Continue a
`http://192.168.3.4:8766/mcp` Streamable HTTP végpontot hívja, az MCP pedig a
Nagypapin loopbacken futó `http://127.0.0.1:8765` RAG service `POST /search` és
`GET /health` végpontját használja. A worker nem függősége egyik service-nek sem.

## Konfiguráció

A Nagypapi meglévő `/home/cztadmin/projects/MyPyRag/.env` fájlját egészítsd ki:

```dotenv
MYPYRAG_MCP_HOST=0.0.0.0
MYPYRAG_MCP_PORT=8766
MYPYRAG_MCP_PATH=/mcp
MYPYRAG_MCP_RAG_SERVICE_URL=http://127.0.0.1:8765
MYPYRAG_MCP_CONNECT_TIMEOUT_SECONDS=10
MYPYRAG_MCP_READ_TIMEOUT_SECONDS=180
MYPYRAG_MCP_HEALTH_TIMEOUT_SECONDS=5
MYPYRAG_MCP_ACCESS_TOKEN=<külön-hosszú-véletlen-token>
MYPYRAG_MCP_ALLOWED_HOSTS=192.168.3.4:8766,127.0.0.1:8766,localhost:8766
MYPYRAG_MCP_ALLOWED_ORIGINS=
```

`MYPYRAG_API_TOKEN` marad a RAG service saját backend tokenje; az MCP ezt csak
a loopback backendhívásokhoz használja. A külön `MYPYRAG_MCP_ACCESS_TOKEN` a
Continue → MCP kapcsolatot védi. Egyik titkot se commitold. Az üres origin-lista
szándékos: a natív Continue kliens Origin fejléc nélkül érkezhet, ezt az SDK
elfogadja; böngészős klienshez csak a pontos origin értéket add meg.

A jelenlegi telepítés izolált LAN-on sima HTTP, tehát bearer hitelesített, de nem
titkosított. Internetes routerportot ne nyiss hozzá. Nem megbízható LAN vagy Wi-Fi
esetén a végpont elé a meglévő infrastruktúrához illő TLS reverse proxy szükséges.

## Függőségek és systemd

```bash
cd /home/cztadmin/projects/MyPyRag
uv sync --locked --python 3.12
sudo bash ./deploy/install-systemd.sh \
  --repo /home/cztadmin/projects/MyPyRag \
  --user cztadmin \
  --env-file /home/cztadmin/projects/MyPyRag/.env
```

A script nem írja az `.env` fájlt. A két unit korábbi változatáról `.bak` mentést
készít, abszolút virtualenv-beli indítókat használ, majd `daemon-reload`, `enable`
és `restart` műveletet végez. A reranker cache-t még online, a service userrel
készítsd elő; utána igény szerint állítsd `HF_HUB_OFFLINE=1` értékre az unitban.
Portütközésnél előbb `ss -ltnp | grep ':8765\|:8766'` paranccsal azonosítsd a
folyamatot; a script nem állít le ismeretlen processt.

Kézi fejlesztői indítás:

```bash
uv run --cache-dir .uv-cache mypyrag mcp
# vagy
uv run --cache-dir .uv-cache mypyrag-mcp
```

Ellenőrzés és napló:

```bash
systemctl is-enabled mypyrag-service mypyrag-mcp
systemctl status mypyrag-service mypyrag-mcp --no-pager
journalctl -u mypyrag-service -u mypyrag-mcp -n 100 --no-pager
```

Az `After` csak indítási sorrend. Az MCP nem `Requires` függőséggel kapcsolódik a
backendhez, ezért backendhiba alatt is elindul, listázza az eszközöket, és a
`get_rag_status` diagnosztikát ad. A következő keresés a backend helyreállása után
MCP-restart nélkül újra próbálkozik.

A RAG `/health` a PostgreSQL mellett rövid, embeddinget nem készítő Ollama- és
modellnév/digest-ellenőrzést is végez. Így az MCP státusz nem jelez `ready`
állapotot, ha a HTTP processz fut, de a kereséshez szükséges Ollama nem érhető el.

## Continue

A repó `.continue/mcpServers/mypyrag.yaml` fájlját a Continue automatikusan
betölti; alternatívaként illeszd az `mcpServers` elemet a Windows
`%USERPROFILE%\.continue\config.yaml` fájlba. A token kerüljön a projekt
`.continue/.env` fájljába (gitignore-olt fájl legyen), vagy a globális
`%USERPROFILE%\.continue\.env` fájlba:

```dotenv
MYPYRAG_MCP_ACCESS_TOKEN=<ugyanaz-a-token-mint-a-Nagypapin>
```

Mentés után a Continue automatikusan újratölti a konfigurációt; ha nem, használd
a **Developer: Reload Window** parancsot. Agent mód szükséges, és az Ollama
modellnek támogatnia kell a tool callingot (`tool_use`). A példa nem tartalmaz
helyi `command`, Python-, Node- vagy proxyindítást, és nem írja felül a modelleket.

Próba Agent módban:

> Hívd meg a MyPyRag search_docs eszközét a retro.c64 universe-ben ezzel a
> kérdéssel: What does the BASIC PRINT command do and what is its syntax? Ezután
> a kapott részletek alapján válaszolj, és nevezd meg a forrásdokumentumot. Ha a
> kereső nem elérhető vagy nincs elég információ, azt jelezd.

A siker bizonyítéka a Continue tool-call részleteiben látható `search_docs` hívás
és a visszakapott forrásmetaadat, nem pusztán a modell szöveges állítása.
