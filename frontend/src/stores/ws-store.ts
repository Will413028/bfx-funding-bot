import { create } from "zustand";
import { env } from "@/lib/env";
import { WSClient } from "@/lib/ws-client";
import type { MarketSnapshot } from "@/types";

type WSStatus = "disconnected" | "connecting" | "connected";

interface WSState {
  snapshot: MarketSnapshot | null;
  status: WSStatus;
  connecting: boolean;
  connect: () => void;
  disconnect: () => void;
}

let client: WSClient | null = null;

export const useWSStore = create<WSState>((set, get) => ({
  snapshot: null,
  status: "disconnected",
  connecting: false,

  connect: () => {
    if (client || get().connecting) return;
    set({ connecting: true });

    client = new WSClient({
      wsUrl: env.NEXT_PUBLIC_WS_URL,
      onSnapshot: (snapshot) => set({ snapshot }),
      onStatusChange: (status) => set({ status }),
    });

    client.connect().finally(() => {
      set({ connecting: false });
    });
  },

  disconnect: () => {
    if (client) {
      client.disconnect();
      client = null;
    }
    set({ status: "disconnected", connecting: false });
  },
}));
