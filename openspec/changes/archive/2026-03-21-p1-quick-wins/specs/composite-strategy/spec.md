## MODIFIED Requirements

### Requirement: Stage 4 Final Adjustments

Final adjustments SHALL:
1. Apply **psychological price avoidance only** (no random noise) to each offer's rate
2. Compute partial fill adjustment (amount ±20% based on fill proxy)
3. Collect residual offer cancels from partial fill detection
4. Collect stale offer cancels from queue position (carried from Stage 1)
5. **Collect refresh cancels**: active offers older than `refreshAge` with queue discount < 1.0
6. Remove any offer with amount < $50 after adjustments

#### Scenario: Noise only avoids psychological levels
- **WHEN** noise is applied to an offer rate of 0.0005
- **THEN** rate SHALL be shifted away from the psychological level but NOT randomly perturbed

#### Scenario: Proactive refresh cancels deep-queue old offers
- **WHEN** an active offer is 12 minutes old and its queue discount is 0.98 (moderate)
- **THEN** the offer SHALL be added to cancels for refresh

#### Scenario: Recent offer not refreshed
- **WHEN** an active offer is 3 minutes old even with deep queue
- **THEN** the offer SHALL NOT be cancelled for refresh

## ADDED Requirements

### Requirement: Confidence-scaled deployment ratio

The composite pipeline SHALL scale the deployed capital by signal confidence. When signals are contradictory (low confidence), less capital is deployed.

Formula: `deploymentRatio = 0.5 + 0.5 × |MDC.Score| × avgConfidence`

#### Scenario: High confidence bullish
- **WHEN** MDC score is 0.8 and average signal confidence is 0.9
- **THEN** deploymentRatio SHALL be 0.86 (deploy 86% of available)

#### Scenario: Low confidence neutral
- **WHEN** MDC score is 0.1 and average signal confidence is 0.5
- **THEN** deploymentRatio SHALL be 0.525 (deploy 52.5%, retain 47.5% as reserve)

#### Scenario: Zero signals
- **WHEN** no signals are available (empty slice)
- **THEN** deploymentRatio SHALL be 0.5 (minimum deployment)
