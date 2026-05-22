# Seed Artifact Policy

The committed `data/official_cache_seed_2026-05-14.zip` remains the deterministic
offline bootstrap seed. New seed zips generated during refresh or recovery runs
must not drift into Git automatically.

Rules:

- Runtime seed output belongs in R2, not as routine Git churn.
- Git should keep a small manifest or checksum when a seed is intentionally
  promoted.
- A new committed seed zip requires an explicit `git add -f` and review note.
- CI and local scripts must resolve seed zips by the date in the filename, never
  by filesystem modification time.

The `.gitignore` rule blocks accidental future `official_cache_seed_*.zip` and
`official_cache_seed_*.sha256` additions while preserving the current bootstrap
artifact.
