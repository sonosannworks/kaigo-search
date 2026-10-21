#!/usr/bin/env python3
"""介護保険事業所検索(全国版)のデータを作るスクリプト。

厚生労働省「介護サービス情報公表システム」オープンデータ(CSV)を取得し、
都道府県 → 市区町村(政令指定都市は区ごと)に分けた JSON を data/ に書き出す。

使い方(このファイルがあるフォルダで実行):
    python build_data.py                 # 厚労省サイトから最新CSVを取得して変換
    python build_data.py --csv-dir csv   # 手元に保存したCSVフォルダから変換
    python build_data.py --as-of 2026年12月末時点   # 時点の表記を指定

Python 3.8 以上、標準ライブラリだけで動きます(追加インストール不要)。
"""
import argparse
import hashlib
import csv
import io
import json
import re
import shutil
import statistics
import sys
import time
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

OPENDATA_PAGE = "https://www.mhlw.go.jp/stf/kaigo-kouhyou_opendata.html"
OPENDATA_BASE = "https://www.mhlw.go.jp"
USER_AGENT = "Mozilla/5.0 (kaigo-search data builder; sonosann Web&AI support)"

# 廃止されたサービス(2024年3月末で制度終了)。検索対象から外す。
COORD_MAX_DEV = 0.6  # 度(約60km)。これより市区町村の中心から離れた座標は捨てる
EXCLUDE_SERVICE_CODES = {"530"}
EXCLUDE_SERVICE_NAMES = {"介護療養型医療施設"}

PREFS = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県",
    "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
    "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
PREF_CODE = {name: f"{i + 1:02d}" for i, name in enumerate(PREFS)}

# 政令指定都市(市の団体コード5桁)。区の行を市にまとめるために使う。
DESIGNATED_CITIES = {
    "札幌市": "01100", "仙台市": "04100", "さいたま市": "11100", "千葉市": "12100",
    "横浜市": "14100", "川崎市": "14130", "相模原市": "14150", "新潟市": "15100",
    "静岡市": "22100", "浜松市": "22130", "名古屋市": "23100", "京都市": "26100",
    "大阪市": "27100", "堺市": "27140", "神戸市": "28100", "岡山市": "33100",
    "広島市": "34100", "北九州市": "40100", "福岡市": "40130", "熊本市": "43100",
}

# 使う列(CSVの見出し名 → 短いキー)
COLUMNS = {
    "code": "都道府県コード又は市町村コード",
    "pref": "都道府県名",
    "city": "市区町村名",
    "name": "事業所名",
    "kana": "事業所名カナ",
    "service": "サービスの種類",
    "addr": "住所",
    "bldg": "方書（ビル名等）",
    "lat": "緯度",
    "lng": "経度",
    "tel": "電話番号",
    "fax": "FAX番号",
    "corp": "法人の名称",
    "no": "事業所番号",
    "days": "利用可能曜日",
    "url": "URL",
}

# 1件分の並び(JSONを小さくするため配列で持つ)。ページ側もこの順で読む。
FIELDS = ["s", "n", "k", "a", "tel", "fax", "corp", "days", "url", "lat", "lng", "w"]


# ---------- 取得 ----------

def http_get(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as res:
                return res.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"取得に失敗しました: {url} ({last})")


def find_latest_csv_links(html):
    """オープンデータのページから、サービスごとの最新CSVのURLを探す。"""
    found = {}
    for m in re.finditer(r'href="([^"]*jigyosho_(\d+)_all_(\d{8,14})\.csv)"', html):
        href, code, stamp = m.group(1), m.group(2), m.group(3)
        if code in found and found[code][1] >= stamp:
            continue
        url = href if href.startswith("http") else OPENDATA_BASE + href
        found[code] = (url, stamp)
    return found


def as_of_from_stamp(stamp):
    """ファイル名の日付(出力日)から「〇年〇月末時点」を推定する。"""
    y, m = int(stamp[:4]), int(stamp[4:6])
    if m <= 6:
        return f"{y - 1}年12月末時点"
    return f"{y}年6月末時点"


