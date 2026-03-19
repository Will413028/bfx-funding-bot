package strategy

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestTermStructure_Steep(t *testing.T) {
	a := NewTermStructureAnalyzer()
	book := []domain.BookEntry{
		{Rate: 0.0001, Period: 2, Amount: 100},
		{Rate: 0.0002, Period: 14, Amount: 100},
		{Rate: 0.0003, Period: 30, Amount: 100},
	}
	if shape := a.ClassifyFromBook(book); shape != TermSteep {
		t.Errorf("expected steep, got %s", shape)
	}
}

func TestTermStructure_Inverted(t *testing.T) {
	a := NewTermStructureAnalyzer()
	book := []domain.BookEntry{
		{Rate: 0.0004, Period: 2, Amount: 100},
		{Rate: 0.0003, Period: 14, Amount: 100},
		{Rate: 0.0002, Period: 30, Amount: 100},
	}
	if shape := a.ClassifyFromBook(book); shape != TermInverted {
		t.Errorf("expected inverted, got %s", shape)
	}
}

func TestTermStructure_Humped(t *testing.T) {
	a := NewTermStructureAnalyzer()
	book := []domain.BookEntry{
		{Rate: 0.0002, Period: 2, Amount: 100},
		{Rate: 0.0005, Period: 10, Amount: 100},
		{Rate: 0.0003, Period: 30, Amount: 100},
	}
	if shape := a.ClassifyFromBook(book); shape != TermHumped {
		t.Errorf("expected humped, got %s", shape)
	}
}

func TestTermStructure_Flat(t *testing.T) {
	a := NewTermStructureAnalyzer()
	book := []domain.BookEntry{
		{Rate: 0.00025, Period: 2, Amount: 100},
		{Rate: 0.00025, Period: 14, Amount: 100},
		{Rate: 0.00025, Period: 30, Amount: 100},
	}
	if shape := a.ClassifyFromBook(book); shape != TermFlat {
		t.Errorf("expected flat, got %s", shape)
	}
}

func TestTermStructure_EmptyBook(t *testing.T) {
	a := NewTermStructureAnalyzer()
	if shape := a.ClassifyFromBook(nil); shape != TermFlat {
		t.Errorf("expected flat for empty book, got %s", shape)
	}
}

func TestTermStructure_FilterPeriod_Steep(t *testing.T) {
	a := NewTermStructureAnalyzer()
	lo, hi := a.FilterPeriod(TermSteep, 2, 30)
	if lo != 2 || hi != 30 {
		t.Errorf("steep: expected (2, 30), got (%d, %d)", lo, hi)
	}
}

func TestTermStructure_FilterPeriod_Inverted(t *testing.T) {
	a := NewTermStructureAnalyzer()
	lo, hi := a.FilterPeriod(TermInverted, 2, 30)
	if lo != 2 || hi != 2 {
		t.Errorf("inverted: expected (2, 2), got (%d, %d)", lo, hi)
	}
}

func TestTermStructure_FilterPeriod_Humped(t *testing.T) {
	a := NewTermStructureAnalyzer()
	lo, hi := a.FilterPeriod(TermHumped, 2, 30)
	if lo != 7 || hi != 14 {
		t.Errorf("humped: expected (7, 14), got (%d, %d)", lo, hi)
	}
}
