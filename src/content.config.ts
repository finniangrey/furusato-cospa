import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { z } from 'astro/zod';

/**
 * 特集・コラム（/columns/）の記事。
 * src/content/columns/<slug>.mdx を追加すると、一覧と記事ページ・サイトマップが自動生成される。
 * 本文では <RankingEmbed category="rice" limit={3} /> を使うと、最新の返礼品データが埋め込まれる。
 */
const columns = defineCollection({
  loader: glob({ pattern: '*.mdx', base: './src/content/columns' }),
  schema: z.object({
    title: z.string(),
    /** メタディスクリプション・一覧カード用の要約（120 字前後） */
    description: z.string(),
    publishedAt: z.coerce.date(),
    updatedAt: z.coerce.date(),
    /** 記事の分類。rule は制度・手続き、他は src/lib/furusato.ts の CategorySlug と同じ */
    category: z.enum(['rule', 'meat', 'seafood', 'rice', 'fruit', 'daily']),
    tags: z.array(z.string()).default([]),
    /** 記事末尾の「関連ランキング」に出すカテゴリ。省略すると category（rule 以外）だけを出す */
    related: z.array(z.enum(['meat', 'seafood', 'rice', 'fruit', 'daily'])).optional(),
  }),
});

export const collections = { columns };
