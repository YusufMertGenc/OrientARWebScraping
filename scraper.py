import os
import re
import json
import base64
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

import firebase_admin
from firebase_admin import credentials, firestore


THIS_WEEK_URL = "https://ncc.metu.edu.tr/this-week-on-campus"
SOCIETIES_URL = "https://ncc.metu.edu.tr/socialandculturalaffairs/societies-communication-details"

COL_EVENTS = "campus_events_weeks"
COL_SOCIETIES = "student_societies"
COL_META = "scrape_meta"
DOC_META = "current"


# ---------------------------
# Utilities
# ---------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_obj(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def slugify(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\s-]", "", t, flags=re.UNICODE)
    t = re.sub(r"[\s_-]+", "-", t, flags=re.UNICODE)
    return t.strip("-")


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def request_html(url: str, timeout: int = 30) -> str:
    headers = {
        "User-Agent": "OrientAR-Scraper/1.0 (+METU NCC capstone; contact: repo owner)",
        "Accept": "text/html,application/xhtml+xml",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def init_firestore_from_b64() -> firestore.Client:
    """
    Expects env:
      FIREBASE_SA_B64: base64(serviceAccountJSON)
    """
    sa_b64 = os.getenv("FIREBASE_SA_B64")
    if not sa_b64:
        raise RuntimeError("Missing env FIREBASE_SA_B64")

    sa_json = base64.b64decode(sa_b64).decode("utf-8")
    cred = credentials.Certificate(json.loads(sa_json))

    # safe init
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    return firestore.client()


def _extract_lines(block: BeautifulSoup) -> List[str]:
    """
    Get meaningful lines inside a block, preserving line breaks.
    """
    raw_lines = block.get_text("\n", strip=True).split("\n")
    lines = [clean_text(x) for x in raw_lines]
    return [x for x in lines if x]


def _looks_like_event_block(lines: List[str]) -> bool:
    """
    Match the actual page style you showed:
      - starts with dd.mm.yyyy (maybe with day name)
      - or dd.mm.yyyy – dd.mm.yyyy (range)
      - often next line contains @HH:MM
    """
    if not lines:
        return False
    first = lines[0]
    return bool(
        re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", first)
        or re.search(r"\b\d{2}\.\d{2}\.\d{4}\s*[–-]\s*\d{2}\.\d{2}\.\d{4}\b", first)
    )


def _parse_time_line(s: str) -> str:
    # "@20:00" -> "20:00"
    s = clean_text(s)
    s = s.replace("@", "").strip()
    return s


def _try_parse_iso(date_line: str, time_line: str) -> Optional[str]:
    """
    Try convert:
      "26.02.2026, Perşembe / Thursday" + "@20:00" -> iso
      "27.02.2026 – 01.03.2026" -> (no single datetime) None
    """
    # If it's a date range, don't force a single datetime
    if re.search(r"\b\d{2}\.\d{2}\.\d{4}\s*[–-]\s*\d{2}\.\d{2}\.\d{4}\b", date_line):
        return None

    # Extract first dd.mm.yyyy
    m = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", date_line)
    if not m:
        return None
    date_part = m.group(1)

    time_part = _parse_time_line(time_line) if time_line else ""
    if not time_part:
        return None

    candidate = f"{date_part} {time_part}"
    try:
        dt = date_parser.parse(candidate, dayfirst=True, fuzzy=True)
        return dt.isoformat()
    except Exception:
        return None


# ---------------------------
# Parsing: This Week on Campus (FIXED for the real layout)
# ---------------------------
def parse_this_week(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main") or soup.find("div", {"role": "main"}) or soup.body

    page_title = clean_text(soup.title.get_text()) if soup.title else "This Week on Campus"

    week_range_text = ""
    if main:
        h1 = main.find("h1")
        if h1:
            week_range_text = clean_text(h1.get_text())

    if not main:
        return {
            "source_url": THIS_WEEK_URL,
            "title": page_title,
            "week_range_text": week_range_text,
            "events": [],
        }

    date_line_re = re.compile(r"^\s*\d{2}\.\d{2}\.\d{4}\b")
    date_range_re = re.compile(r"^\s*\d{2}\.\d{2}\.\d{4}\s*[–-]\s*\d{2}\.\d{2}\.\d{4}\b")
    time_line_re = re.compile(r"^\s*@\s*\d{1,2}:\d{2}(?:\s*[-–]\s*\d{1,2}:\d{2})?\s*$")

    junk_exact = {
        "this week on campus",
        "event calendar",
        "live chat",
        "student societies",
        "general rules",
        "framework directive",
        "forms",
        "contact",
    }

    def is_date_line(line: str) -> bool:
        return bool(date_range_re.match(line) or date_line_re.match(line))

    def is_time_line(line: str) -> bool:
        return bool(time_line_re.match(line))

    def is_junk_line(line: str) -> bool:
        l = clean_text(line).lower()
        if re.fullmatch(r"\d{1,3}", l):
            return True
        if l in junk_exact:
            return True
        if len(l) <= 1:
            return True
        return False

    def normalize_iso(date_line: str, time_line: str) -> Optional[str]:
        if date_range_re.match(date_line):
            return None

        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", date_line)
        if not m or not time_line:
            return None

        date_part = m.group(1)
        t = clean_text(time_line).replace("@", "").strip()
        t = re.split(r"\s*[-–]\s*", t)[0]   # start time only

        try:
            dt = date_parser.parse(f"{date_part} {t}", dayfirst=True, fuzzy=True)
            return dt.isoformat()
        except Exception:
            return None

    raw_lines = main.get_text("\n", strip=True).split("\n")
    lines = [clean_text(x) for x in raw_lines]
    lines = [x for x in lines if x and not is_junk_line(x)]

    events: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    def finalize_current():
        nonlocal current
        if not current:
            return
        if current.get("title"):
            current["description"] = " | ".join(current.get("_desc_lines", []))
            current.pop("_desc_lines", None)
            events.append(current)
        current = None

    i = 0
    while i < len(lines):
        line = lines[i]

        if is_date_line(line):
            finalize_current()

            date_text = line
            time_text = ""
            title = ""

            j = i + 1
            if j < len(lines) and is_time_line(lines[j]):
                time_text = lines[j]
                j += 1

            if j < len(lines) and not is_date_line(lines[j]):
                title = lines[j]
                j += 1

            current = {
                "date_text": date_text,
                "time_text": time_text,
                "date_time_iso": normalize_iso(date_text, time_text),
                "title": title,
                "title_tr": title,
                "title_en": "",
                "location": "",
                "_desc_lines": [],
                "raw_lines": [],
            }

            i = j
            continue

        if current:
            current["_desc_lines"].append(line)
            current["raw_lines"].append(line)

            if not current["location"] and re.search(
                r"\b(Culture and Convention Center|Amfi|Hall|Library|Cafeteria|Academic Buildings|Seminer|Room)\b",
                line,
                re.I,
            ):
                current["location"] = line

        i += 1

    finalize_current()

    seen = set()
    uniq = []
    for e in events:
        k = (
            (e.get("date_text") or "").strip().lower(),
            (e.get("time_text") or "").strip().lower(),
            (e.get("title") or "").strip().lower(),
        )
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)

    def is_date_line(line: str) -> bool:
        return bool(date_range_re.match(line) or date_line_re.match(line))

    def is_time_line(line: str) -> bool:
        return bool(time_line_re.match(line))

    def is_junk_line(line: str) -> bool:
        l = clean_text(line).lower()

        # Single small numbers like "2" (pagination etc.)
        if re.fullmatch(r"\d{1,3}", l):
            return True

        # menu-ish items
        if l in junk_exact:
            return True

        # lines that are just separators or too short
        if len(l) <= 1:
            return True

        return False

    def normalize_iso(date_line: str, time_line: str) -> Optional[str]:
        # For ranges, don't make single datetime
        if date_range_re.match(date_line):
            return None
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", date_line)
        if not m:
            return None
        if not time_line:
            return None
        date_part = m.group(1)
        time_part = clean_text(time_line).replace("@", "").strip()
        try:
            dt = date_parser.parse(f"{date_part} {time_part}", dayfirst=True, fuzzy=True)
            return dt.isoformat()
        except Exception:
            return None

    # ---- Build a clean line stream from content ----
    # Using "\n" keeps structure closer to the boxes.
    raw_lines = main.get_text("\n", strip=True).split("\n")
    lines = [clean_text(x) for x in raw_lines]
    lines = [x for x in lines if x and not is_junk_line(x)]

    events: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    def finalize_current():
        nonlocal current
        if not current:
            return
        # Need at least a title
        if current.get("title"):
            # fill combined description
            current["description"] = " | ".join(current.get("_desc_lines", []))
            current.pop("_desc_lines", None)
            events.append(current)
        current = None

    i = 0
    while i < len(lines):
        line = lines[i]

        if is_date_line(line):
            # New event begins
            finalize_current()

            date_text = line
            time_text = ""
            title_tr = ""
            title_en = ""

            # Lookahead: time line may be the next line
            j = i + 1
            if j < len(lines) and is_time_line(lines[j]):
                time_text = lines[j]
                j += 1

            # Next line = title (must exist)
            if j < len(lines) and not is_date_line(lines[j]):
                title_tr = lines[j]
                j += 1

            title_en = ""
            current = {
                "date_text": date_text,
                "time_text": time_text,
                "date_time_iso": normalize_iso(date_text, time_text),
                "title_tr": title_tr,
                "title_en": title_en,
                "title": title_tr or title_en,
                "location": "",
                "_desc_lines": [],
                "raw_lines": [],
            }

            # Advance i to continue collecting description from j
            i = j
            continue

        # Collect description lines for current event
        if current:
            current["_desc_lines"].append(line)
            current["raw_lines"].append(line)

            # try detect location line
            if not current["location"] and re.search(
                r"\b(Culture and Convention Center|Amfi|Hall|Rauf Raif Denktaş|Library|Cafeteria)\b",
                line,
                re.I,
            ):
                current["location"] = line

        i += 1

    finalize_current()

    # Dedupe by (date_text, time_text, title)
    seen = set()
    uniq = []
    for e in events:
        k = (
            (e.get("date_text") or "").strip().lower(),
            (e.get("time_text") or "").strip().lower(),
            (e.get("title") or "").strip().lower(),
    )
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)

    return {
        "source_url": THIS_WEEK_URL,
        "title": page_title,
        "week_range_text": week_range_text,
        "events": uniq,
    }

