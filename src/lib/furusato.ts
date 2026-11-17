/**
 * 返礼品データ（src/data/furusato.json）の読み込みと、分類・並び替え・表示用の整形。
 * JSON は scripts/fetch_furusato.py が生成する。項目を変えるときは両方を揃えること。
 */
import raw from '../data/furusato.json';

export const SITE = {
  name: 'ふるさと納税コスパ比較',
  tagline: '返礼品を「1,000円あたりの内容量」で比べるデータベース',
  description:
    'ふるさと納税の返礼品を、寄付額1,000円あたり・1万円あたりの内容量で比較。肉・魚介・米・フルーツ・日用品のコスパランキングを毎日更新しています。',
} as const;

export type CategorySlug = 'meat' | 'seafood' | 'rice' | 'fruit' | 'daily';
export type AmountKind = 'weight' | 'volume' | 'count';
export type Source = 'rakuten' | 'yahoo' | 'sample';

export interface Item {
  id: string;
  source: Source;
  category: CategorySlug;
  name: string;
  municipality: string;
  shop: string;
  /** 寄付額（円） */
  price: number;
  /** アフィリエイトリンク */
  url: string;
  image: string;
  review_average: number;
  review_count: number;
  in_stock: boolean;
  /** 内容量。weight は g、volume は ml、count は unit の数 */
  amount: { kind: AmountKind; unit: string; value: number };
  per_1000: number;
  per_10000: number;
  /** 同じカテゴリ・同じ単位の中央値に対する倍率 */
  cospa_score: number;
  is_sample: boolean;
}

interface Dataset {
  generated_at: string;
  mode: 'sample' | 'live';
  sources: Source[];
  count: number;
  items: Item[];
}

export const dataset = raw as Dataset;
export const items: Item[] = dataset.items;
export const isSampleMode = dataset.mode === 'sample';

/** 「2026年11月17日」形式のデータ更新日（JST） */
export const updatedLabel = new Intl.DateTimeFormat('ja-JP', {
  timeZone: 'Asia/Tokyo',
  year: 'numeric',
  month: 'long',
  day: 'numeric',
}).format(new Date(dataset.generated_at));

// ---------------------------------------------------------------------------
// カテゴリ・金額帯・並び順
// ---------------------------------------------------------------------------

export const CATEGORIES: { slug: CategorySlug; label: string; emoji: string; lead: string }[] = [
  { slug: 'meat', label: '肉', emoji: '🥩', lead: '牛肉・豚肉・鶏肉・ハンバーグなど。切り落としや小分けパックは内容量が多くなりがちです。' },
  { slug: 'seafood', label: '魚介', emoji: '🐟', lead: 'ホタテ・いくら・鮭・うなぎ・カニなど。訳あり品や切り身は単価が下がります。' },
  { slug: 'rice', label: '米', emoji: '🌾', lead: '精米・無洗米・パックご飯・定期便。定期便は全回分の合計量で比較しています。' },
  { slug: 'fruit', label: 'フルーツ', emoji: '🍊', lead: 'みかん・りんご・ぶどう・いちごなど。家庭用・訳あり品は量が多い傾向です。' },
  { slug: 'daily', label: '日用品', emoji: '🧻', lead: 'トイレットペーパー・ティッシュ・洗剤・タオルなど。ロール数・箱数・容量で比較しています。' },
];

/** min < 寄付額 <= max の商品がその金額帯に入る */
export const PRICE_BANDS = [
  { slug: 'under-10000', label: '1万円以下', min: 0, max: 10000 },
  { slug: '10000-20000', label: '1〜2万円', min: 10000, max: 20000 },
  { slug: '20000-30000', label: '2〜3万円', min: 20000, max: 30000 },
  { slug: 'over-30000', label: '3万円以上', min: 30000, max: Infinity },
] as const;
export type PriceBand = (typeof PRICE_BANDS)[number];

export type SortKey = 'cospa' | 'rating' | 'reviews';
/** slug が空の並び順が既定（一覧ページのルート URL） */
export const SORTS: { key: SortKey; slug: string; label: string }[] = [
  { key: 'cospa', slug: '', label: 'コスパ順（単価）' },
  { key: 'rating', slug: 'rating', label: 'レビュー高評価順' },
  { key: 'reviews', slug: 'reviews', label: 'レビュー件数順' },
];

export const categoryOf = (slug: string) => CATEGORIES.find((c) => c.slug === slug)!;
export const categoryPath = (slug: string) => `/category/${slug}/`;
export const bandPath = (slug: string) => `/price/${slug}/`;
export const inBand = (item: Item, band: PriceBand) => item.price > band.min && item.price <= band.max;
export const itemsInCategory = (slug: string) => items.filter((i) => i.category === slug);
export const itemsInBand = (band: PriceBand) => items.filter((i) => inBand(i, band));

const COMPARATORS: Record<SortKey, (a: Item, b: Item) => number> = {
  cospa: (a, b) => b.cospa_score - a.cospa_score || b.review_count - a.review_count,
  rating: (a, b) => b.review_average - a.review_average || b.review_count - a.review_count,
  reviews: (a, b) => b.review_count - a.review_count || b.review_average - a.review_average,
};

/** 在庫ありを先に、その中を指定の並び順で並べる */
export function sortItems(list: Item[], key: SortKey): Item[] {
  return [...list].sort((a, b) => Number(b.in_stock) - Number(a.in_stock) || COMPARATORS[key](a, b));
}

/** 総合ランキング。在庫ありの商品をコスパ指数の高い順に */
export const overallRanking = (limit: number) => sortItems(items.filter((i) => i.in_stock), 'cospa').slice(0, limit);

// ---------------------------------------------------------------------------
// 表示用の整形
// ---------------------------------------------------------------------------

const trim = (n: number, digits: number) => Number(n.toFixed(digits)).toLocaleString('ja-JP');

export const formatYen = (n: number) => `${Math.round(n).toLocaleString('ja-JP')}円`;

/** 内容量を読みやすい単位で表す。1,000g 以上は kg、1,000ml 以上は L。 */
export function formatAmount(value: number, unit: string): string {
  if (unit === 'g') return value >= 1000 ? `${trim(value / 1000, 2)}kg` : `${trim(value, 0)}g`;
  if (unit === 'ml') return value >= 1000 ? `${trim(value / 1000, 2)}L` : `${trim(value, 0)}ml`;
  return `${trim(value, value < 10 ? 2 : 1)}${unit}`;
}

/** グラム単価など。重さ・容量は 100 あたり、個数は 1 つあたりの寄付額 */
export function unitPrice(item: Item): { label: string; yen: number } {
  const { kind, unit, value } = item.amount;
  if (kind === 'count') return { label: `1${unit}あたり`, yen: item.price / value };
  return { label: `100${unit}あたり`, yen: (item.price / value) * 100 };
}

export const SOURCE_LABEL: Record<Source, string> = {
  rakuten: '楽天市場',
  yahoo: 'Yahoo!ショッピング',
  sample: '楽天市場',
};
