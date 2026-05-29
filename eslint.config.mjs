import js from "@eslint/js";
import globals from "globals";
import prettier from "eslint-config-prettier";

// Flat config. ESLint is the CI lint gate; Prettier handles formatting and is
// intentionally opt-in (not a blocking --check), mirroring the Python side
// where `ruff check` gates but `ruff format` is not enforced repo-wide.
export default [
  {
    ignores: [
      "node_modules/**",
      ".tmp/**",
      "test-results/**",
      "playwright-report/**",
      "cloudflare/seed/**",
      "cloudflare/python_modules/**",
      // Python virtualenvs may vendor stray .js files (e.g. urllib3's
      // emscripten worker); never lint anything under one.
      ".venv*/**",
      "**/site-packages/**",
      // TypeScript e2e specs are out of scope here; linting them needs
      // typescript-eslint, which can be added in a follow-up.
      "**/*.spec.ts",
    ],
  },
  js.configs.recommended,
  {
    // Browser app scripts (dual CommonJS + global `StockScanner*` pattern,
    // loaded via <script defer> and consumed by app.js through `_mod`).
    files: ["frontend/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "commonjs",
      globals: { ...globals.browser, ...globals.commonjs, globalThis: "readonly" },
    },
    rules: {
      "no-unused-vars": ["error", { args: "none", varsIgnorePattern: "^_" }],
    },
  },
  {
    // Cloudflare Pages Functions: ES modules on the Workers runtime.
    files: ["frontend/functions/**/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { ...globals.browser },
    },
  },
  {
    // Node tooling (CommonJS): playwright config + e2e harness.
    files: ["*.js", "*.cjs", "tests/**/*.cjs"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "commonjs",
      globals: { ...globals.node },
    },
  },
  prettier,
];
