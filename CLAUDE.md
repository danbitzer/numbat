# Numbat — working conventions

## Changelog
Every PR that changes the add-on (code, blueprint, dashboard, docs shipped in
the image) must add an entry to `numbat/CHANGELOG.md` under an `## Unreleased`
section at the top (create it if absent). Reference the issue/PR number where
one exists. At release time the `## Unreleased` heading is renamed to the new
version.

## Releases
The Supervisor pulls images by version tag and never re-pulls an existing
tag, so any change that should reach an installed add-on needs a version
bump. A release = bump the version in `numbat/config.yaml`, `numbat/pyproject.toml`
and `numbat/src/numbat/__init__.py`, run `uv lock` in `numbat/`, rename
`## Unreleased` to the version, commit, push to `main` — CI builds and
publishes the GHCR images.

If the release changes the dashboard's appearance, first regenerate the
README screenshots: `bun run screenshot` from `numbat/frontend/` (builds,
then captures light + dark via headless Firefox into
`docs/screenshots/dashboard-*.png`) and commit the updated PNGs.

## Checks
Run `uv run ruff check .` and `uv run pytest -q` from `numbat/` before
committing. For frontend changes also run `bun run typecheck` and
`bun run build` from `numbat/frontend/`.

## Frontend (numbat/frontend)
React 19 (+ React Compiler) + TypeScript + Recharts + Tailwind v4, built by
Vite with Bun as package manager (`bunfig.toml` pins `minimumReleaseAge` to
7 days — keep it). `bun run build` outputs to `numbat/src/numbat/web/dist`
(gitignored); CI builds it once and the per-arch image builds COPY it — never
add a Node/Bun build step to the Dockerfile (aarch64 builds run under QEMU).
Dev: `bun run dev` proxies `/api` + `/health` to a running Numbat on :8099. The
page is served behind HA ingress: every URL must stay relative (`base: './'`,
fetch `./api/...`) and the bundle fully offline (no CDN).

UI components are shadcn (`src/components/ui`, checked in — edit freely, or
re-generate with `bunx shadcn add <name>`); forms use TanStack Form, data
fetching TanStack Query, API contracts are Zod schemas in `src/api.ts`.
Theming keys off `<html data-theme="light|dark">`: an inline script in
`index.html` stamps the resolved theme pre-paint and `src/theme.ts` owns it
after that (per-browser Light/Dark/System setting; System follows
`prefers-color-scheme`). HA ingress never sets a `.dark` class or exposes HA
theme variables, so don't theme off anything else.
