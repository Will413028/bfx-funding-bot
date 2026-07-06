"use client";

import { useTranslations } from "next-intl";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { WeeklyAttributionPoint } from "@/types";

function toSeries(points: WeeklyAttributionPoint[]) {
  return points.map((p) => ({
    week: new Date(p.weekStartMs).toISOString().slice(0, 10),
    bot: p.realizedAprNetPct === null ? null : Number(p.realizedAprNetPct),
    close:
      p.baselineCloseAprNetPct === null
        ? null
        : Number(p.baselineCloseAprNetPct),
    frr:
      p.baselineFrrAprNetPct === null ? null : Number(p.baselineFrrAprNetPct),
  }));
}

export function AttributionChart({
  cell,
  points,
}: {
  cell: string;
  points: WeeklyAttributionPoint[];
}) {
  const t = useTranslations("attribution");
  const data = toSeries(points);
  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-sm font-medium text-zinc-200">{cell}</h3>
      <p className="text-xs text-zinc-500">{t("subtitle")}</p>
      <div className="mt-4 h-[240px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data}>
            <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
            <XAxis
              dataKey="week"
              tick={{ fill: "#a1a1aa", fontSize: 11 }}
              tickFormatter={(v: string) => v.slice(5)}
              axisLine={{ stroke: "#3f3f46" }}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: "#a1a1aa", fontSize: 11 }}
              axisLine={{ stroke: "#3f3f46" }}
              tickLine={false}
              unit="%"
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "#18181b",
                border: "1px solid #3f3f46",
                borderRadius: 8,
                fontSize: 12,
              }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line
              type="monotone"
              dataKey="bot"
              name={t("botLine")}
              stroke="#10b981"
              strokeWidth={1.5}
              dot={false}
              connectNulls
            />
            <Line
              type="monotone"
              dataKey="close"
              name={t("closeLine")}
              stroke="#818cf8"
              strokeWidth={1.5}
              dot={false}
              connectNulls
            />
            <Line
              type="monotone"
              dataKey="frr"
              name={t("frrLine")}
              stroke="#f59e0b"
              strokeWidth={1.5}
              dot={false}
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
