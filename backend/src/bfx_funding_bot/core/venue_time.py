"""Venue clock facts shared by every comparison against venue timestamps."""

# The venue stamps offers to the whole second on its own clock, so the offer an
# attempt placed can carry an mts_created up to a second (plus clock skew)
# before the attempt started. Evidence that an attempt's offer exists looks
# this much earlier (live UNKNOWN matching and the shadow resolver alike).
VENUE_CLOCK_TOLERANCE_MS = 5_000