# ---------------------------
# Parsing: Societies
# ---------------------------
def parse_societies(html: str) -> Dict[str, Any]:
    """
    Returns:
      {
        "source_url": ...,
        "societies": [
          {"name","slug","details": {...}, "raw_text"}
        ]
      }

    Society page often includes a table or repeated blocks.
    We'll parse tables first; then fallback to blocks/lists.
    """
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main") or soup.find("div", {"role": "main"}) or soup.body

    societies: List[Dict[str, Any]] = []

    # Strategy A: parse tables (most common for "communication details")
    tables = main.find_all("table") if main else []
    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue

        # Extract header cells
        headers = []
        header_row = rows[0].find_all(["th", "td"])
        for cell in header_row:
            headers.append(clean_text(cell.get_text(" ", strip=True)).lower())

        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue

            values = [clean_text(c.get_text(" ", strip=True)) for c in cells]
            row_map: Dict[str, str] = {}
            for i, v in enumerate(values):
                k = headers[i] if i < len(headers) and headers[i] else f"col_{i+1}"
                row_map[k] = v

            name = next((v for v in values if v), "")
            if not name:
                continue

            slug = slugify(name)
            raw_text = " | ".join([f"{k}: {v}" for k, v in row_map.items() if v])

            societies.append({
                "name": name,
                "slug": slug,
                "details": row_map,
                "raw_text": clean_text(raw_text),
            })

    # Strategy B: if no table societies, try headings blocks
    if not societies and main:
        for h in main.find_all(["h2", "h3", "h4"]):
            name = clean_text(h.get_text())
            if not name or len(name) < 3:
                continue

            parts = []
            sib = h.find_next_sibling()
            steps = 0
            while sib and steps < 10:
                if sib.name in ["h2", "h3", "h4"]:
                    break
                txt = clean_text(sib.get_text(" ", strip=True))
                if txt:
                    parts.append(txt)
                sib = sib.find_next_sibling()
                steps += 1

            raw = clean_text(" | ".join(parts))
            if not raw:
                continue

            slug = slugify(name)
            societies.append({
                "name": name,
                "slug": slug,
                "details": {"info": raw},
                "raw_text": raw,
            })

    # Dedupe by slug
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for s in societies:
        if s["slug"] in seen:
            continue
        seen.add(s["slug"])
        uniq.append(s)

    return {
        "source_url": SOCIETIES_URL,
        "societies": uniq,
    }


