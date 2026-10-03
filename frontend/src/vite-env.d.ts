/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" → serve every API call from src/mock (no backend needed). */
  readonly VITE_MOCK?: string;
  /** Optional absolute backend origin, e.g. "http://127.0.0.1:8765" (default: same origin / Vite proxy). */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
