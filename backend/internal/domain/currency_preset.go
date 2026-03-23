package domain

// CurrencyPreset holds per-currency parameters for signal aggregation,
// regime detection, and pricing strategy.
type CurrencyPreset struct {
	MDCWeights             map[SignalType]float64
	MDCLambda              map[SignalType]float64
	RegimeEnterThreshold   float64
	RegimeExitThreshold    float64
	MaxPremiumUp           float64
	MaxPremiumDown         float64
	DeviationContango      float64
	DeviationBackwardation float64
	DeviationNeutral       float64
	DeviationCrisis        float64
}

// StablecoinPreset preserves the original hardcoded values for fUSD/fUST.
// These are the battle-tested defaults used before per-currency support.
var StablecoinPreset = CurrencyPreset{
	MDCWeights: map[SignalType]float64{
		SignalBookConsumption:    0.25,
		SignalLiquidationCascade: 0.20,
		SignalMarginUsage:        0.20,
		SignalMomentum:           0.15,
		SignalCrossCurrency:      0.10,
		SignalIntraday:           0.10,
	},
	MDCLambda: map[SignalType]float64{
		SignalBookConsumption:    0.01,
		SignalLiquidationCascade: 0.005,
		SignalMarginUsage:        0.008,
		SignalMomentum:           0.01,
		SignalCrossCurrency:      0.005,
		SignalIntraday:           0.002,
	},
	RegimeEnterThreshold:   0.30,
	RegimeExitThreshold:    0.20,
	MaxPremiumUp:           0.50,
	MaxPremiumDown:         0.18,
	DeviationContango:      0.40,
	DeviationBackwardation: 0.20,
	DeviationNeutral:       0.25,
	DeviationCrisis:        0.60,
}

// CryptoPreset is optimized for high-volatility, event-driven currencies (fBTC, fETH).
// Book signal is strong (shallow liquidity), needs faster regime response and wider premium space.
var CryptoPreset = CurrencyPreset{
	MDCWeights: map[SignalType]float64{
		SignalBookConsumption:    0.30,
		SignalLiquidationCascade: 0.20,
		SignalMarginUsage:        0.15,
		SignalMomentum:           0.15,
		SignalCrossCurrency:      0.10,
		SignalIntraday:           0.10,
	},
	MDCLambda: map[SignalType]float64{
		SignalBookConsumption:    0.015,
		SignalLiquidationCascade: 0.008,
		SignalMarginUsage:        0.01,
		SignalMomentum:           0.012,
		SignalCrossCurrency:      0.008,
		SignalIntraday:           0.003,
	},
	RegimeEnterThreshold:   0.25,
	RegimeExitThreshold:    0.15,
	MaxPremiumUp:           0.60,
	MaxPremiumDown:         0.25,
	DeviationContango:      0.50,
	DeviationBackwardation: 0.30,
	DeviationNeutral:       0.35,
	DeviationCrisis:        0.70,
}

// PresetForCurrency returns the appropriate CurrencyPreset for the given currency.
// Stablecoins (USD, UST) use conservative parameters; crypto (BTC, ETH) use aggressive parameters.
// Unknown currencies fall back to StablecoinPreset.
func PresetForCurrency(currency string) CurrencyPreset {
	switch currency {
	case "BTC", "ETH":
		return CryptoPreset
	default:
		return StablecoinPreset
	}
}