# ---------------------------
# Firestore write logic
# ---------------------------
def get_meta(db: firestore.Client) -> Dict[str, Any]:
    ref = db.collection(COL_META).document(DOC_META)
    doc = ref.get()
    return doc.to_dict() if doc.exists else {}


def set_meta(db: firestore.Client, meta: Dict[str, Any]) -> None:
    ref = db.collection(COL_META).document(DOC_META)
    ref.set(meta, merge=True)


def upsert_week_events(db: firestore.Client, payload: Dict[str, Any]) -> Tuple[str, bool]:
    """
    Document id: derived from week_range_text if possible, else "current"
    Returns (doc_id, wrote?)
    """
    week_key = payload.get("week_range_text") or "current"
    doc_id = slugify(week_key)[:120] or "current"

    data = {
        "source_url": payload["source_url"],
        "page_title": payload.get("title", ""),
        "week_range_text": payload.get("week_range_text", ""),
        "events": payload.get("events", []),
        "updated_at": utc_now_iso(),
    }

    meta = get_meta(db)
    new_hash = sha256_obj(data)
    old_hash = meta.get("this_week_hash")

    if old_hash == new_hash:
        return doc_id, False

    db.collection(COL_EVENTS).document(doc_id).set(data, merge=True)
    set_meta(db, {
        "this_week_hash": new_hash,
        "this_week_doc_id": doc_id,
        "this_week_last_success": utc_now_iso(),
    })
    return doc_id, True


