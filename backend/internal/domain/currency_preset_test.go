package domain

import "testing"

func TestPresetForCurrency_Stablecoin(t *testing.T) {
	for _, ccy := range []string{"USD", "UST"} {
		p := PresetForCurrency(ccy)
		if p.RegimeEnterThreshold != StablecoinPreset.RegimeEnterThreshold {
			t.Errorf("PresetForCurrency(%q) enter threshold = %v, want %v", ccy, p.RegimeEnterThreshold, StablecoinPreset.RegimeEnterThreshold)
		}
		if p.MDCWeights[SignalBookConsumption] != 0.25 {
			t.Errorf("PresetForCurrency(%q) BookConsumption weight = %v, want 0.25", ccy, p.MDCWeights[SignalBookConsumption])
		}
	}
}

func TestPresetForCurrency_Crypto(t *testing.T) {
	for _, ccy := range []string{"BTC", "ETH"} {
		p := PresetForCurrency(ccy)
		if p.RegimeEnterThreshold != CryptoPreset.RegimeEnterThreshold {
			t.Errorf("PresetForCurrency(%q) enter threshold = %v, want %v", ccy, p.RegimeEnterThreshold, CryptoPreset.RegimeEnterThreshold)
		}
		if p.MDCWeights[SignalBookConsumption] != 0.30 {
			t.Errorf("PresetForCurrency(%q) BookConsumption weight = %v, want 0.30", ccy, p.MDCWeights[SignalBookConsumption])
		}
	}
}

func TestPresetForCurrency_UnknownFallback(t *testing.T) {
	p := PresetForCurrency("XRP")
	if p.RegimeEnterThreshold != StablecoinPreset.RegimeEnterThreshold {
		t.Errorf("PresetForCurrency(XRP) should fallback to stablecoin, got enter threshold %v", p.RegimeEnterThreshold)
	}
}

func TestPresetWeightsSum(t *testing.T) {
	for name, preset := range map[string]CurrencyPreset{"stablecoin": StablecoinPreset, "crypto": CryptoPreset} {
		var sum float64
		for _, w := range preset.MDCWeights {
			sum += w
		}
		if diff := sum - 1.0; diff > 0.001 || diff < -0.001 {
			t.Errorf("%s preset weights sum = %v, want 1.0", name, sum)
		}
	}
}
