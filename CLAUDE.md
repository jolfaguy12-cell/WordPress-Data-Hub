# Claude Code Rules — Behdashtik Data Hub

## Efficiency

- **Caveman/concise mode**: short responses, no preamble, no trailing summary.
- **Targeted reads only**: read specific files needed for the task. Do not scan the full repo.
- **Use Graphify output first**: check `graphify-out/` for graph/manifest before reading source files. Helper binary: `~/.local/bin/graphify`. Never rebuild unless the user explicitly asks.
- **Update graph** only after significant architecture or code changes (run `graphify` from project root).

## Browser / UI

- Use browser-harness **only** when the user explicitly asks for UI/browser verification.
- Browser-harness must run headless (server-safe). Never use it for code reading or exploration.
- Prefer static inspection first; browser-harness only for final UI behaviour checks.

## Scope Guards

- Work only in `/root/wordpress-data-hub/`. Never touch `/root/behdashtik-hub-main/`.
- Do not touch production WordPress, production cron, or production DB writes.
- Never commit `.env` files or real secrets.
- `BDSK_ALLOW_WP_PHP_EXPORT=1` is required before running any WP/PHP row-by-row export.

## Reports

- Final reports: ≤ 5 bullets. No headers, no prose sections.
