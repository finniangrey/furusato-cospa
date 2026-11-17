import type { APIRoute } from 'astro';

/** robots.txt。Sitemap の URL は astro.config.mjs の site（環境変数 SITE_URL）から組み立てる */
export const GET: APIRoute = ({ site }) =>
  new Response(`User-agent: *\nAllow: /\n\nSitemap: ${new URL('/sitemap-index.xml', site).href}\n`, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
