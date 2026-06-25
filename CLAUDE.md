# Claude Code Rules — Behdashtik Data Hub

## Efficiency

- **Concise mode always**: short responses, no preamble, no trailing summary, no unrelated recap.
- **Targeted reads only**: read specific files needed for the task. Do not scan the full repo blindly.
- **Final reports**: ≤ 5 bullets. No headers, no prose sections.

## Graphify (code graph)

- **Binary**: `~/.local/bin/graphify` (v0.8.46). Confirmed working.
- **Latest output**: `graphify-out/GRAPH_REPORT.md`, `graphify-out/graph.json`, `graphify-out/manifest.json` (top-level, always current). Date-stamped snapshots: `graphify-out/YYYY-MM-DD/`.
- **Read GRAPH_REPORT.md first** before reading source files — 514 nodes, 988 edges as of 2026-06-25.
- **Update command**: `graphify update .` from project root — no API/LLM cost, fast. Run only after significant architecture or code changes.
- **Never run full rebuild** unless the user explicitly asks. `graphify update .` is sufficient for code changes.
- `graphify-out/` is gitignored — do not commit graph output.

## Caveman

- Not a separate tool or binary. "Caveman mode" = concise/minimal responses with no fluff. Apply by default.

## Browser / UI

- **Playwright not installed** on this server. No Chromium present.
- Use browser automation **only** when the user explicitly asks and confirms headless/server setup is acceptable.
- Prefer static code inspection; never use browser for code reading or broad exploration.

## Scope Guards

- Work only in `/root/wordpress-data-hub/`. Never touch `/root/behdashtik-hub-main/`.
- Do not touch production WordPress, production cron, or production DB writes.
- Never commit `.env` files or real secrets.
- `BDSK_ALLOW_WP_PHP_EXPORT=1` required before any WP/PHP row-by-row export.
