# Warden documentation site

[Docusaurus](https://docusaurus.io/) app for the kernel. Markdown sources live in **`../docs/`** at the repository root.

## Commands

```bash
npm install   # once
npm start     # local dev server
npm run build # production build → build/
```

Requires **Node ≥ 20**.

## Configuration

- `docusaurus.config.ts` — sets `docs.path` to `../docs` so docs stay beside `website/` at repo root.