def upsert_societies(db: firestore.Client, payload: Dict[str, Any]) -> bool:
    """
    Writes each society as doc: student_societies/{slug}
    Also stores a whole-list hash in meta to skip rewrites.
    """
    societies = payload.get("societies", [])
    list_hash = sha256_obj(societies)

    meta = get_meta(db)
    old_hash = meta.get("societies_hash")
    if old_hash == list_hash:
        return False

    batch = db.batch()
    for s in societies:
        doc_id = s["slug"][:200] or slugify(s["name"])[:200] or "society"
        ref = db.collection(COL_SOCIETIES).document(doc_id)
        batch.set(ref, {
            "name": s["name"],
            "slug": s["slug"],
            "details": s.get("details", {}),
            "raw_text": s.get("raw_text", ""),
            "source_url": payload["source_url"],
            "updated_at": utc_now_iso(),
        }, merge=True)

    batch.commit()

    set_meta(db, {
        "societies_hash": list_hash,
        "societies_last_success": utc_now_iso(),
        "societies_count": len(societies),
    })
    return True


def main() -> None:
    db = init_firestore_from_b64()

    status: Dict[str, Any] = {
        "last_run_at": utc_now_iso(),
        "ok": True,
        "errors": [],
        "wrote": {},
    }

    try:
        this_week_html = request_html(THIS_WEEK_URL)
        this_week_payload = parse_this_week(this_week_html)
        doc_id, wrote = upsert_week_events(db, this_week_payload)
        status["wrote"]["this_week"] = {
            "doc_id": doc_id,
            "updated": wrote,
            "events_count": len(this_week_payload.get("events", []))
        }
    except Exception as e:
        status["ok"] = False
        status["errors"].append(f"this_week_error: {repr(e)}")

    try:
        societies_html = request_html(SOCIETIES_URL)
        societies_payload = parse_societies(societies_html)
        wrote = upsert_societies(db, societies_payload)
        status["wrote"]["societies"] = {
            "updated": wrote,
            "count": len(societies_payload.get("societies", []))
        }
    except Exception as e:
        status["ok"] = False
        status["errors"].append(f"societies_error: {repr(e)}")

    set_meta(db, {
        "last_run_at": status["last_run_at"],
        "last_ok": status["ok"],
        "last_errors": status["errors"],
    })

    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()