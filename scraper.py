"""カードラッシュ デュエマ買取表の価格を毎日記録するプログラム。

- 全カード一覧（買取価格の高い順）と「只今の買取額UP!!」（is_hot=true）を取得
- 100〜30,000円のカードだけを記録
- 価格（または強化中かどうか）が変わったときだけ履歴に点を追加
- docs/data.json（グラフページ用）を作り直す
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://cardrush.media/duel_masters/buying_prices"
MIN_PRICE = 100
MAX_PRICE = 30000
PER_PAGE = 100
MAX_PAGES = 200          # 念のための上限
SLEEP_SEC = 2            # サイトに負担をかけないための待ち時間

ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(ROOT, "data", "history.json")
DOCS_DATA_PATH = os.path.join(ROOT, "docs", "data.json")
DEBUG_DIR = os.path.join(ROOT, "debug")

JST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (personal price tracker; once a day)",
    "Accept-Language": "ja,en;q=0.8",
}


# ---------------------------------------------------------------- 取得

def fetch_page(page, hot_only):
    params = {
        "limit": PER_PAGE,
        "page": page,
        "sort[key]": "amount",
        "sort[order]": "desc",
    }
    if hot_only:
        params["is_hot"] = "true"
    for attempt in range(3):
        try:
            r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=60)
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            print(f"  取得失敗 (page {page}, {attempt + 1}回目): {e}")
            time.sleep(10)
    raise RuntimeError(f"ページ {page} を取得できませんでした")


# ---------------------------------------------------------------- 読み取り

def to_int_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.sub(r"[^\d]", "", str(v))
    return int(m) if m else None


def _find_item_lists(obj, found):
    """JSONの中から「name と amount を持つ要素の配列」を探す。"""
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if dicts and all("amount" in d and "name" in d for d in dicts):
            found.append(dicts)
            return
        for x in obj:
            _find_item_lists(x, found)
    elif isinstance(obj, dict):
        for v in obj.values():
            _find_item_lists(v, found)


def parse_from_json(html):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for s in soup.find_all("script"):
        text = s.string or s.get_text() or ""
        text = text.strip()
        if '"amount"' not in text:
            continue
        # <script type="application/json"> や __NEXT_DATA__ はそのままJSON
        blobs = [text]
        # window.__NUXT__ = {...} などの代入形式
        m = re.search(r"=\s*(\{.*\}|\[.*\])\s*;?\s*$", text, re.S)
        if m:
            blobs.append(m.group(1))
        for b in blobs:
            try:
                data = json.loads(b)
            except ValueError:
                continue
            _find_item_lists(data, candidates)
    if not candidates:
        return None
    items = max(candidates, key=len)
    out = []
    for d in items:
        out.append({
            "name": str(d.get("name", "")).strip(),
            "model": str(d.get("model_number") or "").strip(),
            "amount": to_int_price(d.get("amount")),
            "hot": bool(d.get("is_hot")) if "is_hot" in d else None,
        })
    return out


def parse_from_table(html):
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        h_idx = next((i for i, r in enumerate(rows) if "買取価格" in r.get_text()), 0)
        header = [c.get_text(strip=True) for c in rows[h_idx].find_all(["th", "td"])]
        rows = rows[h_idx:]
        def col(*words):
            for i, h in enumerate(header):
                if any(w in h for w in words):
                    return i
            return None
        i_name, i_model, i_price = col("カード名", "商品名"), col("型番"), col("買取価格", "価格")
        if i_name is None or i_price is None:
            continue
        out = []
        for tr in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) <= max(i_name, i_price):
                continue
            out.append({
                "name": cells[i_name],
                "model": cells[i_model] if i_model is not None and i_model < len(cells) else "",
                "amount": to_int_price(cells[i_price]),
                "hot": None,
            })
        if out:
            return out
    return None


def parse_items(html):
    items = parse_from_json(html)
    if not items:
        items = parse_from_table(html)
    if not items:
        return []
    return [x for x in items if x["name"] and x["amount"] is not None]


def save_debug(html, label):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    path = os.path.join(DEBUG_DIR, f"{label}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  調査用にページを保存しました: {path}")


def collect(hot_only):
    label = "強化中" if hot_only else "全カード"
    print(f"[{label}] 取得開始")
    all_items = []
    for page in range(1, MAX_PAGES + 1):
        html = fetch_page(page, hot_only)
        items = parse_items(html)
        if not items:
            if page == 1:
                save_debug(html, f"{'hot' if hot_only else 'all'}_page1")
                raise RuntimeError(f"[{label}] 1ページ目からカードを読み取れませんでした。")
            break
        all_items.extend(items)
        lowest = min(x["amount"] for x in items)
        print(f"  {page}ページ: {len(items)}件 (最安 ¥{lowest:,})")
        if len(items) < PER_PAGE:
            break
        if lowest < MIN_PRICE:
            break  # 高い順なので、これより先は範囲外
        time.sleep(SLEEP_SEC)
    print(f"[{label}] 合計 {len(all_items)}件")
    return all_items


# ---------------------------------------------------------------- 記録

def make_key(item):
    return f"{item['name']}｜{item['model']}"


def build_snapshot(all_items, hot_items):
    hot_keys = {make_key(x) for x in hot_items}
    snap = {}
    for src in (all_items, hot_items):
        for x in src:
            key = make_key(x)
            if key in snap:
                continue  # 同じカードが重複した場合は最初のものを使う
            hot = x["hot"] if x["hot"] is not None else (key in hot_keys)
            snap[key] = {"name": x["name"], "model": x["model"],
                         "amount": x["amount"], "hot": bool(hot or key in hot_keys)}
    return snap


def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"runs": [], "cards": {}}


def update_history(history, snap, today):
    cards = history["cards"]
    in_range = sum(1 for c in snap.values() if MIN_PRICE <= c["amount"] <= MAX_PRICE)
    prev_count = history["runs"][-1]["count"] if history["runs"] else 0
    # 取得が途中で失敗して件数が極端に少ない日は記録しない（誤った変動を防ぐ）
    if prev_count and in_range < prev_count * 0.5:
        raise RuntimeError(f"取得件数が前回({prev_count})より大幅に少ない({in_range})ため記録を中止しました")

    changed = 0
    for key, c in snap.items():
        point = [today, c["amount"], 1 if c["hot"] else 0]
        if key not in cards:
            # 新しく追うのは範囲内のカードだけ。一度追い始めたら範囲外に出ても追い続ける
            if not (MIN_PRICE <= c["amount"] <= MAX_PRICE):
                continue
            cards[key] = {"name": c["name"], "model": c["model"], "points": [point]}
            continue
        last = cards[key]["points"][-1]
        if last[0] == today:
            cards[key]["points"][-1] = point   # 同じ日に再実行した場合は上書き
        elif last[1] != c["amount"] or last[2] != point[2]:
            cards[key]["points"].append(point)
            if last[1] != c["amount"]:
                changed += 1
        cards[key]["seen"] = today

    if history["runs"] and history["runs"][-1]["date"] == today:
        history["runs"].pop()
    history["runs"].append({"date": today, "count": in_range, "changed": changed})
    return changed


def write_docs_data(history, today):
    """グラフページ用：一度でも価格が動いたカードだけを書き出す。"""
    out = []
    for key, c in history["cards"].items():
        prices = {p[1] for p in c["points"]}
        hot_flags = {p[2] for p in c["points"]}
        if len(prices) < 2 and len(hot_flags) < 2:
            continue
        out.append({"n": c["name"], "m": c["model"], "p": c["points"],
                    "s": c.get("seen", c["points"][-1][0])})
    data = {
        "updated": today,
        "range": [MIN_PRICE, MAX_PRICE],
        "runs": history["runs"][-400:],
        "tracked": history["runs"][-1]["count"] if history["runs"] else 0,
        "cards": out,
    }
    os.makedirs(os.path.dirname(DOCS_DATA_PATH), exist_ok=True)
    with open(DOCS_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"グラフ用データ: 変動カード {len(out)}件")


def main():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    all_items = collect(hot_only=False)
    hot_items = collect(hot_only=True)
    snap = build_snapshot(all_items, hot_items)
    print(f"取得したカード: {len(snap)}件（うち{MIN_PRICE}〜{MAX_PRICE}円は記録対象）")

    history = load_history()
    changed = update_history(history, snap, today)
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{today}: 価格変動 {changed}件")
    write_docs_data(history, today)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"エラー: {e}")
        sys.exit(1)
