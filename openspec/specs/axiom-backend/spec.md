## ADDED Requirements

### Requirement: Axiom zap core integration
The system SHALL integrate axiom-go/adapters/zap as an additional zap core using zapcore.NewTee. Only created when AXIOM_TOKEN and AXIOM_DATASET are set.

#### Scenario: Production with Axiom configured
- **WHEN** AXIOM_TOKEN and AXIOM_DATASET are set
- **THEN** logger SHALL output to both stdout and Axiom

#### Scenario: Development without Axiom
- **WHEN** AXIOM_TOKEN or AXIOM_DATASET is not set
- **THEN** logger SHALL fall back to stdout-only without errors

### Requirement: Appconfig Axiom fields
Config SHALL include optional AxiomToken and AxiomDataset fields.

#### Scenario: Axiom env vars absent
- **WHEN** AXIOM_TOKEN is not set
- **THEN** Load() SHALL NOT return an error

### Requirement: Graceful flush on shutdown
The system SHALL flush buffered Axiom logs via fx.Lifecycle OnStop hook.

#### Scenario: Normal shutdown
- **WHEN** SIGTERM received
- **THEN** Axiom adapter Sync() SHALL be called before exit
