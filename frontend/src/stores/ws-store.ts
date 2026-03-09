import { create } from "zustand";
import { env } from "@/lib/env";
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
let connecting = false;

export const useWSStore = create<WSState>((set) => ({
  snapshot: null,
  status: "disconnected",

  connect: () => {
    if (client || connecting) return;
    connecting = true;

    client = new WSClient({
      wsUrl: env.NEXT_PUBLIC_WS_URL,
      onSnapshot: (snapshot) => set({ snapshot }),
      onStatusChange: (status) => set({ status }),
    });

    client.connect().finally(() => { connecting = false; });
  },

  disconnect: () => {
    if (client) {
      client.disconnect();
      client = null;
    }
    connecting = false;
    set({ status: "disconnected" });
  },
}));
