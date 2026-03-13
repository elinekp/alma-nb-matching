from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


# =========================
# KONFIGURASJON
# =========================

INPUT_CSV = "alma_export.csv"
OUTPUT_DIR = Path("output_12.03")

NB_ITEMS_URL = "https://api.nb.no/catalog/v1/items"

REQUEST_TIMEOUT = 20
REQUEST_SLEEP_SECONDS = 0.1

ISBN_CACHE_FILE = OUTPUT_DIR / "isbn_cache.json"
QUERY_CACHE_FILE = OUTPUT_DIR / "query_cache.json"
ERROR_LOG_FILE = OUTPUT_DIR / "feillogg_rader_som_ikke_ble_behandlet.csv"

# Kolonnenavn i Alma-CSV
COL_ID_CANDIDATES = ["MMS ID", "MMS_ID", "MMS Id", "mms_id", "Barcode", "barcode"]
COL_TITLE = "Title"
COL_YEAR = "Publication Date"
COL_AUTHOR = "Author"
COL_CONTRIBUTOR = "Author (contributor)"
COL_PUBLISHER = "Publisher"
COL_ISBN = "ISBN"
COL_COUNTRY_CODE = "BIB 008 MARC"

# Klassifiseringsterskler for kandidatmatch
CONFIRMED_MIN_SCORE = 75
CONFIRMED_MIN_MARGIN = 10
MANUAL_MIN_SCORE = 45


# =========================
# HJELPEFUNKSJONER: FIL/JSON
# =========================

