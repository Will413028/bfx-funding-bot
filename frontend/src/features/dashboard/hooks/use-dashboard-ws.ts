import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { dashboardKeys } from "@/lib/query-keys";
import { useWSStore } from "@/stores/ws-store";
import type { DashboardSummary, MarketSummary } from "@/types";

export function useDashboardWS() {
  const queryClient = useQueryClient();
  const snapshot = useWSStore((s) => s.snapshot);
  const status = useWSStore((s) => s.status);

  useEffect(() => {
    const { connect, disconnect } = useWSStore.getState();
    connect();
    return () => disconnect();
  }, []);

  // Sync WS snapshot into React Query cache
  useEffect(() => {
    if (!snapshot) return;

    const market: MarketSummary = {
      frr: snapshot.frr,
      regime: snapshot.regime,
      mdcScore: snapshot.mdcScore,
      flashFreeze: snapshot.flashFreeze,
      timestamp: snapshot.timestamp,
    };

    queryClient.setQueryData<DashboardSummary>(
      dashboardKeys.summary(),
      (prev) => (prev ? { ...prev, market } : prev),
    );
  }, [snapshot, queryClient]);

  return { status };
}
