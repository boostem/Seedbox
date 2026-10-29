/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Optional absolute backend origin, e.g. "http://127.0.0.1:8766" (default: same origin / Vite proxy). */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
