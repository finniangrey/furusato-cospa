/// <reference types="astro/client" />

interface ImportMetaEnv {
  /** GA4 の測定 ID（例: G-XXXXXXXXXX）。未設定・空文字なら計測タグを出力しない */
  readonly PUBLIC_GA_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
