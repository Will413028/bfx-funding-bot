"use client";

import { useTranslations } from "next-intl";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useEarningsHistory } from "@/features/dashboard/hooks/use-earnings-history";

export function EarningsChart() {
  const t = useTranslations("overview");
  const { data, isLoading } = useEarningsHistory(30);

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("earningsTitle")}
      </h3>

      {isLoading ? (
        <div className="mt-4 flex h-[200px] items-center justify-center">
          <div className="size-5 animate-spin rounded-full border-2 border-zinc-700 border-t-zinc-400" />
        </div>
      ) : !data || data.length === 0 ? (
        <div className="mt-4 flex h-[200px] items-center justify-center">
          <p className="text-sm text-zinc-500">{t("noEarningsData")}</p>
        </div>
      ) : (
        <div className="mt-4 h-[200px]">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data}>
              <defs>
                <linearGradient id="earnGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#10b981" stopOpacity={0.2} />
                  <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
              <XAxis
                dataKey="date"
                tick={{ fill: "#a1a1aa", fontSize: 11 }}
                tickFormatter={(v: string) => v.slice(5)}
                axisLine={{ stroke: "#3f3f46" }}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: "#a1a1aa", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v: number) => `$${v.toFixed(0)}`}
                width={48}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "#18181b",
                  border: "1px solid rgba(255,255,255,0.1)",
                  borderRadius: 8,
                  fontSize: 12,
                }}
                labelStyle={{ color: "#a1a1aa" }}
                formatter={(value) => [
                  `$${Number(value).toFixed(2)}`,
                  t("earned"),
                ]}
              />
              <Area
                type="monotone"
                dataKey="amount"
                stroke="#10b981"
                strokeWidth={1.5}
                fill="url(#earnGrad)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
