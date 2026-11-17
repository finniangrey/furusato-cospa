// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';

// Cloudflare Pages: Build command `npm run build` / Output directory `dist`
// 本番ドメインが決まったら site と public/robots.txt の Sitemap を書き換える。
export default defineConfig({
  site: 'https://furusato-cospa.pages.dev',
  output: 'static',
  trailingSlash: 'always',
  build: { format: 'directory' },
  integrations: [sitemap()],
  vite: {
    plugins: [tailwindcss()],
  },
});
