# QwenTerelő MCP workflow prototípus

## Döntés

Maradjon **egy HomeLLM/MyPyRag MCP szerver, belső modulokkal**. Így egy helyen marad az
azonosítás, a transport, a toolnév-tér, a policy és a diagnosztika. Külön szerver csak akkor
indokolt, ha a workflow-k eltérő gépen vagy eltérő jogosultsággal futnak, illetve ha külön
üzemeltetési életciklust igényelnek.

A prototípus nem refaktorálja a meglévő RAG és web kódot. Egyetlen új modult és egyetlen új
MCP tool-regisztrációt ad hozzá.

## Könyvtár- és package-struktúra

Jelenlegi minimális állapot:

```text
src/mypyrag/
  mcp_server.py       # transport, auth és tool-regisztráció
  qwen_terelo.py      # run_evidence_file_probe workflow
  web_search.py       # meglévő web modul
tests/
  test_qwen_terelo.py
  test_mcp_server.py
```

Javasolt bővítés csak akkor, amikor a következő workflow ténylegesen elkészül:

```text
src/mypyrag/mcp/
  rag.py
  web.py
  qwen_terelo.py
  diagnostics.py
  policy.py
```

Az azonnali package-átrendezés nem adna értéket a kísérlethez, ezért most szándékosan elmarad.

## Első implementációs lépések

1. `run_evidence_file_probe` izolált Python workflow és egységtesztek.
2. Regisztráció a meglévő `MCPServer` példányon.
3. Workspace-root konfigurálása `MYPYRAG_MCP_WORKSPACE_DIR` értékkel.
4. MCP protokollteszt a tool sémájára és egy sikeres hívásra.
5. Kontrollált Continue/Roo kísérlet ugyanazzal a feladatsorral: atomi toolkészlet és csak
   magas szintű toolkészlet összehasonlítása.

## `run_evidence_file_probe` specifikáció

### Bemenet

| Mező | Típus | Szabály |
| --- | --- | --- |
| `target_path` | string | Relatív út a konfigurált workspace-en belül. Abszolút, kitérő, VCS-meta és linkelt út tiltott. |
| `initial_content` | string | UTF-8-ként, newline-átalakítás nélkül kiírandó tartalom. |
| `append_content` | string | UTF-8-ként, newline-átalakítás nélkül hozzáfűzendő tartalom. |
| `mode` | enum | `fail_if_exists` vagy `overwrite_allowed`. |
| `verify` | boolean | `true`: mindkét visszaolvasás és pontos egyezés kötelező; `false`: ezek a lépések `SKIPPED` állapotúak. |

### Szerveroldali invariánsok

- Nem fut shell parancsot.
- Legfeljebb nyolc névvel ellátott workflow-lépést rögzít.
- A teljes workflow egy szerverfolyamaton belül sorosított, ezért két hívás nem ír egymásba.
- `fail_if_exists` exkluzív fájllétrehozást használ, így az ellenőrzés és írás közti verseny is
  kontrollált elutasítássá válik.
- A végállapot hash-e és mérete a tényleges fájlbájtokból készül.
- Azonos paraméterek SHA-256 fingerprintet, számlálót és `repeated_call=true` jelzést kapnak.
  A számláló folyamatmemóriában él, ezért újraindítás után nullázódik.
- A napló csak lépésnevet, állapotot és rövid evidence-t tartalmaz; a fájltartalmat nem írja ki.

### Kimenet

Minden normál kimenet JSON-objektum. Közös mezők: `status`, `tool`, `steps`, `final_state`,
`diagnostics`, `next_allowed_actions`. `FAIL` és `REJECTED` esetén `reason` és rövid `message`
is van.

- `PASS`: a kért írás/append befejeződött, és `verify=true` esetén mindkét ellenőrzés sikerült.
- `FAIL`: a workflow elkezdődött, de I/O- vagy tartalmi ellenőrzési hiba történt.
- `REJECTED`: a policy vagy a precondition már a művelet előtt megállította a hívást.

A `final_state.path` szerveroldali abszolút út. A kliens ne használja új bemenetként; a következő
hívás továbbra is relatív `target_path` értéket vár.

## Tesztterv és lefedettség

| Eset | Elvárt eredmény | Implementálva |
| --- | --- | --- |
| Sikeres create/read/verify/append/read/verify | `PASS`, 8 `OK`, pontos hash és méret | igen |
| A cél már létezik | `REJECTED/target_already_exists`, nincs módosítás | igen |
| Érvénytelen út | `REJECTED/invalid_path` abszolút, `..`, `.git` és symlink esetén | igen |
| Visszaolvasási eltérés | `FAIL/initial_content_mismatch`, diagnosztizálható végállapot | igen, I/O seam segítségével |
| Azonos ismételt hívás | azonos fingerprint, növekvő számláló, `repeated_call=true` | igen |
| `verify=false` | írás sikerül, négy ellenőrzési lépés `SKIPPED` | igen |
| MCP publikált séma és valódi hívás | enum séma, write annotation, `PASS` | igen |

Későbbi tesztek: többprocesszes azonos célfájl, lemezbetelés/jogosultsághiba, szerver-restart utáni
trace-viselkedés és Linux junction/mount boundary.

## Következő workflow-k terve

### `inspect_project_state`

Csak korlátozott inventoryt adjon: root fájltípusok, ismert konfigurációk, projekt-típus,
deklarált tesztparancsok és rövid porcelain git összesítés. Ne adjon teljes rekurzív fájllistát,
fájltartalmat vagy tetszőleges parancskimenetet. A git lekérdezés fix argumentumlistával,
shell nélkül fusson, timeouttal és outputlimittel.

### `prepare_rag_context`

