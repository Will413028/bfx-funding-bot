## 1. shadcn/ui Components

- [x] 1.1 Install shadcn/ui components: `npx shadcn@latest add separator sheet dropdown-menu` + auto-fix Biome

## 2. Layout Components

- [x] 2.1 Create `src/components/layout/sidebar-nav.tsx` — "use client", nav items array with lucide icons, usePathname active state, i18n labels, logout button at bottom
- [x] 2.2 Create `src/components/layout/sidebar.tsx` — desktop sidebar wrapper (hidden on mobile, w-64, bg-zinc-950 border-r border-white/5), renders SidebarNav + logo
- [x] 2.3 Create `src/components/layout/locale-switcher.tsx` — "use client", DropdownMenu with locale options, useRouter + usePathname to switch locale preserving path
- [x] 2.4 Create `src/components/layout/topbar.tsx` — "use client", logo, mobile hamburger (Sheet trigger), locale switcher, premium dark theme (bg-zinc-950/80 backdrop-blur-xl border-b border-white/5)

## 3. Dashboard Layout & Loading

- [x] 3.1 Update `src/app/[locale]/(dashboard)/layout.tsx` — flex h-screen, desktop sidebar + topbar + scrollable main area
- [x] 3.2 Update `src/app/[locale]/(dashboard)/loading.tsx` — spinner inside main content area (not full-screen)

## 4. Verification

- [x] 4.1 Run `pnpm build` — verify production build succeeds
- [x] 4.2 Run `pnpm lint` — verify tsc + Biome pass
