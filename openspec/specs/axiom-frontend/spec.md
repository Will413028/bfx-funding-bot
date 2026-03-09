## ADDED Requirements

### Requirement: Next.js Axiom integration
Frontend SHALL integrate next-axiom with withAxiom wrapper in next.config.ts.

#### Scenario: Config wrapper chain
- **WHEN** next.config.ts is loaded
- **THEN** chain SHALL be withSentryConfig(withAxiom(withNextIntl(nextConfig)))

#### Scenario: Axiom env vars not configured
- **WHEN** AXIOM_DATASET or AXIOM_TOKEN is not set
- **THEN** application SHALL function normally without sending logs

### Requirement: Server-side log utility
Frontend SHALL export a log utility from next-axiom for API routes and server components.

#### Scenario: Web Vitals collection
- **WHEN** a user loads a page
- **THEN** Web Vitals SHALL be automatically reported to Axiom
