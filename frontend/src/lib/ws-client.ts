import type { MarketSnapshot } from "@/types";

const MIN_RECONNECT_MS = 1_000;
const MAX_RECONNECT_MS = 30_000;

interface WSMessage {
  type: string;
  data: unknown;
}

export interface WSClientOptions {
  wsUrl: string;
  onSnapshot: (snapshot: MarketSnapshot) => void;
  onStatusChange: (status: "connecting" | "connected" | "disconnected") => void;
}

export class WSClient {
  private ws: WebSocket | null = null;
  private reconnectMs = MIN_RECONNECT_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private intentionalClose = false;
  private opts: WSClientOptions;

  constructor(opts: WSClientOptions) {
    this.opts = opts;
  }

  async connect(): Promise<void> {
    this.intentionalClose = false;
    this.opts.onStatusChange("connecting");

    let token: string;
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10_000);
      const res = await fetch("/api/proxy/auth/ws-token", {
        method: "POST",
        signal: controller.signal,
      });
      clearTimeout(timeout);
      if (!res.ok) {
        this.opts.onStatusChange("disconnected");
        this.scheduleReconnect();
        return;
      }
      const body = (await res.json()) as { token: string };
      token = body.token;
    } catch {
      this.opts.onStatusChange("disconnected");
      this.scheduleReconnect();
      return;
    }

    if (this.intentionalClose) {
      return;
    }

    const url = `${this.opts.wsUrl}/api/v1/ws?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(url);

    ws.onopen = () => {
      this.reconnectMs = MIN_RECONNECT_MS;
      this.opts.onStatusChange("connected");
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data as string) as WSMessage;
        if (msg.type === "snapshot") {
          this.opts.onSnapshot(msg.data as MarketSnapshot);
        }
      } catch {
        // Ignore malformed messages
      }
    };

    ws.onclose = () => {
      this.ws = null;
      this.opts.onStatusChange("disconnected");
      if (!this.intentionalClose) {
        this.scheduleReconnect();
      }
    };

    ws.onerror = () => {
      // onclose will fire after onerror
    };

    this.ws = ws;
  }

  disconnect(): void {
    this.intentionalClose = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.opts.onStatusChange("disconnected");
  }

  private scheduleReconnect(): void {
    if (this.intentionalClose) return;

    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, this.reconnectMs);

    this.reconnectMs = Math.min(this.reconnectMs * 2, MAX_RECONNECT_MS);
  }
}
