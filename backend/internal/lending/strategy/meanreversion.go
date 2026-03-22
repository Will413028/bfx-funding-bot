package strategy

import "math"

// PHigherRate computes the probability that the rate will be higher after
// waiting `waitHours`, using a mean-reversion (Ornstein-Uhlenbeck) model.
//
// P(higher) = phi((mu - current) / (sigma * sqrt(t)))
// where mu = EMA_7d, sigma = rate volatility, t = wait time in hours
func PHigherRate(currentRate, ema7d, rateVolatility, waitHours float64) float64 {
	if rateVolatility <= 0 || waitHours <= 0 {
		return 0.5 // no info -> 50/50
	}
	if ema7d <= 0 {
		return 0.5
	}

	z := (ema7d - currentRate) / (rateVolatility * math.Sqrt(waitHours))
	return normalCDF(z)
}

// normalCDF computes the cumulative distribution function of the standard normal.
func normalCDF(x float64) float64 {
	return 0.5 * (1 + math.Erf(x/math.Sqrt2))
}