Egyetlen szerveroldali hívás validálja a `query`, `universe`, `max_chunks` értékeket, meghívja a
meglévő RAG klienst, normalizálja és levágja a chunkokat, majd `high|medium|low` confidence-ot
számít dokumentált küszöbökből. Adjon source/chunk ID-t, score-t, rövid minősítést és
`next_allowed_actions` mezőt; ne generáljon végső választ.

### `prepare_rag_or_web_context`

Fix RAG-first state machine legyen. Web fallback csak akkor induljon, ha a találatszám vagy a
dokumentált score/confidence küszöb nem teljesül. Az evidence packet külön jelölje a lokális
chunkot, keresési snippetet és ténylegesen letöltött oldalt; snippet önmagában ne minősüljön
ellenőrzött webes bizonyítéknak.

### `apply_verified_patch`

Kétfázisú protokoll: `dry_run` kanonikus patch-hash-t, érintett fájlokat, diff-előnézetet és
rövid élettartamú apply tokent ad. `apply` csak ugyanarra a workspace snapshotra és patch-hashre
érvényes tokennel ír. Alkalmazás után visszaolvasás, hash/diff ellenőrzés és opcionális, előre
engedélyezett tesztprofil fusson; tetszőleges shell-paraméter ne legyen bemenet.

## Continue integráció

Javasolt külön Qwen agent profil:

- csak a HomeLLM MCP maradjon bekapcsolva;
- a beépített create/edit/terminal és általános web toolok policy-je legyen `Excluded`;
- a workflow tool módosító volta miatt először `Ask First`, csak stabil kísérlet után `Automatic`;
- a `.continue/rules` alatt mindig alkalmazott szabály mondja ki:
  1. csak a látható, pontos toolnevek használhatók;
  2. toolnevet kitalálni tilos;
  3. XML-, Markdown-, JSON- vagy próza alakú ál-toolhívás tilos;
  4. ha nincs megfelelő tool, `ask_user` vagy megállás következik;
  5. minden válasz után kizárólag a `next_allowed_actions` egyik eleme választható;
  6. `repeated_call=true` esetén változatlan paraméterrel tilos újrahívni.

A Continue IDE-ben a tool policy három állapota `Ask First`, `Automatic`, `Excluded`, és a
toolcsoportok is ki-be kapcsolhatók. A policy jelenleg felhasználónként, lokálisan tárolódik, ezért
a repó rule-ja önmagában nem biztonsági határ; a reprodukálható kísérlethez képernyőmentés vagy
exportált policy-leltár is kell. A modell agent system message-e külön is felülírható
`chatOptions.baseAgentSystemMessage` alatt. Források: [Continue Agent testreszabás][continue-agent]
és [Continue rules][continue-rules].

## Roo integráció

Projekt-szintű `.roomodes` profilban csak az `mcp` csoport legyen engedélyezve; a `read`, `edit`
és `command` csoport maradjon ki. A `customInstructions` kapja ugyanazt a hatpontos protokollt.
A Roo custom mode tooljogosultsága csoportszintű (`read`, `edit`, `command`, `mcp`), ezért ez nem
jelent automatikusan HomeLLM-only per-tool allowlistet: a többi MCP szervert és fölösleges toolt a
Roo MCP panelen külön le kell tiltani. Források: [Roo custom modes][roo-modes] és
[Roo MCP használat][roo-mcp].

Minimális vázlat:

```yaml
customModes:
  - slug: qwen-terelo
    name: QwenTerelő
    roleDefinition: Determinisztikus HomeLLM workflow-kat használó lokális agent.
    groups:
      - mcp
    customInstructions: >-
      Csak a látható HomeLLM toolok pontos neveit használd. Ne írj XML/JSON/Markdown
      ál-toolhívást. Ha nincs megfelelő tool, kérdezz vagy állj meg. Mindig kövesd a
      next_allowed_actions mezőt; repeated_call=true esetén ne ismételj változatlanul.
```

## Kockázatok és nyitott kérdések

- Az `overwrite_allowed` szándékosan destruktív. Éles használat előtt érdemes külön, dedikált
  probe alkönyvtárra szűkíteni, vagy kétfázisú jóváhagyást adni hozzá.
- A path-ellenőrzés és a művelet között külső folyamat cserélhet könyvtárat/linket. A prototípus
  újraellenőriz és folyamaton belül sorosít, de erős többfelhasználós ellenféllel szemben
  descriptor-alapú, operációsrendszer-specifikus megnyitás kell.
- Az ismétlésdetektálás nem tartós és nem oszlik meg több worker között. Később request/trace
  tár vagy kis SQLite napló indokolt, TTL-lel és tartalmi titkok tárolása nélkül.
- Meg kell határozni a tartalom- és fájlméretlimitet, a timeoutot, a retentiont és azt, hogy a
  probe által létrehozott fájlokat ki takarítja el.
- A `verify=false` csak mechanikai smoke teszthez hasznos; Qwen számára alapértelmezetten
  `true` ajánlott.
- A `prepare_rag_context` confidence küszöbeit valódi korpuszon kell kalibrálni, nem intuitív
  konstansokból.
- A siker kritériuma kontrollált A/B mérés legyen: natív tool-call arány, hibás toolnév,
  pszeudohívás, loop, felhasználói beavatkozás, végeredmény és lépésszám.

[continue-agent]: https://docs.continue.dev/ide-extensions/agent/how-to-customize
[continue-rules]: https://docs.continue.dev/customize/deep-dives/rules
[roo-modes]: https://github.com/RooCodeInc/Roo-Code-Docs/blob/main/docs/features/custom-modes.mdx
[roo-mcp]: https://github.com/RooCodeInc/Roo-Code-Docs/blob/main/docs/features/mcp/using-mcp-in-roo.mdx
