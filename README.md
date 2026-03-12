# Matching mot Nasjonalbiblioteket (NB)

Dette prosjektet automatiserer identifikasjon av norske bøker i bibliotekets samling som mangler i Nasjonalbibliotekets katalog. Formålet er å identifisere unikt materiale som kan tilbys NB i tråd med deres bevaringspolicy.

## Prosjektmål
* Ekstrahere metadata for norske bøker fra Alma.
* Sammenligne metadata mot NB Catalog API.
* Identifisere mangler i NBs samling med høy presisjon.
* Redusere manuelt kontrollarbeid gjennom automatisert filtrering og scoring.

## Metodikk

### 1. Inngangsfiltrering (Norwegian Indicators)
Før matching starter, filtreres datasettet for å fjerne materiale som faller utenfor NBs primære bevaringsmandat. En post beholdes kun dersom den oppfyller minst ett av følgende kriterier:
* **Landkode:** Feltet `008` (posisjon 15-17) er satt til `no`.
* **ISBN-prefiks:** Inneholder et ISBN som starter med `82` eller `97882`.

### 2. Fase 1: ISBN-match
Skriptet utfører direkte oppslag på alle tilgjengelige ISBN-numre (10 og 13 siffer). Treff via ISBN regnes som sikre og krever ingen manuell kontroll.

### 3. Fase 2: Metadata-match (Scoring)
For poster uten ISBN-treff genereres søkestrenger basert på tittel, forfatter og utgiver. Kandidater fra NB API scores etter følgende modell:

| Kriterium | Handling | Poeng |
| :--- | :--- | :--- |
| **Tittel** | Eksakt match på hovedtittel eller full tittel | 60 |
| | Overlapp > 80% | 40 |
| | Overlapp > 50% | 20 |
| **Forfatter** | Eksakt match på etternavn (author_key) | 25 |
| | Delvis streng-match | 10 |
| **Årstall** | Eksakt årstall | 15 |
| | Avvik på +/- 1 år | 5 |
| **Utgiver** | Eksakt match på utgivernavn | 10 |
| | Delvis streng-match | 5 |

### 4. Klassifisering av resultater
Resultatene kategoriseres automatisk basert på totalscore og margin (differansen mellom beste og nest beste kandidat):

* **Confirmed:** Score ≥ 75 OG margin ≥ 10.
* **Confirmed (High Score):** Score ≥ 105. Her ignoreres marginen for å håndtere tilfeller der NB har duplikate poster for samme verk.
* **Needs Manual:** Score ≥ 45, men lav margin eller usikker identifikasjon.
* **Not Found:** Ingen kandidater oppnår tilstrekkelig score.

## Teknisk implementering
* **Språk:** Python 3.x
* **API:** NB Catalog API (v1)
* **Caching:** All API-respons lagres lokalt i `isbn_cache.json` og `query_cache.json` for å sikre reproduserbare resultater og redusere belastning på NB API.
* **Rate Limiting:** Skriptet har innebygd pause mellom forespørsler.

## Bruk
1. Plasser eksport fra Alma som `alma_export.csv` i rotmappen.
2. Kjør skriptet: `python match_alma_nb.py`.
3. Analyser resultater i `output/`-mappen. Hovedfokus for manuelt arbeid er `poster_til_manuell_kontroll.csv`.
