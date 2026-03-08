# dashboard-shell Specification

## Purpose
TBD - created by archiving change dashboard-layout. Update Purpose after archive.
## Requirements
### Requirement: Desktop sidebar navigation
The sidebar SHALL display navigation items (Overview, API Keys, Strategy, History, Settings) with lucide icons, highlight the active item based on current pathname, and include a logout button at the bottom.

#### Scenario: Active nav item
- **WHEN** the user is on `/en/overview`
- **THEN** the "Overview" nav item SHALL have active styling (`bg-white/[0.08]`)
- **AND** all other items SHALL have default styling

#### Scenario: Logout
- **WHEN** the user clicks the logout button in the sidebar
- **THEN** the logout Server Action SHALL be called, clearing the auth cookie and redirecting to login

### Requirement: Mobile responsive sidebar
On mobile viewports (below `md` breakpoint), the sidebar SHALL be hidden and accessible via a hamburger menu button in the topbar that opens a Sheet drawer.

#### Scenario: Mobile menu open
- **WHEN** the user taps the hamburger icon on mobile
- **THEN** a Sheet drawer SHALL slide in from the left with the same nav items as the desktop sidebar

#### Scenario: Mobile nav selection
- **WHEN** the user taps a nav item in the mobile Sheet
- **THEN** the Sheet SHALL close and navigate to the selected page

### Requirement: TopBar with logo and controls
The topbar SHALL display the app logo/name, a locale switcher, and on mobile a hamburger menu button. It SHALL use glassmorphism styling (`backdrop-blur-xl`).

#### Scenario: Desktop topbar
- **WHEN** the dashboard is rendered on desktop
- **THEN** the topbar SHALL show the logo on the left and locale switcher on the right
- **AND** the hamburger button SHALL be hidden

#### Scenario: Mobile topbar
- **WHEN** the dashboard is rendered on mobile
- **THEN** the topbar SHALL show a hamburger button on the left, logo in center, and locale switcher on the right

### Requirement: Locale switcher
The locale switcher SHALL allow switching between available locales (en, zh-TW) using a DropdownMenu, preserving the current page path.

#### Scenario: Switch locale
- **WHEN** the user selects "zh-TW" while on `/en/overview`
- **THEN** the app SHALL navigate to `/zh-TW/overview`

### Requirement: Premium dark theme
The dashboard layout SHALL follow ui-premium-dark-theme: sidebar `bg-zinc-950 border-r border-white/5`, topbar `bg-zinc-950/80 backdrop-blur-xl border-b border-white/5`, nav items with `hover:bg-white/[0.04]`.

#### Scenario: Visual appearance
- **WHEN** the dashboard is rendered
- **THEN** the sidebar and topbar SHALL use the specified dark theme classes

### Requirement: Loading skeleton adapts to layout
The dashboard loading skeleton SHALL render within the layout's main content area (not full-screen), showing a spinner or skeleton content.

#### Scenario: Page transition loading
- **WHEN** a dashboard page is loading
- **THEN** the loading skeleton SHALL appear inside the main content area while the sidebar and topbar remain visible

