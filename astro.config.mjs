// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';

// サイトの URL。本番ドメインはビルド時の環境変数 SITE_URL で指定する（Cloudflare Pages の環境変数に設定）。
// canonical・OGP・サイトマップ・robots.txt がすべてこの値を参照する。
const site = (process.env.SITE_URL ?? '').trim().replace(/\/+$/, '') || 'https://furusato-cospa.pages.dev';

// Cloudflare Pages: Build command `npm run build` / Output directory `dist`
export default defineConfig({
  site,
  output: 'static',
  trailingSlash: 'always',
  build: { format: 'directory' },
  // 並び替えページ（/rating/・/reviews/）は canonical が既定の並び順を指すので、サイトマップに載せない
  integrations: [sitemap({ filter: (page) => !/\/(rating|reviews)\/$/.test(page) })],
  vite: {
    plugins: [tailwindcss()],
  },
});
