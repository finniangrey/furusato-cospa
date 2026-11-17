"""ふるさと納税の返礼品データを取得し、src/data/furusato.json を更新する。

    python scripts/fetch_furusato.py              # APIキーがあれば実データ、無ければサンプル
    python scripts/fetch_furusato.py --sample     # 強制的にサンプルデータを生成
    python scripts/fetch_furusato.py --dry-run    # 書き込まずに件数だけ確認
    python scripts/fetch_furusato.py --self-test  # 内容量パーサーのテスト

ソース（環境変数が設定されているものだけ使う。.env があれば読み込む）:
  * 楽天市場 商品検索API      … RAKUTEN_APP_ID（必須） / RAKUTEN_AFFILIATE_ID / RAKUTEN_ACCESS_KEY
  * Yahoo!ショッピング 商品検索API v3 … YAHOO_CLIENT_ID（必須） / YAHOO_AFFILIATE_ID（バリューコマース）

設計方針:
  * どのキーも未設定なら、サンプルデータ（5カテゴリ×6件＝30件）を生成するフォールバックモード。
    サンプルも実データと同じパーサー・計算を通すので、表示確認とパーサーの回帰確認を兼ねる。
  * キーがあるのに 0 件しか取れなかった場合は取得障害とみなし、既存データを守るため非 0 終了。
  * 商品名から内容量を抽出できなかった商品は、コスパを計算できないので掲載しない。
  * 外部ライブラリ不要（標準ライブラリのみ）。

注意: 各APIの利用規約・レート制限（REQUEST_INTERVAL）を守ること。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "src" / "data" / "furusato.json"
JST = timezone(timedelta(hours=9))
REQUEST_INTERVAL = 1.1  # 秒。楽天は 1 リクエスト/秒が上限
TIMEOUT = 20
MIN_PRICE = 2000  # 寄付額がこれ未満の商品は対象外（自己負担額 2,000 円以下では意味がない）
# 重さ（g 換算）で 1,000 円あたりこの値を超える商品は、型番や賞味期限を内容量と誤認した異常値とみなして除外する。
# 参考: 米 20kg・28,000 円で約 714g、訳ありみかん 10kg・12,000 円で約 833g。
MAX_GRAMS_PER_1000_YEN = 3000
ERROR_BODY_LIMIT = 1000  # APIエラー時に表示するレスポンスボディの最大文字数
MAX_CONSECUTIVE_API_ERRORS = 3  # HTTP エラーがこの回数続いたら、そのソースの取得を打ち切る

log = logging.getLogger("fetch_furusato")

# カテゴリ slug → 表示名と検索キーワード。slug は src/lib/furusato.ts の CATEGORIES と揃える。
CATEGORIES: dict[str, dict[str, Any]] = {
    "meat": {"label": "肉", "keywords": ["牛肉", "豚肉", "鶏肉", "ハンバーグ"]},
    "seafood": {"label": "魚介", "keywords": ["ホタテ", "いくら", "鮭", "うなぎ", "カニ"]},
    "rice": {"label": "米", "keywords": ["米 10kg", "無洗米", "パックご飯"]},
    "fruit": {"label": "フルーツ", "keywords": ["みかん", "りんご", "シャインマスカット", "いちご"]},
    "daily": {"label": "日用品", "keywords": ["トイレットペーパー", "ティッシュペーパー", "洗剤"]},
}

# ---------------------------------------------------------------------------
# 内容量パーサー
# ---------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?)"
_MULT = r"(?:\s*[×x*]\s*(\d+))?"
_WEIGHT_RE = re.compile(_NUM + r"\s*(kg|キログラム|キロ|g|グラム)(?![a-z])" + _MULT)
_VOLUME_RE = re.compile(_NUM + r"\s*(ml|ミリリットル|cc|l|リットル)(?![a-z])" + _MULT)
_WEIGHT_FACTOR = {"kg": 1000, "キログラム": 1000, "キロ": 1000, "g": 1, "グラム": 1}
_VOLUME_FACTOR = {"ml": 1, "ミリリットル": 1, "cc": 1, "l": 1000, "リットル": 1000}

# 個数の単位。中身そのものを数える単位を優先し、包装の単位は他に無いときだけ使う。
_ITEM_UNITS = ["ロール", "切れ", "個", "玉", "本", "枚", "尾", "匹", "杯", "缶", "食", "房", "切", "粒", "膳", "羽"]
_PACK_UNITS = ["パック", "袋", "箱", "包"]
_COUNT_RE = re.compile(r"(\d+)\s*(" + "|".join(_ITEM_UNITS + _PACK_UNITS) + r")" + _MULT)
_TOTAL_RE = re.compile(r"(?:合計|総量|総重量|計)\s*約?\s*")
_TEIKI_TIMES_RE = re.compile(r"(?:全|年)?\s*(\d+)\s*(?:回|[ヶかカケ]月)")


def normalize(text: str) -> str:
    """全角英数・単位記号（㎏ など）を半角に寄せ、数字の桁区切りカンマを外す。"""
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"(?<=\d),(?=\d{3})", "", text)


def _measure(text: str, pattern: re.Pattern[str], factors: dict[str, int]) -> tuple[Optional[float], bool]:
    """重さ・容量の合計を返す。戻り値は (基本単位での量, 「×N」の掛け算を使ったか)。

    「2kg（500g×4パック）」のように総量と内訳が併記されることが多いので、
    「合計◯kg」の表記があればそれを、無ければ候補のうち最大のものを総量とみなす。
    """
    best: Optional[float] = None
    multiplied = False
    for m in pattern.finditer(text):
        value = float(m.group(1)) * factors[m.group(2)]
        if m.group(3):
            value *= int(m.group(3))
        if _TOTAL_RE.search(text[max(0, m.start() - 8) : m.start()]):
            return value, bool(m.group(3))
        if best is None or value > best:
            best, multiplied = value, bool(m.group(3))
    return best, multiplied


def parse_amount(name: str) -> Optional[dict[str, Any]]:
    """商品名から内容量を抽出する。優先順位は 重さ > 容量 > 個数。

    戻り値: {"kind": "weight"|"volume"|"count", "unit": "g"|"ml"|"個"…, "value": 数値}
    抽出できなければ None。
    """
    text = normalize(name)

    for kind, unit, pattern, factors in (
        ("weight", "g", _WEIGHT_RE, _WEIGHT_FACTOR),
        ("volume", "ml", _VOLUME_RE, _VOLUME_FACTOR),
    ):
        value, multiplied = _measure(text, pattern, factors)
        if value:
            # 定期便で「毎月5kg・全3回」のように掛け算が書かれていない場合は回数を掛ける
            if "定期" in text and not multiplied:
                times = _TEIKI_TIMES_RE.search(text)
                if times and 2 <= int(times.group(1)) <= 12:
                    value *= int(times.group(1))
            return {"kind": kind, "unit": unit, "value": round(value, 1)}

    matches = list(_COUNT_RE.finditer(text))
    chosen = next((m for m in matches if m.group(2) in _ITEM_UNITS), matches[0] if matches else None)
    if chosen:
        value = int(chosen.group(1)) * int(chosen.group(3) or 1)
        if value > 0:
            return {"kind": "count", "unit": chosen.group(2), "value": value}
    return None


_PREF_CITY_RE = re.compile(r"((?:北海道|東京都|(?:京都|大阪)府|[一-龥]{2,3}県)\s*[一-龥ぁ-んァ-ヶー]{1,8}?[市区町村])")


def parse_municipality(*texts: str) -> str:
    for text in texts:
        m = _PREF_CITY_RE.search(unicodedata.normalize("NFKC", text or ""))
        if m:
            return re.sub(r"\s+", "", m.group(1))
    return ""


def is_furusato(*texts: Optional[str]) -> bool:
    """商品名・キャッチコピー・店舗名のいずれかに「ふるさと納税」を含むか。

    「ふるさと納税 牛肉」で検索しても通常の市販品が混ざるので、実データ取得時はこれで弾く。
    半角カナ・空白入りの表記ゆれ（「ふるさと 納税」「ﾌﾙｻﾄ納税」）も拾う。
    """
    for text in texts:
        compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
        if "ふるさと納税" in compact or "フルサト納税" in compact:
            return True
    return False


# ---------------------------------------------------------------------------
# コスパ計算
# ---------------------------------------------------------------------------


def build_item(
    *,
    item_id: str,
    source: str,
    category: str,
    name: str,
    price: int,
    url: str,
    image: str = "",
    shop: str = "",
    municipality: str = "",
    review_average: float = 0.0,
    review_count: int = 0,
    in_stock: bool = True,
    is_sample: bool = False,
) -> Optional[dict[str, Any]]:
    """1 商品を JSON のレコードに整形する。内容量が取れない・価格が不正なら None。"""
    if price < MIN_PRICE:
        return None
    amount = parse_amount(name)
    if not amount:
        return None
    per_yen = amount["value"] / price
    if amount["kind"] == "weight" and per_yen * 1000 > MAX_GRAMS_PER_1000_YEN:
        log.warning(
            "異常値のため除外: 1,000円あたり %.0fg（上限 %dg）｜内容量 %.0fg・寄付額 %d円｜%s｜%s",
            per_yen * 1000,
            MAX_GRAMS_PER_1000_YEN,
            amount["value"],
            price,
            item_id,
            name.strip()[:60],
        )
        return None
    return {
        "id": item_id,
        "source": source,
        "category": category,
        "name": name.strip(),
        "municipality": municipality or parse_municipality(shop, name),
        "shop": shop,
        "price": price,
        "url": url,
        "image": image,
        "review_average": round(float(review_average or 0), 2),
        "review_count": int(review_count or 0),
        "in_stock": bool(in_stock),
        "amount": amount,
        "per_1000": round(per_yen * 1000, 2),  # 1,000円あたりの内容量（amount.unit 単位）
        "per_10000": round(per_yen * 10000, 1),  # 1万円あたりの内容量
        "cospa_score": 1.0,  # finalize() で上書き
        "is_sample": is_sample,
    }


def finalize(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """重複を除き、コスパ指数を付ける。

    コスパ指数 = その商品の「1,000円あたり内容量」÷ 同じカテゴリ・同じ単位の中央値。
    g と ロール のように単位が違う商品を、同じ物差しで並べるための相対値。
    """
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        unique.setdefault(item["id"], item)
    result = list(unique.values())

    groups: dict[tuple[str, str], list[float]] = {}
    for item in result:
        groups.setdefault((item["category"], item["amount"]["unit"]), []).append(item["per_1000"])
    medians = {key: statistics.median(values) for key, values in groups.items()}
    for item in result:
        median = medians[(item["category"], item["amount"]["unit"])]
        item["cospa_score"] = round(item["per_1000"] / median, 2) if median else 1.0

    result.sort(key=lambda i: (-i["cospa_score"], i["id"]))
    return result


# ---------------------------------------------------------------------------
# サンプルデータ（フォールバックモード）
# ---------------------------------------------------------------------------

# (カテゴリ, 検索キーワード, 商品名, 自治体, 寄付額, レビュー平均, レビュー件数, 在庫)
# 商品名は実在の返礼品によくある書式をまねた架空のもの。内容量はあえて書式をばらつかせてある。
SAMPLES: list[tuple[str, str, str, str, int, float, int, bool]] = [
    ("meat", "豚肉 切り落とし", "【ふるさと納税】宮崎県産 豚肉 切り落とし 4kg（500g×8パック）小分け 真空 冷凍", "宮崎県都城市", 10000, 4.62, 12840, True),
    ("meat", "鶏もも肉", "【ふるさと納税】九州産 若鶏 もも肉 3.3kg（300g×11袋）カット済み 小分け", "宮崎県川南町", 10000, 4.55, 8421, True),
    ("meat", "佐賀牛 切り落とし", "【ふるさと納税】佐賀牛 切り落とし 1kg（500g×2パック）A4〜A5等級 すき焼き 牛丼", "佐賀県嬉野市", 20000, 4.71, 3256, True),
    ("meat", "牛タン", "【ふるさと納税】厚切り 牛タン 塩味 900g（300g×3）仙台名物", "宮城県名取市", 17000, 4.48, 5310, True),
    ("meat", "サーロインステーキ", "【ふるさと納税】鹿児島県産 黒毛和牛 サーロインステーキ 200g×3枚 A5等級", "鹿児島県曽於市", 34000, 4.83, 942, True),
    ("meat", "ハンバーグ", "【ふるさと納税】鉄板焼 ハンバーグ デミソース 150g×20個 温めるだけ", "福岡県飯塚市", 12000, 4.67, 21503, True),
    ("seafood", "ホタテ", "【ふるさと納税】オホーツク産 ホタテ 貝柱 1kg 生食用 大粒", "北海道紋別市", 15000, 4.74, 15872, True),
    ("seafood", "いくら", "【ふるさと納税】いくら 醤油漬け 500g（250g×2パック）北海道産 鮭卵", "北海道白糠町", 15000, 4.69, 9920, True),
    ("seafood", "銀鮭", "【ふるさと納税】訳あり 銀鮭 切り身 約2.4kg 甘塩 冷凍 規格外 不揃い", "千葉県銚子市", 11000, 4.41, 6204, True),
    ("seafood", "うなぎ 蒲焼", "【ふるさと納税】鹿児島県産 うなぎ 蒲焼 2尾（計320g）タレ・山椒付き", "鹿児島県大崎町", 14000, 4.58, 4877, True),
    ("seafood", "ズワイガニ", "【ふるさと納税】生 ズワイガニ ポーション 1kg（500g×2）かにしゃぶ", "福井県敦賀市", 25000, 4.36, 2190, False),
    ("seafood", "さば", "【ふるさと納税】無塩 さば フィレ 2.5kg 骨取り 冷凍 業務用", "茨城県神栖市", 8000, 4.33, 3015, True),
    ("rice", "ななつぼし", "【ふるさと納税】令和8年産 ななつぼし 20kg（5kg×4袋）北海道産 精米", "北海道深川市", 28000, 4.72, 7733, True),
    ("rice", "コシヒカリ", "【ふるさと納税】令和8年産 魚沼産 コシヒカリ 5kg 精米", "新潟県南魚沼市", 19000, 4.80, 4120, True),
    ("rice", "はえぬき", "【ふるさと納税】令和8年産 はえぬき 15kg（5kg×3袋）山形県産", "山形県鶴岡市", 24000, 4.63, 5546, True),
    ("rice", "無洗米 ひとめぼれ", "【ふるさと納税】無洗米 ひとめぼれ 5kg×2袋 令和8年産 宮城県産", "宮城県登米市", 18000, 4.59, 3382, True),
    ("rice", "ゆめぴりか 定期便", "【ふるさと納税】【3ヶ月定期便】ゆめぴりか 5kg×3回 北海道産 特A", "北海道東川町", 36000, 4.77, 1264, True),
    ("rice", "パックご飯", "【ふるさと納税】パックご飯 200g×24食 新潟県産こしひかり レンジで2分", "新潟県新発田市", 9000, 4.52, 10418, True),
    ("fruit", "シャインマスカット", "【ふるさと納税】シャインマスカット 2房 約1.2kg 山梨県産 先行予約", "山梨県笛吹市", 15000, 4.44, 6890, True),
    ("fruit", "みかん 訳あり", "【ふるさと納税】訳あり 有田みかん 10kg 家庭用 サイズ混合", "和歌山県有田市", 12000, 4.38, 18244, True),
    ("fruit", "完熟マンゴー", "【ふるさと納税】宮崎県産 完熟マンゴー 2玉 化粧箱入り", "宮崎県西都市", 20000, 4.65, 1530, True),
    ("fruit", "りんご サンふじ", "【ふるさと納税】青森県産 りんご サンふじ 約5kg 家庭用", "青森県弘前市", 11000, 4.47, 7105, True),
    ("fruit", "さくらんぼ 佐藤錦", "【ふるさと納税】さくらんぼ 佐藤錦 500g バラ詰め 山形県産", "山形県東根市", 12000, 4.56, 3948, True),
    ("fruit", "あまおう", "【ふるさと納税】福岡県産 いちご あまおう 280g×4パック", "福岡県八女市", 13000, 4.61, 5822, True),
    ("daily", "トイレットペーパー ダブル", "【ふるさと納税】トイレットペーパー ダブル 12ロール×8パック 再生紙 無香料", "静岡県富士市", 16000, 4.66, 14370, True),
    ("daily", "ティッシュペーパー", "【ふるさと納税】ティッシュペーパー 5箱×12セット 200組 まとめ買い", "静岡県富士宮市", 13000, 4.60, 9015, True),
    ("daily", "食器用洗剤 詰め替え", "【ふるさと納税】食器用洗剤 詰め替え 950ml×6本 大容量", "栃木県真岡市", 10000, 4.49, 2671, True),
    ("daily", "キッチンペーパー", "【ふるさと納税】キッチンペーパー 4ロール×12パック 厚手 吸収", "岐阜県可児市", 11000, 4.43, 1988, True),
    ("daily", "今治タオル", "【ふるさと納税】今治タオル フェイスタオル 4枚セット 綿100％", "愛媛県今治市", 10000, 4.70, 3460, True),
    ("daily", "トイレットペーパー シングル", "【ふるさと納税】トイレットペーパー シングル 64ロール 長巻き 無包装", "愛媛県四国中央市", 8000, 4.54, 6732, True),
]


def generate_samples() -> list[dict[str, Any]]:
    affiliate_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "").strip()
    items = []
    for index, (category, keyword, name, municipality, price, rating, reviews, in_stock) in enumerate(SAMPLES, 1):
        # サンプルは架空の商品なので、リンク先は楽天市場の検索結果にする
        search = "https://search.rakuten.co.jp/search/mall/" + urllib.parse.quote(f"ふるさと納税 {keyword}") + "/"
        url = (
            f"https://hb.afl.rakuten.co.jp/hgc/{affiliate_id}/?pc=" + urllib.parse.quote(search, safe="")
            if affiliate_id
            else search
        )
        item = build_item(
            item_id=f"sample-{index:03d}",
            source="sample",
            category=category,
            name=name,
            price=price,
            url=url,
            shop=f"{municipality}（サンプル）",
            municipality=municipality,
            review_average=rating,
            review_count=reviews,
            in_stock=in_stock,
            is_sample=True,
        )
        if item is None:
            raise RuntimeError(f"サンプルの内容量を解析できません: {name}")
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# API 取得
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """API が HTTP エラー（400/401/403/429 など）を返した。body はレスポンスボディ（エラーメッセージ）。"""

    def __init__(self, status: int, reason: str, body: str) -> None:
        super().__init__(f"HTTP {status} {reason}")
        self.status = status
        self.reason = reason
        self.body = body

    @property
    def is_auth_error(self) -> bool:
        """キーの誤り・権限不足。同じキーで再試行しても直らない。"""
        return self.status in (401, 403)


class FetchIncomplete(Exception):
    """取得が途中で打ち切られた、または一部のリクエストが失敗した。

    この状態のデータで furusato.json を上書きすると、カテゴリごと商品が消えるため、書き込まずに異常終了させる。
    """


def http_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "furusato-cospa/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
        except OSError:
            body = ""
        raise ApiError(exc.code, exc.reason or "", body) from exc


def report_api_error(source: str, keyword: str, exc: ApiError) -> None:
    """HTTP エラーの内容を、ステータスコードとレスポンスボディ付きで出力する（URL にはキーが入るので出さない）。"""
    body = exc.body or "（レスポンスボディなし）"
    try:  # JSON なら 1 行に整形して読みやすくする
        body = json.dumps(json.loads(body), ensure_ascii=False)
    except ValueError:
        pass
    if len(body) > ERROR_BODY_LIMIT:
        body = body[:ERROR_BODY_LIMIT] + "…（以下省略）"
    reason = "" if str(exc.reason) == str(exc.status) else f" {exc.reason}"
    log.error("%s: 「%s」で APIエラー HTTP %d%s", source, keyword, exc.status, reason)
    log.error("%s: レスポンス: %s", source, body)
    if exc.is_auth_error:
        log.error("%s: 認証・権限のエラーです。APIキーの値と、アプリの利用設定を確認してください。以降の取得を中止します", source)


def fetch_rakuten(app_id: str, hits: int) -> list[dict[str, Any]]:
    """楽天市場 商品検索API。カテゴリごとのキーワードを「ふるさと納税 ◯◯」で検索する。"""
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY", "").strip()
    endpoint = os.environ.get("RAKUTEN_API_ENDPOINT", "").strip() or (
        "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
        if access_key
        else "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
    )
    affiliate_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "").strip()
    items: list[dict[str, Any]] = []
    skipped = 0
    not_furusato = 0  # 「ふるさと納税」表記が無く、一般商品とみなして除外した件数
    api_errors = 0  # 連続した HTTP エラーの回数
    failed: list[str] = []  # 取得に失敗したキーワード（1 件でもあれば不完全なデータとして扱う）
    for category, meta in CATEGORIES.items():
        for keyword in meta["keywords"]:
            try:
                data = http_get_json(
                    endpoint,
                    {
                        "applicationId": app_id,
                        "accessKey": access_key,
                        "affiliateId": affiliate_id,
                        "keyword": f"ふるさと納税 {keyword}",
                        "hits": hits,
                        "sort": "-reviewCount",
                        "minPrice": MIN_PRICE,
                        "availability": 0,  # 在庫切れも取得し、サイト側で「在庫なし」と表示する
                        "formatVersion": 2,
                    },
                )
            except ApiError as exc:
                report_api_error("楽天", keyword, exc)
                failed.append(keyword)
                api_errors += 1
                if exc.is_auth_error or api_errors >= MAX_CONSECUTIVE_API_ERRORS:
                    if not exc.is_auth_error:
                        log.error("楽天: APIエラーが %d 回続いたため、以降の取得を中止します", api_errors)
                    raise FetchIncomplete(f"楽天: 取得を途中で打ち切りました（HTTP {exc.status}・「{keyword}」）") from exc
                time.sleep(REQUEST_INTERVAL)
                continue
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                log.error("楽天: 「%s」の取得に失敗: %s", keyword, exc)
                failed.append(keyword)
                time.sleep(REQUEST_INTERVAL)
                continue
            api_errors = 0
            for raw in data.get("Items", []):
                raw = raw.get("Item", raw)  # formatVersion=1 の入れ子にも対応
                name = raw.get("itemName", "")
                if not is_furusato(name, raw.get("catchcopy"), raw.get("shopName")):
                    not_furusato += 1
                    continue
                images = raw.get("mediumImageUrls") or []
                image = images[0] if images else ""
                if isinstance(image, dict):
                    image = image.get("imageUrl", "")
                item = build_item(
                    item_id=f"rakuten-{raw.get('itemCode', '')}",
                    source="rakuten",
                    category=category,
                    name=name,
                    price=int(raw.get("itemPrice") or 0),
                    url=raw.get("affiliateUrl") or raw.get("itemUrl", ""),
                    image=image,
                    shop=raw.get("shopName", ""),
                    review_average=raw.get("reviewAverage") or 0,
                    review_count=raw.get("reviewCount") or 0,
                    in_stock=int(raw.get("availability", 1)) == 1,
                )
                if item and item["url"]:
                    items.append(item)
                else:
                    skipped += 1
            time.sleep(REQUEST_INTERVAL)
    log.info(
        "楽天: %d 件取得（「ふるさと納税」表記なしで除外 %d 件／内容量を抽出できない・異常値などで除外 %d 件）",
        len(items),
        not_furusato,
        skipped,
    )
    if failed:
        raise FetchIncomplete(f"楽天: {len(failed)} 件のキーワードで取得に失敗しました（{', '.join(failed)}）")
    return items


_YAHOO_IMAGE_PATH_RE = re.compile(r"^(https?://[^/]+\.yimg\.jp/i/)[a-z]/")


def pick_yahoo_image(raw: dict[str, Any]) -> str:
    """Yahoo の商品画像 URL（600×600px）。

    商品検索API v3 には画像サイズの指定パラメータが無く、image.medium は 146×146px
    （.../i/g/<店舗>_<商品>）、small は 76×76px（.../i/c/...）が返る。
    画像サーバー上の同じ商品は、パスの /i/g/ を /i/l/ に変えると 600×600px で取得できる
    （実際の取得で HTTP 200・600×600px を確認済み）ので、そちらへ変換して返す。
    exImage があればそれを優先し、/i/<英字>/ 形式でなければ変換せずそのまま返す。
    """
    ex = raw.get("exImage") or {}
    image = raw.get("image") or {}
    url = ex.get("url") or image.get("medium") or image.get("small") or ""
    return _YAHOO_IMAGE_PATH_RE.sub(r"\1l/", url)


def fetch_yahoo(client_id: str, hits: int) -> list[dict[str, Any]]:
    """Yahoo!ショッピング 商品検索API v3。"""
    endpoint = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
    affiliate_id = os.environ.get("YAHOO_AFFILIATE_ID", "").strip()
    items: list[dict[str, Any]] = []
    skipped = 0
    not_furusato = 0  # 「ふるさと納税」表記が無く、一般商品とみなして除外した件数
    api_errors = 0  # 連続した HTTP エラーの回数
    failed: list[str] = []  # 取得に失敗したキーワード（1 件でもあれば不完全なデータとして扱う）
    for category, meta in CATEGORIES.items():
        for keyword in meta["keywords"]:
            try:
                data = http_get_json(
                    endpoint,
                    {
                        "appid": client_id,
                        "query": f"ふるさと納税 {keyword}",
                        "results": min(hits, 50),
                        "sort": "-review_count",
                        "price_from": MIN_PRICE,
                        "affiliate_type": "vc" if affiliate_id else None,
                        "affiliate_id": affiliate_id,
                    },
                )
            except ApiError as exc:
                report_api_error("Yahoo", keyword, exc)
                failed.append(keyword)
                api_errors += 1
                if exc.is_auth_error or api_errors >= MAX_CONSECUTIVE_API_ERRORS:
                    if not exc.is_auth_error:
                        log.error("Yahoo: APIエラーが %d 回続いたため、以降の取得を中止します", api_errors)
                    raise FetchIncomplete(f"Yahoo: 取得を途中で打ち切りました（HTTP {exc.status}・「{keyword}」）") from exc
                time.sleep(REQUEST_INTERVAL)
                continue
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                log.error("Yahoo: 「%s」の取得に失敗: %s", keyword, exc)
                failed.append(keyword)
                time.sleep(REQUEST_INTERVAL)
                continue
            api_errors = 0
            for raw in data.get("hits", []):
                name = raw.get("name", "")
                seller = raw.get("seller") or {}
                if not is_furusato(name, raw.get("headLine"), seller.get("name")):
                    not_furusato += 1
                    continue
                review = raw.get("review") or {}
                item = build_item(
                    item_id=f"yahoo-{raw.get('code', '')}",
                    source="yahoo",
                    category=category,
                    name=name,
                    price=int(raw.get("price") or 0),
                    url=raw.get("url", ""),
                    image=pick_yahoo_image(raw),
                    shop=seller.get("name", ""),
                    review_average=review.get("rate") or 0,
                    review_count=review.get("count") or 0,
                    in_stock=bool(raw.get("inStock", True)),
                )
                if item and item["url"]:
                    items.append(item)
                else:
                    skipped += 1
            time.sleep(REQUEST_INTERVAL)
    log.info(
        "Yahoo: %d 件取得（「ふるさと納税」表記なしで除外 %d 件／内容量を抽出できない・異常値などで除外 %d 件）",
        len(items),
        not_furusato,
        skipped,
    )
    if failed:
        raise FetchIncomplete(f"Yahoo: {len(failed)} 件のキーワードで取得に失敗しました（{', '.join(failed)}）")
    return items


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def is_fresh(dataset: Any, today: str, mode: str, sources: list[str]) -> bool:
    """dataset が「今日（JST）に、同じ取得元・同じモードで」作られたものか。

    取得元やモードが違う場合（サンプル → 実データ、楽天キーを追加した、など）は
    今日の日付でも作り直す必要があるので False を返す。
    """
    if not isinstance(dataset, dict):
        return False
    return (
        str(dataset.get("generated_at", ""))[:10] == today
        and dataset.get("mode") == mode
        and dataset.get("sources") == sources
        and bool(dataset.get("items"))
    )


def load_dotenv() -> None:
    """ローカル実行用。.env の値を、未設定の環境変数にだけ読み込む。"""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def self_test() -> int:
    cases: list[tuple[str, Optional[tuple[str, float]]]] = [
        ("豚肉 切り落とし 4kg（500g×8パック）", ("g", 4000)),
        ("サーロインステーキ 200g×3枚", ("g", 600)),
        ("ハンバーグ １５０ｇ×２０個", ("g", 3000)),
        ("うなぎ 蒲焼 2尾（計320g）", ("g", 320)),
        ("シャインマスカット 2房 約1.2kg", ("g", 1200)),
        ("お米 ５㎏ 精米", ("g", 5000)),
        ("牛肉 1,200g 大容量", ("g", 1200)),
        ("【3ヶ月定期便】ゆめぴりか 5kg×3回", ("g", 15000)),
        ("【定期便】毎月お届け 無洗米 5kg 全6回", ("g", 30000)),
        ("内容量 1.5キロ", ("g", 1500)),
        ("食器用洗剤 詰め替え 950ml×6本", ("ml", 5700)),
        ("天然水 2L×9本", ("ml", 18000)),
        ("トイレットペーパー 12ロール×8パック", ("ロール", 96)),
        ("ティッシュペーパー 5箱×12セット", ("箱", 60)),
        ("完熟マンゴー 2玉 化粧箱入り", ("玉", 2)),
        ("フェイスタオル 4枚セット", ("枚", 4)),
        ("感謝状のみ 返礼品なし", None),
    ]
    failed = 0
    for name, expected in cases:
        got = parse_amount(name)
        actual = (got["unit"], got["value"]) if got else None
        ok = actual == expected
        failed += not ok
        print(f"{'OK ' if ok else 'NG '} {name} -> {actual}" + ("" if ok else f"  (期待値 {expected})"))
    # 外れ値ガード: (商品名, 寄付額, 掲載されるか)
    guard_cases: list[tuple[str, int, bool]] = [
        ("お米 30kg（10kg×3袋）", 10000, True),  # 1,000円あたり 3,000g ちょうどは通す
        ("お米 20kg 精米", 28000, True),
        ("豚肉 型番 a-50kg 切り落とし", 10000, False),  # 型番を内容量と誤認 → 5,000g/1,000円
        ("ハンバーグ 賞味期限 365000g", 12000, False),
        ("トイレットペーパー 96ロール", 3000, True),  # 個数系は対象外
        ("天然水 2L×24本", 10000, True),  # 容量系は対象外
    ]
    for name, price, expected in guard_cases:
        got = build_item(item_id="test", source="sample", category="meat", name=name, price=price, url="https://example.com/")
        ok = (got is not None) == expected
        failed += not ok
        print(f"{'OK ' if ok else 'NG '} 外れ値ガード: {name}（{price}円） -> {'掲載' if got else '除外'}")

    # ふるさと納税フィルタ: ((商品名, キャッチコピー, 店舗名), 返礼品とみなすか)
    furusato_cases: list[tuple[tuple[Optional[str], Optional[str], Optional[str]], bool]] = [
        (("【ふるさと納税】豚肉 切り落とし 4kg", "", "宮崎県都城市"), True),
        (("豚肉 切り落とし 4kg", "ふるさと納税 人気の返礼品", "精肉店"), True),
        (("豚肉 切り落とし 4kg", None, "ふるさと納税　宮崎県都城市"), True),
        (("【ふるさと 納税】ホタテ 1kg", "", ""), True),
        (("国産 豚肉 切り落とし 4kg 業務用 送料無料", "お買い得", "肉の卸問屋"), False),
        (("ふるさとの味 みかん 10kg", None, None), False),
    ]
    for texts, expected in furusato_cases:
        ok = is_furusato(*texts) == expected
        failed += not ok
        print(f"{'OK ' if ok else 'NG '} ふるさと納税フィルタ: {texts} -> {'返礼品' if is_furusato(*texts) else '除外'}")

    # Yahoo の画像 URL: exImage > image.medium > image.small
    image_cases: list[tuple[dict[str, Any], str]] = [
        ({"exImage": {"url": "https://i/600.jpg"}, "image": {"medium": "https://i/146.jpg"}}, "https://i/600.jpg"),
        (  # exImage が無ければ medium（/i/g/ = 146px）を /i/l/（600px）へ変換
            {"exImage": None, "image": {"medium": "https://item-shopping.c.yimg.jp/i/g/shop_item01", "small": "https://item-shopping.c.yimg.jp/i/c/shop_item01"}},
            "https://item-shopping.c.yimg.jp/i/l/shop_item01",
        ),
        ({"image": {"small": "https://item-shopping.c.yimg.jp/i/c/shop_item01"}}, "https://item-shopping.c.yimg.jp/i/l/shop_item01"),
        ({"exImage": {"url": "https://item-shopping.c.yimg.jp/i/n/shop_item01"}}, "https://item-shopping.c.yimg.jp/i/l/shop_item01"),
        ({"image": {"medium": "https://item-shopping.c.yimg.jp/i/l/shop_item01"}}, "https://item-shopping.c.yimg.jp/i/l/shop_item01"),
        ({"image": {"medium": "https://example.com/a/g/x.jpg"}}, "https://example.com/a/g/x.jpg"),  # Yahoo の画像サーバー以外は触らない
        ({"exImage": None, "image": None}, ""),
        ({}, ""),
    ]
    for raw, expected in image_cases:
        ok = pick_yahoo_image(raw) == expected
        failed += not ok
        print(f"{'OK ' if ok else 'NG '} Yahoo画像URL: {raw} -> {pick_yahoo_image(raw)!r}")

    # 本日更新済みガード: (dataset, モード, 取得元, 今日と同じ日付なら True になるか)
    base = {"generated_at": "2026-11-18T04:20:00+09:00", "mode": "live", "sources": ["yahoo"], "items": [{"id": "x"}]}
    fresh_cases: list[tuple[str, Any, str, list[str], bool]] = [
        ("今日・同じ条件 → スキップ", base, "live", ["yahoo"], True),
        ("昨日のデータ → 取得する", {**base, "generated_at": "2026-11-17T23:59:00+09:00"}, "live", ["yahoo"], False),
        ("サンプルデータ → 実データを取得する", {**base, "mode": "sample", "sources": ["sample"]}, "live", ["yahoo"], False),
        ("楽天キーを追加 → 取得する", base, "live", ["rakuten", "yahoo"], False),
        ("商品が空 → 取得する", {**base, "items": []}, "live", ["yahoo"], False),
        ("壊れたデータ → 取得する", None, "live", ["yahoo"], False),
    ]
    for label, dataset, mode, sources, expected in fresh_cases:
        ok = is_fresh(dataset, "2026-11-18", mode, sources) == expected
        failed += not ok
        print(f"{'OK ' if ok else 'NG '} 本日更新済みガード: {label}")

    total = len(cases) + len(guard_cases) + len(furusato_cases) + len(image_cases) + len(fresh_cases)
    print(f"{total - failed}/{total} passed")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", action="store_true", help="APIキーがあってもサンプルデータを生成する")
    parser.add_argument("--dry-run", action="store_true", help="ファイルに書き込まない")
    parser.add_argument("--self-test", action="store_true", help="内容量パーサーのテストを実行する")
    parser.add_argument("--force", action="store_true", help="本日更新済みでも、APIを叩いて取得し直す")
    parser.add_argument("--hits", type=int, default=30, help="1 キーワードあたりの取得件数（最大 30）")
    args = parser.parse_args()

    # Windows のコンソール（cp932）でも日本語ログが化けないようにする
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    # ログは標準出力に出す（GitHub Actions のログやリダイレクトで、エラー内容を 1 本の流れで追えるように）
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    if args.self_test:
        return self_test()

    load_dotenv()
    rakuten_id = os.environ.get("RAKUTEN_APP_ID", "").strip()
    yahoo_id = os.environ.get("YAHOO_CLIENT_ID", "").strip()
    hits = max(1, min(args.hits, 30))

    sources: list[str] = []
    items: list[dict[str, Any]] = []
    if args.sample or not (rakuten_id or yahoo_id):
        if not args.sample:
            log.info("RAKUTEN_APP_ID / YAHOO_CLIENT_ID が未設定のため、サンプルデータを生成します")
        mode = "sample"
        sources = ["sample"]
        items = generate_samples()
    else:
        mode = "live"
        planned = (["rakuten"] if rakuten_id else []) + (["yahoo"] if yahoo_id else [])
        # GitHub Actions は 1 日 2 回起動する（片方がスキップされても更新できるように）。
        # 1 回目が成功していれば、2 回目は API を一切叩かずに正常終了して、呼び出し上限を節約する。
        if not args.force and not args.dry_run and OUTPUT.exists():
            try:
                existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = None
            today = datetime.now(JST).strftime("%Y-%m-%d")
            if is_fresh(existing, today, mode, planned):
                log.info("%s は本日（%s・JST）すでに更新済みです。APIは呼ばずに終了します（強制更新は --force）", OUTPUT.relative_to(ROOT), today)
                return 0
        try:
            if rakuten_id:
                sources.append("rakuten")
                items += fetch_rakuten(rakuten_id, hits)
            if yahoo_id:
                sources.append("yahoo")
                items += fetch_yahoo(yahoo_id, hits)
        except FetchIncomplete as exc:
            log.error("%s", exc)
            log.error("取得が不完全なため %s は更新しません（既存データを保持して異常終了します）", OUTPUT.relative_to(ROOT))
            return 1
        if not items:
            log.error("APIから 1 件も取得できませんでした。既存データは変更しません")
            return 1

    items = finalize(items)
    dataset = {
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "mode": mode,
        "sources": sources,
        "count": len(items),
        "items": items,
    }
    by_category = {slug: sum(1 for i in items if i["category"] == slug) for slug in CATEGORIES}
    log.info("合計 %d 件（%s） %s", len(items), mode, by_category)

    if args.dry_run:
        log.info("--dry-run のため書き込みません")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("%s を更新しました", OUTPUT.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
