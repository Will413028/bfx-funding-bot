import { create } from "zustand";
import { WSClient } from "@/lib/ws-client";
import type { MarketSnapshot } from "@/types";

type WSStatus = "disconnected" | "connecting" | "connected";

interface WSState {
  snapshot: MarketSnapshot | null;
  status: WSStatus;
  connect: () => void;
  disconnect: () => void;
}

let client: WSClient | null = null;

export const useWSStore = create<WSState>((set) => ({
  snapshot: null,
  status: "disconnected",

  connect: () => {
    if (client) return;

    client = new WSClient({
      wsUrl: process.env.NEXT_PUBLIC_WS_URL || "",
      onSnapshot: (snapshot) => set({ snapshot }),
      onStatusChange: (status) => set({ status }),
    });

    client.connect();
  },

  disconnect: () => {
    if (client) {
      client.disconnect();
      client = null;
    }
    set({ status: "disconnected" });
  },
}));