def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_json_file(path: Path) -> Dict[str, Any]:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json_file(path: Path, data: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def append_error_row(row: Dict[str, Any], error_message: str) -> None:
    file_exists = ERROR_LOG_FILE.exists()
    with ERROR_LOG_FILE.open("a", encoding="utf-8-sig", newline="") as f:
        fieldnames = list(row.keys()) + ["error_message"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        row_copy = dict(row)
        row_copy["error_message"] = error_message
        writer.writerow(row_copy)


# =========================
# HJELPEFUNKSJONER: CSV
# =========================

def read_csv_rows(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def get_first_existing_value(row: Dict[str, str], keys: List[str]) -> str:
    for key in keys:
        value = row.get(key, "")
        if value and str(value).strip():
            return str(value).strip()
    return ""


def get_row_id(row: Dict[str, str], fallback_index: int) -> str:
    value = get_first_existing_value(row, COL_ID_CANDIDATES)
    if value:
        return value
    return f"row_{fallback_index}"


# =========================
# NORMALISERING
# =========================

def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[.,;:!?()\[\]\"'/\\\-]+", " ", text)
    text = normalize_whitespace(text)
    return text


def extract_main_title(text: str) -> str:
    text = text or ""
    parts = text.split(":")
    return normalize_text(parts[0])


def normalize_title_full(text: str) -> str:
    return normalize_text(text)


def strip_trailing_life_dates(text: str) -> str:
    # Fjerner enkle mønstre som "1941-" eller "1918-1985" på slutten
    return re.sub(r"\s+\d{4}(?:-\d{0,4})?\s*$", "", text or "").strip()


def normalize_author(text: str) -> str:
    text = (text or "").lower()
    text = text.replace(".", "")
    text = strip_trailing_life_dates(text)
    text = normalize_whitespace(text)
    return text


def author_key(text: str) -> str:
    text = normalize_author(text)
    if "," in text:
        return text.split(",", 1)[0].strip()
    return text

def split_contributors(text: str) -> List[str]:
    """
    Splitter contributor-felt på semikolon og fjerner tomme verdier.
    """
    if not text:
        return []

    parts = [part.strip() for part in text.split(";")]
    return [part for part in parts if part]

def extract_year(text: str) -> str:
    match = re.search(r"(1[0-9]{3}|20[0-9]{2})", text or "")
    return match.group(1) if match else ""


def normalize_publisher(text: str) -> str:
    return normalize_text(text)


def extract_isbn_candidates(raw: str) -> List[str]:
    """
    Trekker ut ISBN-10 og ISBN-13 fra tekstfelt.
    Håndterer flere ISBN i samme celle, f.eks. semikolonseparert.
    Tillater X som siste tegn i ISBN-10.
    """
    if not raw:
        return []

    text = raw.upper().strip()

    # Del først opp på vanlige separatorer mellom flere ISBN
    parts = re.split(r"[;,\|/]+", text)

    results = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Fjern alt unntatt sifre og X
        isbn = re.sub(r"[^0-9X]", "", part)

        if len(isbn) == 10 and re.fullmatch(r"\d{9}[0-9X]", isbn):
            results.append(isbn)
        elif len(isbn) == 13 and re.fullmatch(r"\d{13}", isbn):
            results.append(isbn)

    # dedupliser, behold rekkefølge
    seen = set()
    unique = []
    for x in results:
        if x not in seen:
            seen.add(x)
            unique.append(x)

    return unique


# =========================
# NB API
# =========================

session = requests.Session()


def nb_get_items(params: Dict[str, str]) -> Dict[str, Any]:
    time.sleep(REQUEST_SLEEP_SECONDS)
    response = session.get(NB_ITEMS_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def search_nb_by_isbn(isbn: str, isbn_cache: Dict[str, Any]) -> Dict[str, Any]:
    if isbn in isbn_cache:
        return isbn_cache[isbn]

    params = {
        "q": f"isbn:{isbn}",
        "filter": "mediatype:bøker",
        "size": "1",
    }

    try:
        data = nb_get_items(params)
        isbn_cache[isbn] = data
        return data
    except Exception as e:
        isbn_cache[isbn] = {"error": str(e)}
        return isbn_cache[isbn]


def search_nb_by_query(query: str, query_cache: Dict[str, Any], size: int = 5) -> Dict[str, Any]:
    cache_key = f"{query}||size={size}"
    if cache_key in query_cache:
        return query_cache[cache_key]

    params = {
        "q": query,
        "size": str(size),
    }

    try:
        data = nb_get_items(params)
        query_cache[cache_key] = data
        return data
    except Exception as e:
        query_cache[cache_key] = {"error": str(e)}
        return query_cache[cache_key]


def get_total_elements(nb_response: Dict[str, Any]) -> int:
    return int(nb_response.get("page", {}).get("totalElements", 0))


def get_first_item(nb_response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    items = nb_response.get("_embedded", {}).get("items", [])
    return items[0] if items else None


def extract_nb_item_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    metadata = item.get("metadata", {})
    origin = metadata.get("originInfo", {}) or {}
    creators = metadata.get("creators", []) or []

    return {
        "nb_id": item.get("id", ""),
        "nb_title": metadata.get("title", ""),
        "nb_author": " ; ".join(creators) if creators else "",
        "nb_year": origin.get("issued", ""),
        "nb_publisher": origin.get("publisher", ""),
    }


# =========================
# SØKESTRENG FOR ISBN-LØSE
# =========================

def choose_best_author(row: Dict[str, str]) -> str:
    """
    Brukes i kandidatmatchfasen, der radene har standardiserte små nøkkelnavn.
    """
    author = (row.get("author", "") or "").strip()
    contributor = (row.get("contributor", "") or "").strip()

    if author:
        return author
    if contributor:
        return contributor
    return ""


def build_candidate_queries(row: Dict[str, str]) -> List[str]:
    """
    Bygger søkestrenger for kandidatmatch.
    Forventer standardiserte feltnavn fra no_isbn_rows:
    - title
    - publication_date
    - author
    - contributor
    - publisher
    """
    raw_title = row.get("title", "") or ""
    main_title = normalize_whitespace(raw_title.split(":", 1)[0])
    full_title = normalize_whitespace(raw_title)

    year = extract_year(row.get("publication_date", ""))
    author = choose_best_author(row)
    publisher = normalize_whitespace(row.get("publisher", ""))

    queries = []

    # 1. Start bredt: hovedtittel alene
    if main_title:
        queries.append(f"\"{main_title}\"")
        queries.append(main_title)

    # 2. Hovedtittel + author
    if main_title and author:
        queries.append(f"\"{main_title}\" {author}")
        queries.append(f"{main_title} {author}")

    # 3. Hovedtittel + year
    if main_title and year:
        queries.append(f"\"{main_title}\" {year}")
        queries.append(f"{main_title} {year}")

    # 4. Hovedtittel + publisher
    if main_title and publisher:
        queries.append(f"\"{main_title}\" {publisher}")
        queries.append(f"{main_title} {publisher}")

    # 5. Full tittel som fallback
    if full_title and full_title != main_title:
        queries.append(f"\"{full_title}\"")
        queries.append(full_title)

        if author:
            queries.append(f"\"{full_title}\" {author}")
            queries.append(f"{full_title} {author}")

        if year:
            queries.append(f"\"{full_title}\" {year}")
            queries.append(f"{full_title} {year}")

        if publisher:
            queries.append(f"\"{full_title}\" {publisher}")
            queries.append(f"{full_title} {publisher}")

    # dedupliser
    seen = set()
    unique = []
    for q in queries:
        q = normalize_whitespace(q)
        if q and q not in seen:
            seen.add(q)
            unique.append(q)

    return unique


# =========================
# SCORING
# =========================

def word_set(text: str) -> set[str]:
    return set(normalize_text(text).split()) if text else set()


def overlap_ratio(a: str, b: str) -> float:
    a_set = word_set(a)
    b_set = word_set(b)
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / max(len(a_set), len(b_set))


def score_title(alma_title: str, nb_title: str) -> Tuple[int, str]:
    alma_full = normalize_title_full(alma_title)
    alma_main = extract_main_title(alma_title)

    nb_full = normalize_title_full(nb_title)
    nb_main = extract_main_title(nb_title)

    if alma_main and alma_main == nb_main:
        return 60, "exact_main_title"
    if alma_full and alma_full == nb_full:
        return 60, "exact_full_title"

    full_overlap = overlap_ratio(alma_full, nb_full)
    main_overlap = overlap_ratio(alma_main, nb_main)

    best_overlap = max(full_overlap, main_overlap)

    if best_overlap >= 0.8:
        return 40, "high_title_overlap"
    if best_overlap >= 0.5:
        return 20, "partial_title_overlap"

    return 0, "no_title_match"


def score_author(alma_author: str, alma_contributor: str, nb_author: str) -> Tuple[int, str]:
    nb_author_norm = normalize_author(nb_author)
    nb_author_key = author_key(nb_author)

    alma_author_norm = normalize_author(alma_author)
    alma_author_key = author_key(alma_author)

    if alma_author_key and nb_author_key and alma_author_key == nb_author_key:
        return 25, "author_key_match"

    if alma_author_norm and nb_author_norm and (
        alma_author_norm in nb_author_norm or nb_author_norm in alma_author_norm
    ):
        return 10, "weak_author_match"

    # Test hver contributor separat, og bruk beste score
    best_points = 0
    best_reason = "no_contributor_match"

    for contributor in split_contributors(alma_contributor):
        contrib_norm = normalize_author(contributor)
        contrib_key = author_key(contributor)

        if contrib_key and nb_author_key and contrib_key == nb_author_key:
            if 10 > best_points:
                best_points = 10
                best_reason = "contributor_key_match"

        elif contrib_norm and nb_author_norm and (
            contrib_norm in nb_author_norm or nb_author_norm in contrib_norm
        ):
            if 5 > best_points:
                best_points = 5
                best_reason = "weak_contributor_match"

    if best_points > 0:
        return best_points, best_reason

    return 0, "no_author_match"


def score_year(alma_year: str, nb_year: str) -> Tuple[int, str]:
    a = extract_year(alma_year)
    b = extract_year(nb_year)

    if not a or not b:
        return 0, "no_year_match"

    if a == b:
        return 15, "exact_year_match"

    try:
        if abs(int(a) - int(b)) == 1:
            return 5, "near_year_match"
    except ValueError:
        pass

    return 0, "no_year_match"


def score_publisher(alma_publisher: str, nb_publisher: str) -> Tuple[int, str]:
    a = normalize_publisher(alma_publisher)
    b = normalize_publisher(nb_publisher)

    if not a or not b:
        return 0, "no_publisher_match"

    if a == b:
        return 10, "exact_publisher_match"

    if a in b or b in a:
        return 5, "weak_publisher_match"

    return 0, "no_publisher_match"


def score_candidate(row: Dict[str, str], nb_item: Dict[str, Any]) -> Dict[str, Any]:
    nb_meta = extract_nb_item_metadata(nb_item)

    title_points, title_reason = score_title(row.get("title", ""), nb_meta["nb_title"])
    author_points, author_reason = score_author(
        row.get("author", ""),
        row.get("contributor", ""),
        nb_meta["nb_author"],
    )
    year_points, year_reason = score_year(row.get("publication_date", ""), nb_meta["nb_year"])
    publisher_points, publisher_reason = score_publisher(
        row.get("publisher", ""),
        nb_meta["nb_publisher"],
    )

    total = title_points + author_points + year_points + publisher_points

    return {
        "score_total": total,
        "score_title": title_points,
        "score_author": author_points,
        "score_year": year_points,
        "score_publisher": publisher_points,
        "reasons": [title_reason, author_reason, year_reason, publisher_reason],
        **nb_meta,
    }


def classify_candidate_scores(scored_candidates: List[Dict[str, Any]]) -> Tuple[str, str, int, int]:
    if not scored_candidates:
        return "not_found_candidate", "no_candidates", 0, 0

    scored_candidates = sorted(scored_candidates, key=lambda x: x["score_total"], reverse=True)

    best = scored_candidates[0]
    second_score = scored_candidates[1]["score_total"] if len(scored_candidates) > 1 else 0
    margin = best["score_total"] - second_score

    # Hvis treffet er svært sterkt, godtar vi det uavhengig av margin (håndterer duplikater i NB)
    if best["score_total"] >= 105:
        return "confirmed_in_nb", f"high_score={best['score_total']}", best["score_total"], second_score

    if best["score_total"] >= CONFIRMED_MIN_SCORE and margin >= CONFIRMED_MIN_MARGIN:
        return "confirmed_in_nb", f"score={best['score_total']}, margin={margin}", best["score_total"], second_score

    if best["score_total"] >= MANUAL_MIN_SCORE:
        return "needs_manual", f"uncertain_margin={margin}", best["score_total"], second_score

    return "not_found_candidate", "low_score", best["score_total"], second_score

def is_likely_norwegian(row: Dict[str, str]) -> bool:
    isbn_raw = row.get(COL_ISBN, "") or ""
    isbn_list = extract_isbn_candidates(isbn_raw)
    
    # Hvis boka mangler ISBN, beholder vi den for sikkerhets skyld
    if not isbn_list:
        return True
        
    # Hvis den HAR ISBN, sjekker vi om det er norsk eller om landkoden er 'no'
    country_code = (row.get(COL_COUNTRY_CODE, "") or "").strip().lower()
    has_norwegian_isbn = any(isbn.startswith("82") or isbn.startswith("97882") for isbn in isbn_list)
    
    return country_code == "no" or has_norwegian_isbn

# =========================
# FASE 1: ISBN
# =========================


def process_isbn_rows(rows: List[Dict[str, str]], isbn_cache: Dict[str, Any]) -> Tuple[
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
]:
    found_rows = []
    not_found_rows = []
    no_isbn_rows = []

    for idx, row in enumerate(rows, start=1):
        if idx % 100 == 0 or idx == 1:
            print(f"[ISBN-fase] Behandler rad {idx} av {len(rows)}")
        alma_id = get_row_id(row, idx)
        isbn_raw = row.get(COL_ISBN, "") or ""
        isbn_candidates = extract_isbn_candidates(isbn_raw)

        if not isbn_candidates:
            no_isbn_rows.append({
                "alma_id": alma_id,
                "title": row.get(COL_TITLE, ""),
                "publication_date": row.get(COL_YEAR, ""),
                "author": row.get(COL_AUTHOR, ""),
                "contributor": row.get(COL_CONTRIBUTOR, ""),
                "publisher": row.get(COL_PUBLISHER, ""),
            })
            continue

        matched = False
        tested = []

        for isbn in isbn_candidates:
            tested.append(isbn)
            response = search_nb_by_isbn(isbn, isbn_cache)

            if "error" in response:
                append_error_row(row, f"ISBN search failed for {isbn}: {response['error']}")
                continue

            total = get_total_elements(response)
            if total > 0:
                item = get_first_item(response)
                meta = extract_nb_item_metadata(item) if item else {}

                found_rows.append({
                    "alma_id": alma_id,
                    "title": row.get(COL_TITLE, ""),
                    "isbn_raw": isbn_raw,
                    "isbn_used": isbn,
                    "nb_found": True,
                    "nb_total_elements": total,
                    "match_method": "isbn",
                    **meta,
                })
                matched = True
                break

        if not matched:
            # send videre til metadata-match
            no_isbn_rows.append({
                "alma_id": alma_id,
                "title": row.get(COL_TITLE, ""),
                "publication_date": row.get(COL_YEAR, ""),
                "author": row.get(COL_AUTHOR, ""),
                "contributor": row.get(COL_CONTRIBUTOR, ""),
                "publisher": row.get(COL_PUBLISHER, ""),
            })

            not_found_rows.append({
                "alma_id": alma_id,
                "title": row.get(COL_TITLE, ""),
                "isbn_raw": isbn_raw,
                "isbn_candidates_tested": " ; ".join(tested),
                "nb_found": False,
                "match_method": "isbn",
            })

    return found_rows, not_found_rows, no_isbn_rows


# =========================
# FASE 2: KANDIDATMATCH
# =========================

def process_candidate_rows(rows: List[Dict[str, Any]], query_cache: Dict[str, Any]) -> Tuple[
    List[Dict[str, Any]],
    List[Dict[str, Any]],
]:
    all_results = []
    needs_manual = []

    for idx, row in enumerate(rows, start=1):
        if idx % 50 == 0 or idx == 1:
            print(f"[Metadata-fase] Behandler rad {idx} av {len(rows)}")
        try:
            queries = build_candidate_queries(row)
            scored_candidates = []

            # Prøv ALLE queries og samle ALLE kandidater
            for query in queries:
                response = search_nb_by_query(query, query_cache, size=5)

                if "error" in response:
                    append_error_row(row, f"Query search failed for '{query}': {response['error']}")
                    continue

                items = response.get("_embedded", {}).get("items", [])
                for item in items:
                    scored = score_candidate(row, item)
                    scored["query_used_for_candidate"] = query
                    scored_candidates.append(scored)

            # Dedupliser kandidater på nb_id, behold beste versjon av hver kandidat
            deduped_candidates = {}

            for cand in scored_candidates:
                nb_id = cand.get("nb_id", "")
                if not nb_id:
                    continue

                existing = deduped_candidates.get(nb_id)

                if existing is None:
                    deduped_candidates[nb_id] = cand
                else:
                    # Behold kandidaten med høyest score.
                    # Hvis score er lik, behold den første.
                    if cand["score_total"] > existing["score_total"]:
                        deduped_candidates[nb_id] = cand

            scored_candidates = sorted(
                deduped_candidates.values(),
                key=lambda x: x["score_total"],
                reverse=True
            )

            status, reason, best_score, second_score = classify_candidate_scores(scored_candidates)

            best = scored_candidates[0] if scored_candidates else {}
            chosen_query = best.get("query_used_for_candidate", "") if best else ""

            result_row = {
                "alma_id": row.get("alma_id", f"row_{idx}"),
                "title": row.get("title", ""),
                "publication_date": row.get("publication_date", ""),
                "author": row.get("author", ""),
                "contributor": row.get("contributor", ""),
                "publisher": row.get("publisher", ""),
                "nb_status": status,
                "query_used": chosen_query,
                "nb_best_id": best.get("nb_id", ""),
                "nb_best_title": best.get("nb_title", ""),
                "nb_best_author": best.get("nb_author", ""),
                "nb_best_year": best.get("nb_year", ""),
                "nb_best_publisher": best.get("nb_publisher", ""),
                "score_best": best_score,
                "score_second": second_score,
                "match_reason": reason,
                "score_breakdown": " | ".join(best.get("reasons", [])),
            }

            all_results.append(result_row)

            if status == "needs_manual":
                needs_manual.append(result_row)

        except Exception as e:
            append_error_row(row, f"Candidate processing failed: {str(e)}")

    return all_results, needs_manual


# =========================
# MAIN
# =========================

def main() -> None:
    ensure_output_dir()

    # 1. Last inn cache-filer først
    isbn_cache = load_json_file(ISBN_CACHE_FILE)
    query_cache = load_json_file(QUERY_CACHE_FILE)

    # 2. Les inn alle rådata fra CSV
    raw_rows = read_csv_rows(INPUT_CSV)
    total_input = len(raw_rows)
    
    # 3. Filtrer dataene og behold resultatet i 'rows'
    rows = [r for r in raw_rows if is_likely_norwegian(r)]

    excluded_rows = [r for r in raw_rows if not is_likely_norwegian(r)]

    total_rows = len(rows)
    
    filtered_count = total_input - total_rows
    print(f"Filtrering fullført: {total_rows} poster beholdt, {filtered_count} poster fjernet.")

    # 4. Prosesser den filtrerte 'rows'-listen (IKKE les inn filen på nytt her)
    found_isbn, not_found_isbn, no_isbn_rows = process_isbn_rows(rows, isbn_cache)

    candidate_results, needs_manual = process_candidate_rows(no_isbn_rows, query_cache)

    # 5. Lagre oppdatert cache og resultater
    save_json_file(ISBN_CACHE_FILE, isbn_cache)
    save_json_file(QUERY_CACHE_FILE, query_cache)
    

    write_csv(
        OUTPUT_DIR / "poster_funnet_i_nb_via_isbn.csv",
        found_isbn,
        [
            "alma_id",
            "title",
            "isbn_raw",
            "isbn_used",
            "nb_found",
            "nb_total_elements",
            "nb_id",
            "nb_title",
            "nb_author",
            "nb_year",
            "nb_publisher",
            "match_method",
        ],
    )

    write_csv(
        OUTPUT_DIR / "poster_med_isbn_ikke_funnet_via_isbn.csv",
        not_found_isbn,
        [
            "alma_id",
            "title",
            "isbn_raw",
            "isbn_candidates_tested",
            "nb_found",
            "match_method",
        ],
    )

    write_csv(
        OUTPUT_DIR / "poster_sendt_til_metadata_match.csv",
        no_isbn_rows,
        [
            "alma_id",
            "title",
            "publication_date",
            "author",
            "contributor",
            "publisher",
        ],
    )

    write_csv(
        OUTPUT_DIR / "resultat_metadata_match.csv",
        candidate_results,
        [
            "alma_id",
            "title",
            "publication_date",
            "author",
            "contributor",
            "publisher",
            "nb_status",
            "query_used",
            "nb_best_id",
            "nb_best_title",
            "nb_best_author",
            "nb_best_year",
            "nb_best_publisher",
            "score_best",
            "score_second",
            "match_reason",
            "score_breakdown",
        ],
    )

    write_csv(
        OUTPUT_DIR / "poster_til_manuell_kontroll.csv",
        needs_manual,
        [
            "alma_id",
            "title",
            "publication_date",
            "author",
            "contributor",
            "publisher",
            "nb_status",
            "query_used",
            "nb_best_id",
            "nb_best_title",
            "nb_best_author",
            "nb_best_year",
            "nb_best_publisher",
            "score_best",
            "score_second",
            "match_reason",
            "score_breakdown",
        ],
    )

    write_csv(
        OUTPUT_DIR / "poster_filtrert_bort_før_match.csv",
        excluded_rows,
        list(raw_rows[0].keys()) if raw_rows else []
    )

    actual_no_isbn = total_rows - (len(found_isbn) + len(not_found_isbn))
    candidate_input_count = len(no_isbn_rows)

    print("Ferdig.")
    print(f"Totalt antall poster i input-fil: {total_input}")
    print(f"Poster med ISBN: {len(found_isbn) + len(not_found_isbn)}")
    print(f"ISBN funnet i NB: {len(found_isbn)}")
    print(f"ISBN ikke funnet i NB: {len(not_found_isbn)}")
    print(f"Poster uten ISBN: {actual_no_isbn}")
    print(f"Poster sendt videre til kandidatmatch: {candidate_input_count}")
    print(f"Kandidatmatch-resultater: {len(candidate_results)}")
    print(f"Manuell kontroll: {len(needs_manual)}")




if __name__ == "__main__":
    main()