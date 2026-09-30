"""Venue clock facts shared by every comparison against venue timestamps."""

# The venue stamps offers to the whole second on its own clock, so the offer an
# attempt placed can carry an mts_created up to a second (plus clock skew)
# before the attempt started. Evidence that an attempt's offer exists looks
# this much earlier (live UNKNOWN matching and the shadow resolver alike).
VENUE_CLOCK_TOLERANCE_MS = 5_000

# Local history request bounds must cover the earliest referenced attempt with
# this margin (independent of any timestamps returned by the venue).
HISTORY_QUERY_MARGIN_MS = 60_000
