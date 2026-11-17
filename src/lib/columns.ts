import { getCollection, type CollectionEntry } from 'astro:content';

export type Column = CollectionEntry<'columns'>;

export const COLUMN_CATEGORY_LABEL: Record<Column['data']['category'], string> = {
  rule: '制度・手続き',
  meat: '肉',
  seafood: '魚介',
  rice: '米',
  fruit: 'フルーツ',
  daily: '日用品',
};

/** 公開日の新しい順 */
export async function getColumns(): Promise<Column[]> {
  const all = await getCollection('columns');
  return all.sort((a, b) => b.data.publishedAt.valueOf() - a.data.publishedAt.valueOf());
}

export const columnPath = (slug: string) => `/columns/${slug}/`;

export const isoDate = (d: Date) => d.toISOString().slice(0, 10);
export const jpDate = (d: Date) => `${d.getUTCFullYear()}年${d.getUTCMonth() + 1}月${d.getUTCDate()}日`;