def load_sources(args):
    """(サービスコード, CSVテキスト) の一覧と、データ時点の表記を返す。"""
    sources = []
    stamps = []
    if args.csv_dir:
        for p in sorted(Path(args.csv_dir).glob("*.csv")):
            m = re.search(r"jigyosho_(\d+)_all_(\d{8,14})", p.name)
            code = m.group(1) if m else p.stem
            if m:
                stamps.append(m.group(2))
            sources.append((code, p.read_bytes()))
        if not sources:
            sys.exit(f"CSVが見つかりません: {args.csv_dir}")
    else:
        print("厚生労働省のオープンデータのページを確認しています…")
        html = http_get(OPENDATA_PAGE).decode("utf-8", errors="replace")
        links = find_latest_csv_links(html)
        if not links:
            sys.exit("CSVのリンクが見つかりませんでした。ページの構成が変わった可能性があります。")
        save_dir = Path(args.save_csv) if args.save_csv else None
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
        for i, (code, (url, stamp)) in enumerate(sorted(links.items()), 1):
            print(f"  [{i}/{len(links)}] {code} を取得中…", flush=True)
            raw = http_get(url)
            if save_dir:
                (save_dir / url.rsplit("/", 1)[-1]).write_bytes(raw)
            sources.append((code, raw))
            stamps.append(stamp)
            time.sleep(0.5)
    as_of = args.as_of or (as_of_from_stamp(max(stamps)) if stamps else "")
    return sources, as_of


def decode_csv(raw):
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------- 整形 ----------

def nfkc(v):
    return unicodedata.normalize("NFKC", v or "").strip()


def clean_tel(v):
    v = nfkc(v).replace("ー", "-").replace("―", "-").replace("‐", "-").replace("−", "-")
    v = re.sub(r"\s+", "", v)
    if not v or not re.search(r"\d", v):
        return ""
    v = re.sub(r"^\((0\d{1,4})\)", r"\1-", v)  # (096)351-8088 → 096-351-8088
    v = re.sub(r"(?<=\d)\((\d{1,4})\)", r"-\1-", v)  # 0984(23)1234 → 0984-23-1234
    v = re.split(r"[/、,・~～]|内線|\(", v)[0]  # 2つ目の番号や注記は落とす
    v = re.sub(r"[^\d-]", "", v).strip("-")
    v = re.sub(r"-{2,}", "-", v)
    digits = re.sub(r"\D", "", v)
    if "-" not in v and len(digits) == 10 and digits.startswith("0"):
        # 市外局番の桁数は地域で違うので、ハイフンなしの番号だけ一般的な形に整える
        if digits[:2] in ("03", "06"):
            return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if "-" not in v and len(digits) == 11 and digits[:3] in ("090", "080", "070", "050"):
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return v


def clean_url(v):
    u = nfkc(v).replace("⁻", "-").replace("−", "-")
    u = re.sub(r"\s+", "", u)
    if not u or "@" in u and not u.startswith("http"):
        return ""
    scheme = re.match(r"^h?t?t?p+s?\s*[:;]?\s*/+", u, re.I)
    if scheme:
        u = ("https://" if "s" in scheme.group(0).lower() else "http://") + u[scheme.end():]
    if not re.match(r"^https?://", u):
        u = "http://" + u
    u = u.rstrip(".")
    host = re.sub(r"^https?://", "", u).split("/")[0]
    if "." not in host:
        return ""
    return u


