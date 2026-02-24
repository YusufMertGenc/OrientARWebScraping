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
    t = text.strip().lower()
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


# ---------------------------
# Parsing: This Week on Campus
# ---------------------------
def parse_this_week(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")

    main = soup.find("main") or soup.body

    # 1️⃣ Week Header (en üstte büyük başlık)
    week_header = ""
    h1 = main.find("h1")
    if h1:
        week_header = clean_text(h1.get_text())

    events = []

    # 2️⃣ Event blocks → genelde border'lı div'ler
    # Güvenli yaklaşım: tarih pattern'i içeren strong/bold veya text bul
    blocks = main.find_all(["div", "section"])

    date_pattern = re.compile(r"\d{2}\.\d{2}\.\d{4}")

    for block in blocks:
        text = clean_text(block.get_text(" ", strip=True))

        if not date_pattern.search(text):
            continue

        lines = [clean_text(x) for x in block.get_text("\n").split("\n") if clean_text(x)]

        if len(lines) < 2:
            continue

        # İlk satır genelde tarih
        date_line = lines[0]

        # Saat varsa genelde ikinci satırda "@"
        time_line = ""
        title_line = ""
        description_lines = []

        idx = 1
        if idx < len(lines) and "@" in lines[idx]:
            time_line = lines[idx]
            idx += 1

        # Sonraki satır title kabul edelim
        if idx < len(lines):
            title_line = lines[idx]
            idx += 1

        # Kalanlar açıklama
        description_lines = lines[idx:]

        # ISO datetime üretmeye çalış
        dt_iso = None
        try:
            dt_candidate = date_line + " " + time_line.replace("@", "")
            dt = date_parser.parse(dt_candidate, fuzzy=True)
            dt_iso = dt.isoformat()
        except Exception:
            pass

        event = {
            "title": title_line,
            "date_text": date_line,
            "time_text": time_line,
            "date_time_iso": dt_iso,
            "description": " | ".join(description_lines),
            "raw_text": text,
        }

        events.append(event)

    return {
        "source_url": THIS_WEEK_URL,
        "week_range_text": week_header,
        "events": events,
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

        # If table looks empty or has no meaningful headers, still parse
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            values = [clean_text(c.get_text(" ", strip=True)) for c in cells]
            row_map: Dict[str, str] = {}
            for i, v in enumerate(values):
                k = headers[i] if i < len(headers) and headers[i] else f"col_{i+1}"
                row_map[k] = v

            # Name guess: first non-empty cell
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

    # Compare hash vs stored hash
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

    # Scrape & parse
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
        status["wrote"]["this_week"] = {"doc_id": doc_id, "updated": wrote, "events_count": len(this_week_payload.get("events", []))}
    except Exception as e:
        status["ok"] = False
        status["errors"].append(f"this_week_error: {repr(e)}")

    try:
        societies_html = request_html(SOCIETIES_URL)
        societies_payload = parse_societies(societies_html)
        wrote = upsert_societies(db, societies_payload)
        status["wrote"]["societies"] = {"updated": wrote, "count": len(societies_payload.get("societies", []))}
    except Exception as e:
        status["ok"] = False
        status["errors"].append(f"societies_error: {repr(e)}")

    # Save run status to meta
    set_meta(db, {
        "last_run_at": status["last_run_at"],
        "last_ok": status["ok"],
        "last_errors": status["errors"],
    })

    # Print for GitHub Actions logs
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()