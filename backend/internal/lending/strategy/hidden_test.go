package strategy

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestHidden_HighHiddenRatio(t *testing.T) {
	h := NewHiddenOfferStrategy()
	snap := &domain.MarketSnapshot{HiddenRatio: 0.40, CompetitorActivity: 0.3}
	if !h.ShouldHide(snap, 10000) {
		t.Error("expected hidden when HiddenRatio > 30%")
	}
}

func TestHidden_LowHiddenRatio(t *testing.T) {
	h := NewHiddenOfferStrategy()
	snap := &domain.MarketSnapshot{HiddenRatio: 0.10, CompetitorActivity: 0.3}
	if h.ShouldHide(snap, 10000) {
		t.Error("expected public when HiddenRatio < 30% and low competition")
	}
}

func TestHidden_HighCompetition(t *testing.T) {
	h := NewHiddenOfferStrategy()
	snap := &domain.MarketSnapshot{HiddenRatio: 0.20, CompetitorActivity: 0.70}
	if !h.ShouldHide(snap, 10000) {
		t.Error("expected hidden when high competition + moderate hidden ratio")
	}
}

func TestHidden_SmallAmount(t *testing.T) {
	h := NewHiddenOfferStrategy()
	snap := &domain.MarketSnapshot{HiddenRatio: 0.50, CompetitorActivity: 0.8}
	if h.ShouldHide(snap, 3000) {
		t.Error("expected public for small amounts < 5000")
	}
}

func TestHidden_NilSnapshot(t *testing.T) {
	h := NewHiddenOfferStrategy()
	if h.ShouldHide(nil, 10000) {
		t.Error("expected public for nil snapshot")
	}
}