def clean_addr(addr, pref, city):
    a = nfkc(addr).replace(" ", "").replace("　", "")
    a = re.sub(r"(?<=\d)[‐－―ー−](?=\d)", "-", a)
    if a.startswith(pref):
        a = a[len(pref):]
    base, ward = split_city_ward(pref, city)
    # 「熊本市熊本市北区…」「北区熊本市北区…」のような重複を取り除く
    for _ in range(3):
        if a.startswith(city):
            a = a[len(city):]
        elif ward and a.startswith(base):
            a = a[len(base):]
        elif ward and a.startswith(ward):
            a = a[len(ward):]
        else:
            break
    if re.match(r"^.{1,6}郡", a) and not city.endswith("郡"):
        return pref + a
    return pref + city + a


def clean_coord(lat, lng):
    try:
        la, lo = float(lat), float(lng)
    except (TypeError, ValueError):
        return None, None
    if 20 < la < 46.5 and 122 < lo < 154.5:  # 日本の範囲外は使わない
        return round(la, 6), round(lo, 6)
    return None, None


def split_city_ward(pref, city):
    """('横浜市', '中区') のように市と区を分ける。区のない市町村は ward=''。"""
    if pref != "東京都":
        for dc in DESIGNATED_CITIES:
            if city.startswith(dc) and len(city) > len(dc):
                return dc, city[len(dc):]
    return city, ""


# ---------- 変換 ----------

def build(sources, as_of, out_dir):
    pref_cities = defaultdict(lambda: defaultdict(lambda: {"code": "", "wards": defaultdict(list)}))
    seen = set()
    stats = Counter()
    for svc_code, raw in sources:
        if svc_code in EXCLUDE_SERVICE_CODES:
            continue
        reader = csv.DictReader(io.StringIO(decode_csv(raw)))
        missing = [c for c in ("pref", "city", "name", "service", "addr") if COLUMNS[c] not in (reader.fieldnames or [])]
        if missing:
            print(f"  ! {svc_code}: 必要な列がありません {missing}。このファイルは飛ばします。")
            continue
        for row in reader:
            g = lambda k: (row.get(COLUMNS[k]) or "").strip()  # noqa: E731
            pref = nfkc(g("pref"))
            if pref not in PREF_CODE:
                stats["都道府県不明"] += 1
                continue
            service = nfkc(g("service"))
            if service in EXCLUDE_SERVICE_NAMES:
                continue
            city_full = nfkc(g("city"))
            if not city_full:
                stats["市区町村不明"] += 1
                continue
            city, ward = split_city_ward(pref, city_full)
            key = (g("no"), service, g("name"))
            if key in seen:
                stats["重複"] += 1
                continue
            seen.add(key)
            lat, lng = clean_coord(g("lat"), g("lng"))
            if lat is None and g("lat"):
                stats["座標除外"] += 1
            code6 = re.sub(r"\D", "", g("code"))
            code5 = code6[:5] if len(code6) >= 5 else ""
            item = {
                "s": service,
                "n": nfkc(g("name")),
                "k": nfkc(g("kana")),
                "a": clean_addr(g("addr"), pref, city_full),
                "tel": clean_tel(g("tel")),
                "fax": clean_tel(g("fax")),
                "corp": nfkc(g("corp")),
                "days": nfkc(g("days")),
                "url": clean_url(g("url")),
                "lat": lat,
                "lng": lng,
                "w": ward,
            }
            c = pref_cities[pref][city]
            if ward:
                c["code"] = DESIGNATED_CITIES[city]
                c["wards"][ward].append((code5, item))
            else:
                if code5 and (not c["code"] or code5 < c["code"]):
                    c["code"] = code5
                c["wards"][""].append((code5, item))
            stats["件数"] += 1

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    types_all = set()
    pref_index = []
    for pref in PREFS:
        if pref not in pref_cities:
            continue
        pcode = PREF_CODE[pref]
        (out_dir / pcode).mkdir()
        cities = []
        for city, c in pref_cities[pref].items():
            ccode = c["code"] or "x" + hashlib.md5(city.encode()).hexdigest()[:6]
            ward_list = []
            all_items = []
            for ward, entries in c["wards"].items():
                wcode = min((e[0] for e in entries if e[0]), default="")
                for _, it in entries:
                    all_items.append(it)
                if ward:
                    ward_list.append({"code": wcode, "name": ward, "count": len(entries)})
            ward_list.sort(key=lambda w: w["code"] or w["name"])
            stats["座標除外"] += write_city(out_dir / pcode / f"{ccode}.json", all_items, types_all)
            cities.append({
                "code": ccode, "name": city, "count": len(all_items),
                **({"wards": ward_list} if ward_list else {}),
            })
        cities.sort(key=lambda c: c["code"])
        total = sum(c["count"] for c in cities)
        dump(out_dir / pcode / "index.json", {"pref": pref, "code": pcode, "count": total, "cities": cities})
        pref_index.append({"code": pcode, "name": pref, "count": total, "cityCount": len(cities)})

    meta = {
        "source": "厚生労働省 介護サービス情報公表システム オープンデータ",
        "sourceUrl": OPENDATA_PAGE,
        "asOf": as_of,
        "builtAt": time.strftime("%Y-%m-%d"),
        "count": sum(p["count"] for p in pref_index),
        "fields": FIELDS,
        "prefs": pref_index,
    }
    dump(out_dir / "index.json", meta)
    return meta, stats, types_all


