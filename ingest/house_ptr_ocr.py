"""Read scanned House Periodic Transaction Report pages.

The paper form is a fixed table: owner, asset, purchase/sale/exchange
checkboxes, two dates, then ten amount checkboxes. Scans have no text
layer, dates are often MM/DD/YY, and a checked column is the amount.
Tesseract's full-page text glues asset words together and misses the
checkbox, so this module finds the ruling lines and reads each cell.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

logger = logging.getLogger("rollcalltrack_ingest.house_ptr_ocr")

# Column order on the House PTR (Rev. forms in the 2025-2026 scans).
AMOUNT_RANGES = (
    "$1,001 - $15,000",
    "$15,001 - $50,000",
    "$50,001 - $100,000",
    "$100,001 - $250,000",
    "$250,001 - $500,000",
    "$500,001 - $1,000,000",
    "$1,000,001 - $5,000,000",
    "$5,000,001 - $25,000,000",
    "$25,000,001 - $50,000,000",
    "Over $50,000,000",
)
# Three type boxes: purchase, sale, exchange.
# Some printings add a partial-sale box between sale and exchange.
_TYPES_3 = ("purchase", "sale", "exchange")
_TYPES_4 = ("purchase", "sale", "sale_partial", "exchange")

# Words worth a space inside a glued all-caps asset token. Short English
# words are omitted so proper nouns (BLACKSTONE) stay intact.
_ASSET_WORDS = {
    "AMERICAN",
    "ARKANSAS",
    "BOND",
    "BONDS",
    "CAPITAL",
    "CLASS",
    "COMMON",
    "COMMUNICATION",
    "COMMUNICATIONS",
    "COMPANY",
    "CONGLOMERATE",
    "CONSUMER",
    "CORE",
    "CORPORATION",
    "CREDIT",
    "DEFENSE",
    "DEVELOPED",
    "DEVICES",
    "DISTRICT",
    "DIVIDEND",
    "EMERGING",
    "ENERGY",
    "EQUITY",
    "ESTATE",
    "EUROPE",
    "FINANCIAL",
    "FIXED",
    "FUND",
    "FUNDS",
    "GLOBAL",
    "GOVERNMENT",
    "GROUP",
    "GROWTH",
    "HEALTH",
    "HOLDINGS",
    "HOME",
    "HOMEOWNER",
    "INCOME",
    "INDEX",
    "INDUSTRIAL",
    "INFLATION",
    "INFORMATION",
    "INTERNATIONAL",
    "LIMITED",
    "LONG",
    "MARKET",
    "MARKETS",
    "MATERIALS",
    "MICRO",
    "MID",
    "MORTGAGE",
    "MUNICIPAL",
    "NATIONAL",
    "NORTH",
    "OPPORTUNITIES",
    "OPPORTUNITY",
    "OWNER",
    "PARTNERS",
    "PARTNERSHIP",
    "PRIVATE",
    "PROTECTED",
    "REAL",
    "REVENUE",
    "SECTOR",
    "SELECT",
    "SERVICES",
    "SHARE",
    "SHARES",
    "SHORT",
    "SMALL",
    "STATE",
    "STOCK",
    "STRATEGIC",
    "STRATEGIES",
    "STRATEGY",
    "SYSTEM",
    "TECH",
    "TECHNOLOGIES",
    "TECHNOLOGY",
    "TERM",
    "TEXAS",
    "TOTAL",
    "TREASURY",
    "TRUST",
    "ULTRA",
    "UTILITIES",
    "VALUE",
    "VANGUARD",
    "WATER",
    "WORLD",
    "YORK",
    "NEW",
    "CAP",
}
# Matched even when short, including inside a glued token.
_ASSET_ABBREV = {
    "ADR",
    "CO",
    "CORP",
    "ETF",
    "FD",
    "FDS",
    "INC",
    "LLC",
    "LP",
    "LTD",
    "PLC",
    "REIT",
    "SHS",
    "TR",
}
_CLASS_LETTERS = {"A", "B", "C", "I", "II", "III"}
_ASSET_REJECT = (
    "PROVIDE FULL",
    "TICKER SYMBOL",
    "EXAMPLE MEGA",
    "NOTHING TO REPORT",
    "NOTHING TO",
    "FILER NOTE",
    "THIS PAGE WILL",
    "UNITED STATES HOUSE",
    "PERIODIC TRANSACTION",
    "FULL ASSET NAME",
    "AMOUNT OF TRANSACTION",
    "FOR OFFICIAL USE",
    "OFFICE TELEPHONE",
    "PUBLICLY DISCLOSED",
    "HOUSE OF REPRESENTATIVES",
    "MEMBER OF THE",
)


def parse_ptr_date(text: str | None) -> str | None:
    """Normalize a scanned date to MM/DD/YYYY.

    Accepts slashes, dashes, and digit runs such as 072826. Two-digit years
    00-69 map to 2000-2069. Returns None when the value is not a real day.
    """
    if not text:
        return None
    cleaned = (
        str(text)
        .upper()
        .replace("O", "0")
        .replace("I", "1")
        .replace("L", "1")
        .replace("|", "/")
        .replace("\\", "/")
    )
    match = re.search(
        r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})",
        cleaned,
    )
    if match:
        parsed = _valid_date(*match.groups())
        if parsed:
            return parsed
        # A ruling line beside the month OCRs as a leading 1, so 6/9/26
        # becomes 16/9/26. Drop that digit only when the month is impossible.
        month, day, year = match.groups()
        if (
            len(month) == 2
            and month.startswith("1")
            and month not in {"10", "11", "12"}
        ):
            parsed = _valid_date(month[1:], day, year)
            if parsed:
                return parsed
    digits = re.sub(r"\D", "", cleaned)
    candidates: list[tuple[str, str, str]] = []
    if len(digits) == 6:
        candidates.append((digits[0:2], digits[2:4], digits[4:6]))
    elif len(digits) == 8:
        candidates.append((digits[0:2], digits[2:4], digits[4:8]))
    elif len(digits) == 5:
        candidates.append((digits[0:1], digits[1:3], digits[3:5]))
        candidates.append((digits[0:2], digits[2:3], digits[3:5]))
    for parts in candidates:
        parsed = _valid_date(*parts)
        if parsed:
            return parsed
    return None


def _valid_date(month: str, day: str, year: str) -> str | None:
    try:
        month_n = int(month)
        day_n = int(day)
        year_s = str(year)
        if len(year_s) == 2:
            yy = int(year_s)
            year_n = 2000 + yy if yy < 70 else 1900 + yy
        elif len(year_s) == 4:
            year_n = int(year_s)
        else:
            return None
        if not (1995 <= year_n <= 2035):
            return None
        datetime(year_n, month_n, day_n)
    except (TypeError, ValueError):
        return None
    return f"{month_n:02d}/{day_n:02d}/{year_n}"


def restore_asset_spacing(text: str) -> str:
    """Put spaces back into an all-caps asset token Tesseract glued together.

    ``BLACKSTONEPRIVATEEQUITYSTRATEGIESFUNDLPCLASS`` becomes
    ``BLACKSTONE PRIVATE EQUITY STRATEGIES FUND LP CLASS``. A token that
    does not split into real words is left unchanged.
    """
    if not text:
        return ""

    def _fix(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.isupper():
            spaced = _segment_glued(token)
            return spaced or token
        camel = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", token)
        camel = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", camel)
        return camel

    return re.sub(r"[A-Za-z]{12,}", _fix, text)


def _segment_glued(token: str) -> str | None:
    words = sorted(_ASSET_WORDS | _ASSET_ABBREV, key=len, reverse=True)
    parts: list[str] = []
    i = 0
    n = len(token)
    while i < n:
        match = _longest_word(token, i, words)
        if match and _word_at_boundary(match, bool(parts)):
            parts.append(match)
            i += len(match)
            if match == "CLASS" and i < n and token[i:] in _CLASS_LETTERS:
                parts.append(token[i:])
                i = n
            continue
        j = i + 1
        while j < n:
            nxt = _longest_word(token, j, words)
            if nxt and (len(nxt) >= 5 or nxt in _ASSET_ABBREV or nxt == "CLASS"):
                break
            j += 1
        parts.append(token[i:j])
        i = j
    if len(parts) < 2:
        return None
    known = 0
    known_n = 0
    for part in parts:
        if part in _ASSET_WORDS or part in _ASSET_ABBREV or part in _CLASS_LETTERS:
            known += len(part)
            known_n += 1
    # A trailing fund suffix is enough when the rest is one proper noun.
    suffix = parts[-1] in _ASSET_ABBREV or parts[-1] in {"FUND", "TRUST", "CLASS"}
    if known_n < 2 and not (suffix and len(parts) == 2 and len(parts[0]) >= 4):
        return None
    if known / len(token) < 0.45 and not suffix:
        return None
    return " ".join(parts)


def _longest_word(token: str, index: int, words: list[str]) -> str | None:
    for word in words:
        if token.startswith(word, index):
            return word
    return None


def _word_at_boundary(word: str, has_previous: bool) -> bool:
    if word in _ASSET_ABBREV or word == "CLASS":
        return True
    if len(word) >= 5:
        return True
    # NEW / YORK / FUND / CORE and the other short words only at a boundary
    # we already reached, or at the start of the token.
    return has_previous or word in _ASSET_WORDS


def columns_from_vline_centers(
    centers: list[int], page_width: int
) -> dict | None:
    """Map vertical ruling-line centers to PTR columns.

    Returns owner, asset, type, date, and amount boxes as (left, right) pairs.
    Amount boxes are the ten value brackets, left to right. An extra narrow
    column on the right is the spouse/dependent note, not an amount.
    """
    centers = _merge_close_centers(centers, page_width)
    if page_width <= 0 or len(centers) < 8:
        return None
    gaps = [
        (centers[i], centers[i + 1], centers[i + 1] - centers[i])
        for i in range(len(centers) - 1)
        if centers[i + 1] > centers[i]
    ]
    best: tuple[tuple[int, int, int], int, int] | None = None
    for start in range(len(gaps)):
        for count in range(9, 13):
            end = start + count
            if end > len(gaps):
                break
            chunk = gaps[start:end]
            widths = [gap[2] for gap in chunk]
            med = sorted(widths)[len(widths) // 2]
            if not (0.012 * page_width <= med <= 0.06 * page_width):
                continue
            # One column may be wider where a rule did not print. More than
            # one odd gap means this is not the amount grid.
            odd = [
                width
                for width in widths
                if width > med * 1.45 or width < med * 0.68
            ]
            if len(odd) > 1 or max(widths) > med * 2.3 or min(widths) < med * 0.68:
                continue
            right = chunk[-1][1]
            left = chunk[0][0]
            if right < page_width * 0.72 or left < page_width * 0.40:
                continue
            # The column just left of the amount grid is a date, so it is
            # wider than the amount boxes. A run that starts mid-grid is not.
            if start == 0:
                continue
            date_width = gaps[start - 1][2]
            if date_width < med * 1.4:
                continue
            score = (right, -abs(count - 10), count)
            if best is None or score > best[0]:
                best = (score, start, end)
    if best is None:
        return None
    _, start, end = best
    amounts = list(gaps[start:end])
    if len(amounts) > 10:
        amounts = amounts[:10]
    med = sorted(gap[2] for gap in amounts)[len(amounts) // 2]
    # A checked box after a double-wide gap is shifted, so those indexes
    # are not trusted. Columns before the gap still line up.
    amount_trust = len(amounts)
    for index, gap in enumerate(amounts):
        if gap[2] > med * 1.45:
            amount_trust = index
            break
    left = gaps[:start]
    dates: list[tuple[int, int, int]] = []
    while left and len(dates) < 2 and left[-1][2] >= med * 1.45:
        dates.insert(0, left.pop())
    if not dates:
        return None
    types: list[tuple[int, int, int]] = []
    while left and len(types) < 4 and left[-1][2] <= med * 1.45:
        types.insert(0, left.pop())
    if len(types) == 5:
        types = types[-4:]
    if len(types) not in (3, 4) or not left:
        return None
    asset = left.pop()
    if asset[2] < page_width * 0.10:
        return None
    owner = None
    if left and page_width * 0.015 <= left[-1][2] <= page_width * 0.12:
        owner = left[-1]
    return {
        "owner": _pair(owner),
        "asset": _pair(asset),
        "types": [_pair(gap) for gap in types],
        "dates": [_pair(gap) for gap in dates],
        "amounts": [_pair(gap) for gap in amounts],
        "amount_trust": amount_trust,
    }


def _merge_close_centers(centers: list[int], page_width: int) -> list[int]:
    """Collapse a double-printed ruling line into one center.

    Amount and type columns are wider than this, so real columns stay split.
    """
    if not centers:
        return []
    # Narrower than an amount checkbox (about 2.1% of the page on these
    # scans) and wider than a double-struck rule (about 1.8%).
    limit = max(4, int(page_width * 0.0195))
    merged = [int(centers[0])]
    for center in centers[1:]:
        current = int(center)
        if current - merged[-1] <= limit:
            merged[-1] = (merged[-1] + current) // 2
        else:
            merged.append(current)
    return merged


def _pair(gap: tuple[int, int, int] | None) -> tuple[int, int] | None:
    if gap is None:
        return None
    return (gap[0], gap[1])


def page_has_ptr_grid(image) -> bool:
    """True when this page has the PTR ruling lines, even if no row was readable."""
    return _has_ptr_grid(image)


def transactions_from_ptr_image(image) -> list[dict]:
    """Transaction rows from one upright grayscale page. Empty when unreadable."""
    rows = _parse_oriented(image)
    if rows:
        return rows
    # OSD already turned the page upright. A page that already has vertical
    # rules is not a sideways scan, so rotating it would invent a table.
    if _has_ptr_grid(image) or _has_vertical_rules(image):
        return []
    for angle in (90, 270, 180):
        try:
            rotated = image.rotate(angle, expand=True)
        except Exception:
            continue
        rows = _parse_oriented(rotated)
        if rows:
            logger.info("House PTR grid matched after %s degree rotation", angle)
            return rows
    return []


def _image_array(image):
    import numpy as np

    arr = np.asarray(image.convert("L"))
    if getattr(arr, "ndim", 0) != 2 or min(arr.shape) < 8:
        return None
    return arr


def _has_ptr_grid(image) -> bool:
    try:
        arr = _image_array(image)
    except Exception:
        return False
    if arr is None:
        return False
    return _columns_for_image(arr) is not None


def _has_vertical_rules(image) -> bool:
    try:
        arr = _image_array(image)
    except Exception:
        return False
    if arr is None:
        return False
    dark = arr < 140
    centers = _line_centers(dark, axis="v", min_frac=0.22, y0=0.30, y1=0.98)
    return len(centers) >= 8


def _parse_oriented(image) -> list[dict]:
    try:
        import numpy as np
    except ImportError:
        logger.warning("numpy unavailable; skipping House PTR table OCR")
        return []
    gray = image.convert("L")
    arr = np.asarray(gray)
    columns = _columns_for_image(arr)
    if not columns:
        return []
    bands = _text_bands(arr, columns["asset"])
    if not bands:
        return []
    date_hits = [_date_hits(gray, pair) for pair in columns["dates"]]
    fallback: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for top, bottom in bands:
        row = _read_band(gray, arr, columns, top, bottom, date_hits)
        if not row:
            continue
        key = (
            row["asset_name"].upper(),
            row["transaction_date"],
            row["filer_relation"],
        )
        if key in seen:
            continue
        seen.add(key)
        fallback.append(row)
    return fallback


def _column_lines(image, box, psm: int, whitelist: str | None = None, min_conf: float = 0):
    try:
        import pytesseract
    except ImportError:
        return []
    x0, x1 = box
    x0 = max(0, x0 + 1)
    x1 = max(x0 + 2, x1 - 1)
    cell = image.crop((x0, 0, x1, image.size[1]))
    config = f"--psm {psm} --oem 1 -c preserve_interword_spaces=1"
    if whitelist:
        config += f" -c tessedit_char_whitelist={whitelist}"
    try:
        data = pytesseract.image_to_data(
            cell, config=config, output_type=pytesseract.Output.DICT
        )
    except Exception as exc:
        logger.warning("House PTR column OCR failed: %s", exc)
        return []
    words = []
    texts = data.get("text") or []
    for index, raw in enumerate(texts):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][index])
        except (TypeError, ValueError, KeyError):
            conf = -1
        if conf < min_conf:
            continue
        top = int(data["top"][index])
        height = int(data["height"][index])
        if height <= 0 or height > image.size[1] * 0.2:
            continue
        words.append(
            {
                "text": text,
                "top": top,
                "bottom": top + height,
                "left": int(data["left"][index]),
            }
        )
    words.sort(key=lambda word: (word["top"], word["left"]))
    heights = sorted(word["bottom"] - word["top"] for word in words)
    typical = heights[len(heights) // 2] if heights else 12
    # Cluster on the top of each word. A tall box's bottom reaches into the
    # next row, so overlap-on-bottom glues two trades together.
    slack = max(6, int(typical * 0.55))
    lines = []
    for word in words:
        if lines and abs(word["top"] - lines[-1]["top"]) <= slack:
            lines[-1]["words"].append(word)
            lines[-1]["top"] = min(lines[-1]["top"], word["top"])
            lines[-1]["bottom"] = max(lines[-1]["bottom"], word["bottom"])
        else:
            lines.append(
                {
                    "top": word["top"],
                    "bottom": word["bottom"],
                    "words": [word],
                }
            )
    cooked = []
    for line in lines:
        ordered = sorted(line["words"], key=lambda word: word["left"])
        text = " ".join(word["text"] for word in ordered)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        top = line["top"]
        bottom = line["bottom"]
        cooked.append(
            {
                "text": text,
                "top": top,
                "bottom": bottom,
                "center": (top + bottom) / 2,
                "height": max(8, bottom - top),
            }
        )
    return cooked


def _date_hits(image, box) -> list[tuple[float, str]]:
    hits = []
    for line in _column_lines(
        image, box, psm=6, whitelist="0123456789/", min_conf=0
    ):
        parsed = parse_ptr_date(line["text"])
        if parsed and parsed != "01/11/2010":
            hits.append((line["center"], parsed))
    return hits


def _date_near(hits, top: int, bottom: int) -> str | None:
    if not hits:
        return None
    mid = (top + bottom) / 2
    limit = max(28, (bottom - top) * 1.2)
    best = None
    best_dist = limit
    for center, parsed in hits:
        dist = abs(center - mid)
        if dist < best_dist:
            best = parsed
            best_dist = dist
    return best


def _columns_for_image(arr):
    height, width = arr.shape[:2]
    # Try a few darkness cutoffs. Faint fax scans need a higher threshold
    # (more pixels count as ink) before the ruling lines show up.
    for cutoff in (140, 170, 110, 190):
        dark = arr < cutoff
        centers = _line_centers(dark, axis="v", min_frac=0.34, y0=0.32, y1=0.99)
        columns = columns_from_vline_centers(centers, width)
        if columns:
            return columns
        # Short tables: a line only has to cross about a quarter of the page.
        centers = _line_centers(dark, axis="v", min_frac=0.22, y0=0.40, y1=0.98)
        columns = columns_from_vline_centers(centers, width)
        if columns:
            return columns
    if height < 10 or width < 10:
        return None
    return None


def _line_centers(dark, axis: str, min_frac: float, y0: float, y1: float) -> list[int]:
    import numpy as np

    height, width = dark.shape
    if axis == "v":
        band = dark[int(height * y0) : int(height * y1), :]
        runs = _longest_runs(band, along="row")
        limit = band.shape[0] * min_frac
        idx = np.where(runs >= limit)[0]
        gap = max(3, width // 450)
    else:
        band = dark[:, int(width * y0) : int(width * y1)]
        runs = _longest_runs(band, along="col")
        limit = band.shape[1] * min_frac
        idx = np.where(runs >= limit)[0]
        gap = max(3, height // 500)
    return _cluster(idx, gap)


def _longest_runs(band, along: str):
    import numpy as np

    if along == "row":
        # Longest consecutive dark run in each column.
        values = band
    else:
        values = band.T
    longest = np.zeros(values.shape[1], dtype=np.int32)
    run = np.zeros(values.shape[1], dtype=np.int32)
    ones = values.astype(np.int32)
    for row in ones:
        run = (run + row) * row
        np.maximum(longest, run, out=longest)
    return longest


def _cluster(indexes, gap: int) -> list[int]:
    if len(indexes) == 0:
        return []
    groups: list[list[int]] = [[int(indexes[0])]]
    for value in indexes[1:]:
        current = int(value)
        if current - groups[-1][-1] <= gap:
            groups[-1].append(current)
        else:
            groups.append([current])
    return [int(sum(group) / len(group)) for group in groups]


def _text_bands(arr, asset: tuple[int, int]) -> list[tuple[int, int]]:
    """Horizontal bands in the asset column that actually contain writing."""
    import numpy as np

    height = arr.shape[0]
    x0, x1 = asset
    x0 = max(0, x0 + 2)
    x1 = min(arr.shape[1], x1 - 2)
    if x1 <= x0 + 8:
        return []
    ink = (arr[:, x0:x1] < 150).mean(axis=1)
    # Ignore a faint rule that survived the crop.
    active = ink > 0.012
    bands: list[tuple[int, int]] = []
    start = None
    for y, on in enumerate(active.tolist()):
        if on and start is None:
            start = y
        elif not on and start is not None:
            if y - start >= 8:
                bands.append((start, y))
            start = None
    if start is not None and height - start >= 8:
        bands.append((start, height))
    if not bands:
        return []
    heights = [b - a for a, b in bands]
    typical = sorted(heights)[len(heights) // 2]
    merged: list[tuple[int, int]] = []
    for top, bottom in bands:
        # Only glue ink that is actually touching. A one-pixel gap is a
        # broken stroke; a few pixels of whitespace is the next trade.
        if merged and top - merged[-1][1] <= 1:
            merged[-1] = (merged[-1][0], bottom)
        else:
            merged.append((top, bottom))
    padded: list[tuple[int, int]] = []
    pad = max(4, int(typical * 0.35))
    for index, (top, bottom) in enumerate(merged):
        prev_bottom = merged[index - 1][1] if index else 0
        next_top = merged[index + 1][0] if index + 1 < len(merged) else height
        padded.append(
            (
                max(prev_bottom, top - pad),
                min(next_top, bottom + pad),
            )
        )
    return [(a, b) for a, b in padded if b - a >= 10]


def _read_band(gray, arr, columns: dict, top: int, bottom: int, date_hits=None) -> dict | None:
    asset_box = (columns["asset"][0], top, columns["asset"][1], bottom)
    if _ink_fraction(arr, asset_box) < 0.008:
        return None
    owner_code = None
    if columns.get("owner"):
        owner_text = _ocr_cell(
            gray,
            (columns["owner"][0], top, columns["owner"][1], bottom),
            psm=7,
        )
        owner_code = _owner_code(owner_text)
    asset_text = _ocr_cell(gray, asset_box, psm=6)
    asset_owner, asset_name = _clean_asset(asset_text)
    owner_code = owner_code or asset_owner
    if not _plausible_asset(asset_name):
        return None
    dates = []
    for index, (left, right) in enumerate(columns["dates"]):
        raw = _ocr_cell(
            gray,
            (left, top, right, bottom),
            psm=7,
            whitelist="0123456789/",
            scale=2,
        )
        parsed = parse_ptr_date(raw)
        if not parsed:
            raw = _ocr_cell(gray, (left, top, right, bottom), psm=7, scale=2)
            parsed = parse_ptr_date(raw)
        if not parsed and date_hits and index < len(date_hits):
            parsed = _date_near(date_hits[index], top, bottom)
        dates.append(parsed)
    txn_date = next((item for item in dates if item), None)
    # 01/11/2010 is the revision date printed on the form, not a trade.
    if not txn_date or txn_date == "01/11/2010":
        return None
    notify = None
    for item in dates[1:]:
        if item and item != txn_date:
            notify = item
            break
        if item and notify is None:
            notify = item
    # Two identical dates are normal (same-day notice). Keep the second.
    if len(dates) > 1 and dates[1]:
        notify = dates[1]
    type_labels = _TYPES_4 if len(columns["types"]) == 4 else _TYPES_3
    type_fracs = [
        _ink_fraction(arr, (left, top, right, bottom))
        for left, right in columns["types"]
    ]
    amount_fracs = [
        _ink_fraction(arr, (left, top, right, bottom))
        for left, right in columns["amounts"]
    ]
    ttype = "other"
    chosen = _checked_index(type_fracs)
    if chosen is not None and chosen < len(type_labels):
        ttype = type_labels[chosen]
    if ttype == "sale" and "partial" in asset_name.lower():
        ttype = "sale_partial"
    amount = ""
    chosen_amt = _checked_index(amount_fracs)
    trust = columns.get("amount_trust", len(amount_fracs))
    if (
        chosen_amt is not None
        and chosen_amt < len(AMOUNT_RANGES)
        and chosen_amt < trust
    ):
        amount = AMOUNT_RANGES[chosen_amt]
    relation = {"SP": "spouse", "JT": "joint", "DC": "dependent_child"}.get(
        owner_code or "", "self"
    )
    row = {
        "asset_name": asset_name,
        "filer_relation": relation,
        "transaction_type": ttype,
        "transaction_date": txn_date,
        "amount_range_raw": amount,
    }
    if notify:
        row["notification_date"] = notify
    return row


def _checked_index(fracs: list[float]) -> int | None:
    """Index of a single clearly marked checkbox. None when ambiguous."""
    if not fracs:
        return None
    order = sorted(range(len(fracs)), key=lambda i: fracs[i], reverse=True)
    best = order[0]
    second = fracs[order[1]] if len(order) > 1 else 0.0
    if fracs[best] < 0.06:
        return None
    if fracs[best] < second + 0.035:
        return None
    return best


def _ink_fraction(arr, box: tuple[int, int, int, int], inset: float = 0.2) -> float:
    x0, y0, x1, y1 = box
    width = x1 - x0
    height = y1 - y0
    if width < 6 or height < 6:
        return 0.0
    ix0 = x0 + int(width * inset)
    ix1 = x1 - int(width * inset)
    iy0 = y0 + int(height * inset)
    iy1 = y1 - int(height * inset)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    cell = arr[iy0:iy1, ix0:ix1]
    if cell.size == 0:
        return 0.0
    return float((cell < 145).mean())


def _ocr_cell(image, box, psm: int, whitelist: str | None = None, scale: int = 1) -> str:
    try:
        import pytesseract
    except ImportError:
        return ""
    x0, y0, x1, y1 = box
    x0 = max(0, x0 + 1)
    y0 = max(0, y0 + 1)
    x1 = max(x0 + 2, x1 - 1)
    y1 = max(y0 + 2, y1 - 1)
    cell = image.crop((x0, y0, x1, y1))
    if scale > 1 or cell.size[1] < 36:
        factor = max(scale, 2 if cell.size[1] < 36 else 1)
        if factor > 1:
            cell = cell.resize(
                (cell.size[0] * factor, cell.size[1] * factor)
            )
    config = f"--psm {psm} --oem 1 -c preserve_interword_spaces=1"
    if whitelist:
        config += f" -c tessedit_char_whitelist={whitelist}"
    try:
        return pytesseract.image_to_string(cell, config=config) or ""
    except Exception as exc:
        logger.warning("House PTR cell OCR failed: %s", exc)
        return ""


def _owner_code(text: str) -> str | None:
    """SP / JT / DC only when that code is actually written in the cell.

    A character whitelist makes an empty cell look like ``JT``, so the cell
    is read as normal text and the code has to stand on its own.
    """
    found = set(re.findall(r"\b(SP|JT|DC)\b", (text or "").upper()))
    if len(found) == 1:
        return found.pop()
    return None


def _clean_asset(text: str) -> tuple[str | None, str]:
    flattened = text.replace("\n", " ")
    flattened = re.sub(r"\bIll\b", "III", flattened)
    flattened = re.sub(r"[|\[\]{}<>_=*~]+", " ", flattened)
    flattened = re.sub(r"^[^A-Za-z0-9(]+", "", flattened)
    flattened = re.sub(r"\s+", " ", flattened).strip(" .,-/")
    owner = None
    match = re.match(r"^(SP|JT|DC)\b\s*", flattened, re.IGNORECASE)
    if match:
        owner = match.group(1).upper()
        flattened = flattened[match.end() :]
    # A ruling line or footnote mark often OCRs as a leading 1 or !.
    flattened = re.sub(r"^[!1lI]\s+(?=[A-Za-z])", "", flattened)
    flattened = restore_asset_spacing(flattened)
    # A class-share "I" often OCRs as "!" or "1", including "CLASS!" after
    # a glued token is split.
    flattened = re.sub(
        r"(?i)\bCLASS\s*[!|1lI](?=\s|$|[^A-Za-z0-9])",
        "CLASS I",
        flattened,
    )
    flattened = re.sub(r"\s+", " ", flattened).strip(" .,-/")
    return owner, flattened


def _plausible_asset(name: str) -> bool:
    if not name or len(name) > 140:
        return False
    upper = name.upper()
    if any(snip in upper for snip in _ASSET_REJECT):
        return False
    if _is_form_boilerplate(upper):
        return False
    # A ruling line glued to a class line ("ICLASS A") is still the form, not an issuer.
    fence = re.match(r"^[|!1IL]", upper)
    if fence:
        rest = upper[fence.end() :].lstrip()
        if rest and _is_form_boilerplate(rest):
            return False
    letters = re.sub(r"[^A-Za-z]", "", name)
    if len(letters) < 3:
        return False
    # A row of template digits or a single repeated glyph is not an asset.
    if len(set(letters.upper())) == 1:
        return False
    return True


# Tokens that name a share class or a legal suffix, not an issuer.
_GENERIC_ASSET_TOKENS = {
    "A",
    "AND",
    "ADR",
    "B",
    "C",
    "CL",
    "CLASS",
    "CMN",
    "CMIN",
    "CMM",
    "CMNN",
    "COMN",
    "CVIN",
    "CO",
    "COM",
    "COMMON",
    "COMPANY",
    "CORP",
    "CORPORATION",
    "ETF",
    "FUND",
    "FUNDS",
    "HOLDING",
    "HOLDINGS",
    "I",
    "II",
    "III",
    "INC",
    "INCORPORATED",
    "LLC",
    "LP",
    "LTD",
    "OF",
    "PERPETUAL",
    "PLC",
    "REIT",
    "SHARE",
    "SHARES",
    "SHS",
    "STOCK",
    "STOCKS",
    "THE",
    "TR",
    "TRUST",
    "USD",
}


def _is_form_boilerplate(upper: str) -> bool:
    """Preprinted instructions and the sample 'Example Mega Corp' row."""
    letters = re.sub(r"[^A-Z]", "", upper)
    if "FILEAN" in letters or "NOTHINGTOREPORT" in letters or "PENALTY" in letters:
        return True
    if any(snip in letters for snip in ("ORIGINALAND", "ORGINALAND", "ONGINALAND")):
        return True
    if "EXAMPLE" in letters or "EXMPLE" in letters or "EXAMPL" in letters:
        return True
    if "MEGCORP" in letters or "MEGACORP" in letters or "MAGCORPCOMMON" in letters:
        return True
    # "E mp! Meg Com Comm Stock" is the same sample row with the letters broken.
    if (
        "STOCK" in letters
        and "MEG" in letters
        and "COM" in letters
        and len(letters) <= 28
    ):
        return True
    if letters.startswith("EXMP") and "STOCK" in letters and len(letters) <= 32:
        return True
    if "COMMONSTOCK" in letters and letters.startswith("EXM"):
        return True
    if any(
        snip in letters
        for snip in (
            "ANSWEREDYES",
            "FURTHERGUIDANCE",
            "PLEASECONTACT",
            "PURCHASEANYSHARES",
            "ALLOCATEDAS",
            "INITIALPUBLICOFFERING",
            "SEETHEATTACHED",
        )
    ):
        return True
    if "THISQUESTION" in letters and ("YES" in letters or "CONTACT" in letters):
        return True
    if "PLEASESEE" in letters and "ATTACH" in letters:
        return True
    if "PUBLICOFFERING" in letters and "SHARES" in letters:
        return True
    if _only_generic_asset(upper):
        return True
    return False


def _only_generic_asset(upper: str) -> bool:
    """True when the line is only 'COMMON STOCK' / 'CORP CLASS A' and no issuer."""
    tokens = re.findall(r"[A-Z]+", upper)
    core = [
        token
        for token in tokens
        if token not in _GENERIC_ASSET_TOKENS and len(token) > 1
    ]
    if not core:
        return True
    if max(len(token) for token in core) < 4 and sum(len(token) for token in core) < 8:
        return True
    return False
