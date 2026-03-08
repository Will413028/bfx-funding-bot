## ADDED Requirements

### Requirement: Next.js 16 project initialization
The system SHALL have a `frontend/` directory at the project root containing a Next.js 16 App Router project initialized with pnpm.

#### Scenario: Project starts successfully
- **WHEN** running `pnpm dev` in the `frontend/` directory
- **THEN** the development server starts on port 3000 with Turbopack enabled

#### Scenario: Production build succeeds
- **WHEN** running `pnpm build` in the `frontend/` directory
- **THEN** the build completes without errors

### Requirement: Package dependencies
The project SHALL include all Phase 1 dependencies in `package.json`:
- **Framework**: next, react, react-dom
- **Styling**: tailwindcss, @tailwindcss/postcss, tailwind-merge, clsx, class-variance-authority, tailwindcss-animate, lucide-react
- **State**: zustand, @tanstack/react-query, @tanstack/react-query-devtools, nuqs
- **Forms**: react-hook-form, @hookform/resolvers, zod
- **i18n**: next-intl
- **Quality**: @biomejs/biome (devDependency), knip (devDependency)

#### Scenario: All dependencies installed
- **WHEN** running `pnpm install` in the `frontend/` directory
- **THEN** all listed packages are installed without peer dependency conflicts

### Requirement: Biome configuration
The project SHALL use Biome as the sole linter and formatter (no ESLint). The configuration file `biome.json` SHALL:
- Enable formatter with space indentation
- Enable linter with recommended rules
- Disable `noUnusedVariables` (handled by Knip)
- Organize imports automatically
- Use double quotes for JavaScript/TypeScript

#### Scenario: Biome lint passes on scaffold code
- **WHEN** running `pnpm lint` (which runs `tsc --noEmit && biome check src/`)
- **THEN** no lint errors are reported on the scaffolded code

### Requirement: Knip configuration
The project SHALL include `knip.config.ts` configured to detect unused files, exports, dependencies, and types.

#### Scenario: Knip runs without false positives on scaffold
- **WHEN** running `pnpm knip`
- **THEN** no unused code is reported for the scaffolded files

### Requirement: PostCSS configuration
The project SHALL have `postcss.config.mjs` with `@tailwindcss/postcss` plugin for Tailwind CSS v4.

#### Scenario: PostCSS processes Tailwind directives
- **WHEN** the dev server compiles CSS
- **THEN** Tailwind utility classes are generated correctly

### Requirement: Environment variables example
The project SHALL include `.env.example` documenting all required environment variables:
- `NEXT_PUBLIC_APP_URL` — app base URL
- `NEXT_PUBLIC_APP_NAME` — app display name
- `NEXT_PUBLIC_WS_URL` — WebSocket URL (client-side)
- `API_URL` — Go backend URL (server-only)
- `AUTH_SECRET` — auth secret (server-only)

#### Scenario: Developer sets up environment
- **WHEN** a developer copies `.env.example` to `.env.local`
- **THEN** all required variables are documented with example values

### Requirement: Package scripts
The `package.json` SHALL define the following scripts:
- `dev`: `next dev --turbopack`
- `build`: `next build`
- `start`: `next start`
- `lint`: `tsc --noEmit && biome check src/`
- `format`: `biome format --write`
- `knip`: `knip`

#### Scenario: All scripts executable
- **WHEN** running any defined script
- **THEN** it executes without configuration errors

### Requirement: shadcn/ui configuration
The project SHALL include `components.json` for shadcn/ui CLI, configured with:
- Style: `new-york`
- TypeScript: enabled
- Tailwind CSS: v4 mode
- Aliases matching the `src/` directory structure (`@/components`, `@/lib`, `@/hooks`)

#### Scenario: Adding a shadcn component
- **WHEN** running `npx shadcn@latest add button`
- **THEN** the component is created at `src/components/ui/button.tsx` with correct imports