def write_city(path, items, types_all):
    items.sort(key=lambda it: (it["s"], it["k"] or it["n"]))
    # 市区町村の中心(中央値)から大きく外れた座標は入力ミスとみなして使わない
    dropped = 0
    pts = [(it["lat"], it["lng"]) for it in items if it["lat"] is not None]
    if len(pts) >= 3:
        mlat = statistics.median(p[0] for p in pts)
        mlng = statistics.median(p[1] for p in pts)
        for it in items:
            if it["lat"] is not None and (abs(it["lat"] - mlat) > COORD_MAX_DEV or abs(it["lng"] - mlng) > COORD_MAX_DEV):
                it["lat"] = it["lng"] = None
                dropped += 1
    types = sorted({it["s"] for it in items})
    types_all.update(types)
    tindex = {t: i for i, t in enumerate(types)}
    rows = []
    for it in items:
        row = [tindex[it["s"]]] + [it[f] if it[f] is not None else "" for f in FIELDS[1:]]
        while row and row[-1] == "":  # 末尾の空欄は省く
            row.pop()
        rows.append(row)
    dump(path, {"types": types, "items": rows})
    return dropped


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="介護保険事業所検索(全国版)のデータを作る")
    ap.add_argument("--csv-dir", help="手元のCSVフォルダから変換する(ダウンロードしない)")
    ap.add_argument("--save-csv", help="ダウンロードしたCSVを保存するフォルダ(任意)")
    ap.add_argument("--as-of", help="データ時点の表記(例: 2026年6月末時点)")
    ap.add_argument("--out", default="data", help="出力フォルダ(既定: data)")
    args = ap.parse_args()

    sources, as_of = load_sources(args)
    print(f"{len(sources)}ファイルを変換しています…")
    meta, stats, types_all = build(sources, as_of, Path(args.out))

    size = sum(p.stat().st_size for p in Path(args.out).rglob("*.json"))
    files = sum(1 for _ in Path(args.out).rglob("*.json"))
    biggest = max(Path(args.out).rglob("*.json"), key=lambda p: p.stat().st_size)
    print("\n=== 完了 ===")
    print(f"データ時点: {meta['asOf']}")
    print(f"事業所: {meta['count']:,}件 / 都道府県: {len(meta['prefs'])} / ファイル: {files:,}個 / 合計 {size / 1e6:.1f}MB")
    print(f"いちばん大きいファイル: {biggest} ({biggest.stat().st_size / 1e6:.2f}MB)")
    for k in ("重複", "座標除外", "都道府県不明", "市区町村不明"):
        if stats[k]:
            print(f"  {k}: {stats[k]:,}件")
    print(f"サービスの種類: {len(types_all)}種類")


if __name__ == "__main__":
    main()
