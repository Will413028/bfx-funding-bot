## ADDED Requirements

### Requirement: Marketing layout
The marketing layout SHALL provide a header with logo, navigation (Pricing link), and Login button, plus a footer with copyright. It SHALL use premium dark theme styling (`bg-zinc-950`).

#### Scenario: Header navigation
- **WHEN** a visitor views any marketing page
- **THEN** the header SHALL display the app name, a link to Pricing, and a Login button linking to the login page

#### Scenario: Footer display
- **WHEN** a visitor views any marketing page
- **THEN** the footer SHALL display a copyright notice

### Requirement: Landing page hero section
The Landing page SHALL display a hero section with a headline, subtitle describing the product value, and a primary CTA button linking to the register page.

#### Scenario: Hero display
- **WHEN** a visitor views the landing page
- **THEN** the hero SHALL show a headline, subtitle, and "Get Started" CTA button

### Requirement: Landing page features section
The Landing page SHALL display 4 feature cards (Automated Lending, Market Analysis, Secure & Encrypted, Real-time Monitoring) with icons, titles, and descriptions. Cards SHALL use premium dark theme styling.

#### Scenario: Features display
- **WHEN** a visitor views the landing page
- **THEN** 4 feature cards SHALL be displayed in a responsive grid (1 col mobile, 2 col sm, 4 col lg)

### Requirement: Landing page bottom CTA
The Landing page SHALL display a bottom CTA section encouraging visitors to sign up, with a button linking to the register page.

#### Scenario: Bottom CTA display
- **WHEN** a visitor scrolls to the bottom of the landing page
- **THEN** a CTA section SHALL be displayed with a register link

### Requirement: Pricing page plan comparison
The Pricing page SHALL display 4 plan cards (Free, Starter, Pro, Enterprise) with price, feature checklist, and CTA button. The Pro plan SHALL be visually highlighted as recommended.

#### Scenario: Plan cards display
- **WHEN** a visitor views the pricing page
- **THEN** 4 plan cards SHALL be displayed with price, features list, and CTA button

#### Scenario: Pro plan highlight
- **WHEN** displaying the Pro plan card
- **THEN** it SHALL have a visual highlight (e.g., border accent, "Recommended" badge)
