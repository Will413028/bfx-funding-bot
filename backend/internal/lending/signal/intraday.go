package signal

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// Session defines a high-demand trading session in UTC.
type session struct {
	startHour   int
	startMinute int
	endHour     int
	endMinute   int
	peak        float64
}

var highDemandSessions = []session{
	{startHour: 0, startMinute: 0, endHour: 3, endMinute: 0, peak: 0.45},  // Asia open
	{startHour: 7, startMinute: 0, endHour: 9, endMinute: 0, peak: 0.40},  // Europe open
	{startHour: 13, startMinute: 0, endHour: 15, endMinute: 0, peak: 0.45}, // US open
}

const (
	transitionMinutes = 30
	lowDemandBaseline = -0.3
)

// Intraday produces a time-based demand signal based on trading session
// and month-of-day position (V10 §4.5).
type Intraday struct{}

func NewIntraday() *Intraday {
	return &Intraday{}
}

func (i *Intraday) Name() string { return string(domain.SignalIntraday) }

func (i *Intraday) Compute(data *domain.RawMarketData) domain.SignalValue {
	t := data.Timestamp.UTC()
	minuteOfDay := t.Hour()*60 + t.Minute()

	// Compute raw session signal
	value := sessionSignal(minuteOfDay)

	// Apply month-of-day multiplier
	value *= monthMultiplier(t.Day())

	// Clamp to [-0.5, +0.5]
	value = math.Max(-0.5, math.Min(0.5, value))

	return domain.SignalValue{
		Type:       domain.SignalIntraday,
		Value:      value,
		Confidence: 1.0,
		Timestamp:  data.Timestamp,
	}
}

// sessionSignal returns the signal value based on which trading session
// the current time falls in, with linear transition zones.
func sessionSignal(minuteOfDay int) float64 {
	for _, s := range highDemandSessions {
		start := s.startHour*60 + s.startMinute
		end := s.endHour*60 + s.endMinute

		transStart := start - transitionMinutes
		transEnd := end + transitionMinutes

		// Handle wrap-around for sessions near midnight (Asia open transition)
		if transStart < 0 {
			// Transition zone wraps to previous day
			if minuteOfDay >= 24*60+transStart { // e.g., 23:30+
				progress := float64(minuteOfDay-(24*60+transStart)) / float64(transitionMinutes)
				return lerp(lowDemandBaseline, s.peak, progress)
			}
		}

		if minuteOfDay >= start && minuteOfDay < end {
			// Fully inside session
			return s.peak
		}

		if transStart >= 0 && minuteOfDay >= transStart && minuteOfDay < start {
			// Entering transition zone
			progress := float64(minuteOfDay-transStart) / float64(transitionMinutes)
			return lerp(lowDemandBaseline, s.peak, progress)
		}

		if minuteOfDay >= end && minuteOfDay < transEnd {
			// Leaving transition zone
			progress := float64(minuteOfDay-end) / float64(transitionMinutes)
			return lerp(s.peak, lowDemandBaseline, progress)
		}
	}

	return lowDemandBaseline
}

// monthMultiplier returns the month-position factor per V10 §4.5.
func monthMultiplier(day int) float64 {
	switch {
	case day >= 28:
		return 1.1
	case day <= 3:
		return 1.05
	default:
		return 1.0
	}
}

// lerp performs linear interpolation between a and b.
func lerp(a, b, t float64) float64 {
	return a + (b-a)*t
}
