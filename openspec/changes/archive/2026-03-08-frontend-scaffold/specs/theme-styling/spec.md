## ADDED Requirements

### Requirement: Dark financial theme
The `src/app/globals.css` SHALL define a dark color scheme suitable for financial trading interfaces using CSS custom properties:
- `--background`: deep blue-black (~220 20% 6%)
- `--foreground`: light gray-white (~210 20% 92%)
- `--primary`: green (profit/up ~142 70% 45%)
- `--destructive`: red (loss/down ~0 72% 51%)
- `--accent`: blue (interactive elements ~217 91% 60%)
- `--muted`, `--muted-foreground`: subdued tones
- `--card`: slightly lighter than background
- `--border`: subtle border color
- `--radius`: 0.5rem

#### Scenario: Dark theme applied by default
- **WHEN** any page loads
- **THEN** the page uses the dark color scheme (html has `dark` class)

### Requirement: Tailwind v4 theme mapping
The `globals.css` SHALL map CSS custom properties to Tailwind theme tokens using `@theme` directive:
- `--color-background`, `--color-foreground`, `--color-primary`, etc.

#### Scenario: Tailwind utility classes use theme colors
- **WHEN** using `className="bg-background text-foreground"`
- **THEN** the element uses the dark theme colors

### Requirement: Tailwind v4 CSS entry point
The `globals.css` SHALL include:
- `@import "tailwindcss"` as the Tailwind v4 entry point
- `@plugin "tailwindcss-animate"` for shadcn/ui animation support
- `@custom-variant dark (&:is(.dark *))` for dark mode variant

#### Scenario: Tailwind processes correctly
- **WHEN** the dev server compiles CSS
- **THEN** utility classes, animations, and dark variant are all available

### Requirement: cn() utility function
The `src/lib/utils.ts` SHALL export a `cn()` function that combines `clsx` and `tailwind-merge` for conditional class merging without conflicts.

#### Scenario: cn merges classes correctly
- **WHEN** calling `cn("px-4 py-2", "px-6")`
- **THEN** the result is `"px-6 py-2"` (tailwind-merge resolves conflicts)
