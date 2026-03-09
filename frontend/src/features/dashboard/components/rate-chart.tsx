"use client";

import { useEffect, useRef, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useWSStore } from "@/stores/ws-store";

const MAX_POINTS = 60;

interface RatePoint {
  time: string;
  apr: number;
}

export function RateChart() {
  const snapshot = useWSStore((s) => s.snapshot);
  const status = useWSStore((s) => s.status);
  const bufferRef = useRef<RatePoint[]>([]);
  const [data, setData] = useState<RatePoint[]>([]);

  useEffect(() => {
    if (!snapshot) return;

    const point: RatePoint = {
      time: new Date(snapshot.timestamp).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }),
      apr: snapshot.frr * 365 * 100,
    };

    const buf = bufferRef.current;
    buf.push(point);
    if (buf.length > MAX_POINTS) buf.shift();
    setData([...buf]);
  }, [snapshot]);

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        FRR (APR%) — Live
      </h3>

      {data.length === 0 ? (
        <div className="mt-4 flex h-[200px] items-center justify-center">
          <p className="text-sm text-zinc-500">
            {status === "connected"
              ? "Waiting for market data..."
              : "Connecting to market feed..."}
          </p>
        </div>
      ) : (
        <div className="mt-4 h-[200px]">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data}>
              <defs>
                <linearGradient id="rateGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#10b981" stopOpacity={0.2} />
                  <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
              <XAxis
                dataKey="time"
                tick={{ fill: "#a1a1aa", fontSize: 11 }}
                axisLine={{ stroke: "#3f3f46" }}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: "#a1a1aa", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v: number) => `${v.toFixed(1)}%`}
                width={48}
                domain={["auto", "auto"]}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "#18181b",
                  border: "1px solid rgba(255,255,255,0.1)",
                  borderRadius: 8,
                  fontSize: 12,
                }}
                labelStyle={{ color: "#a1a1aa" }}
                formatter={(value) => [`${Number(value).toFixed(2)}%`, "APR"]}
              />
              <Area
                type="monotone"
                dataKey="apr"
                stroke="#10b981"
                strokeWidth={1.5}
                fill="url(#rateGrad)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
