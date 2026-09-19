"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import type { FundingStatus, ReleaseSession } from "@/types";

export function FundingPanel({
  exchangeAccountId,
}: {
  exchangeAccountId: string;
}) {
  // Parent keys by account: no prior account's session or amounts can survive a switch.
  const t = useTranslations("funding");
  const cache = useQueryClient();
  const inputId = useId();
  const [selected, setSelected] = useState("");
  const [maximum, setMaximum] = useState("");
  const [minutes, setMinutes] = useState("15");
  const [confirmedRevision, setConfirmedRevision] = useState("");
  const [sessionId, setSessionId] = useState(() =>
    typeof window === "undefined"
      ? ""
      : (localStorage.getItem(`release-session:${exchangeAccountId}`) ?? ""),
  );
  const [restoreId, setRestoreId] = useState("");
  const base = accountScopedPath(exchangeAccountId, "/release-sessions");
  const status = useQuery({
    queryKey: ["funding-status", exchangeAccountId],
    queryFn: () =>
      apiClient.get<FundingStatus>(
        accountScopedPath(exchangeAccountId, "/funding-status"),
      ),
    refetchInterval: 15000,
  });
  const session = useQuery({
    queryKey: ["release-session", exchangeAccountId, sessionId],
    queryFn: () => apiClient.get<ReleaseSession>(`${base}/${sessionId}`),
    enabled: Boolean(sessionId),
    refetchInterval: 3000,
  });
  const request = useMutation({
    mutationFn: async (
      action: "prepare" | "authorize" | "validate" | "promote",
    ) => {
      if (action === "prepare") {
        const cell =
          status.data?.configured_cells
            .filter((c) => c.symbol === "fUST")
            .find((c) => `${c.strategy}:${c.cell}` === selected) ??
          status.data?.configured_cells.find((c) => c.symbol === "fUST");
        if (!cell || !/^\d+(\.\d+)?$/.test(maximum) || !/[1-9]/.test(maximum))
          throw new Error(t("invalidAmount"));
        return apiClient.post<ReleaseSession>(base, {
          symbol: "fUST",
          cell: cell.cell,
          strategy: cell.strategy,
          max_amount: maximum,
          expires_at_ms: Date.now() + Number(minutes) * 60000,
        });
      }
      if (!session.data || session.isError)
        throw new Error(t("refreshRequired"));
      return apiClient.post<ReleaseSession>(`${base}/${sessionId}/${action}`, {
        expected_revision: session.data.request_revision,
      });
    },
    retry: false,
    onSuccess: (data) => {
      setSessionId(data.id);
      localStorage.setItem(`release-session:${exchangeAccountId}`, data.id);
      cache.setQueryData(["release-session", exchangeAccountId, data.id], data);
      setConfirmedRevision("");
    },
    onError: () => {
      setConfirmedRevision("");
      if (sessionId) session.refetch();
    },
  });

  function clearSessionSelection() {
    setSessionId("");
    setRestoreId("");
    setConfirmedRevision("");
    request.reset();
    localStorage.removeItem(`release-session:${exchangeAccountId}`);
  }

  if (status.isError) return <p role="alert">{t("unavailable")}</p>;
  if (!status.data) return <p>{t("loading")}</p>;
  const data = status.data;
  const row = session.isError ? undefined : session.data;
  const revisionKey = row
    ? `${row.id}:${row.state}:${row.request_revision}`
    : "";
  const confirmation = Boolean(
    revisionKey && revisionKey === confirmedRevision,
  );
  const pending =
    request.isPending ||
    !row ||
    row.request_revision !== row.processed_revision;
  const expired = Boolean(row && row.expires_at_ms <= Date.now());
  const choices = data.configured_cells.filter((c) => c.symbol === "fUST");

  return (
    <section className="space-y-4 rounded-xl border p-5">
      <h2 className="text-lg font-semibold">{t("title")}</h2>
      <p>
        {data.halt.halted ? t("halted") : t("notHalted")} {data.halt.reason}
      </p>
      {Object.entries(data.symbols).map(([symbol, capital]) => (
        <section key={symbol} className="space-y-2 border-t pt-3">
          <h3>{symbol === "fUST" ? "USDT" : "USD"}</h3>
          {!capital.capital_available ? (
            <>
              <output>{capital.reason}</output>
              {capital.policy_revision !== undefined && (
                <p>{t("revision", { revision: capital.policy_revision })}</p>
              )}
            </>
          ) : (
            <>
              <p>
                {capital.policy.enabled ? t("enabled") : t("disabled")} ·{" "}
                {t("revision", { revision: capital.policy_revision })}
              </p>
              <dl className="grid gap-2 sm:grid-cols-2">
                {(
                  [
                    ["available", capital.available_balance],
                    ["pending", capital.unreflected_commitments],
                    ["reserve", capital.policy.reserve_amount],
                    ["spendable", capital.spendable],
                    ["total", capital.total_capital],
                    ["unattributed", capital.unattributed_credit_exposure],
                  ] as const
                ).map(([label, value]) => (
                  <div key={label}>
                    <dt>{t(label)}</dt>
                    <dd className="break-all font-mono">{value}</dd>
                  </div>
                ))}
              </dl>
              <p>{t("sharedBudget")}</p>
              {Object.entries(capital.cells).map(([cell, budget]) => (
                <div key={cell} className="text-sm">
                  <span>
                    {cell}: {t("cellHeadroom")} {budget.cell_headroom} ·{" "}
                    {t("cellBudget")} {budget.max_new_offer}
                  </span>
                  <p>
                    {budget.reason ??
                      data.dry_run.symbols[symbol]?.cells[cell]?.blocked_by}
                  </p>
                </div>
              ))}
            </>
          )}
          <p>{data.dry_run.symbols[symbol]?.blocked_by}</p>
        </section>
      ))}
      <h3 className="font-semibold">{t("sessionTitle")}</h3>
      <p>{t("humanOnly")}</p>
      <p>{t("mfaRequired")}</p>
      {!row && !sessionId && (
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            request.mutate("prepare");
          }}
        >
          <label className="block">
            {t("strategy")}
            <select
              className="block"
              value={
                selected ||
                (choices[0] ? `${choices[0].strategy}:${choices[0].cell}` : "")
              }
              onChange={(event) => setSelected(event.target.value)}
            >
              {choices.map((c) => (
                <option
                  key={`${c.strategy}:${c.cell}`}
                  value={`${c.strategy}:${c.cell}`}
                >
                  {c.strategy === "mean_reversion"
                    ? t("meanReversion")
                    : c.strategy}{" "}
                  · {c.period}
                </option>
              ))}
            </select>
          </label>
          <label className="block" htmlFor={`${inputId}-maximum`}>
            {t("maximum")}
            <Input
              id={`${inputId}-maximum`}
              value={maximum}
              onChange={(e) => setMaximum(e.target.value)}
              inputMode="decimal"
              required
            />
          </label>
          <label className="block" htmlFor={`${inputId}-expiry`}>
            {t("expiry")}
            <Input
              id={`${inputId}-expiry`}
              type="number"
              min="1"
              max="60"
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              required
            />
          </label>
          <Button
            disabled={
              request.isPending ||
              !choices.length ||
              !data.symbols.fUST?.capital_available ||
              !data.symbols.fUST.policy.enabled
            }
            type="submit"
          >
            {t("prepare")}
          </Button>
        </form>
      )}
      {!row && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (/^[0-9a-f-]{36}$/.test(restoreId)) {
              setSessionId(restoreId);
              localStorage.setItem(
                `release-session:${exchangeAccountId}`,
                restoreId,
              );
            }
          }}
        >
          <label htmlFor={`${inputId}-restore`}>
            {t("restore")}
            <Input
              id={`${inputId}-restore`}
              value={restoreId}
              onChange={(e) => setRestoreId(e.target.value)}
              required
              pattern="[0-9a-f-]{36}"
            />
          </label>
          <Button type="submit" variant="outline">
            {t("openSession")}
          </Button>
        </form>
      )}
      {session.isError && <p role="alert">{t("refreshRequired")}</p>}
      {sessionId && !row && (
        <div className="space-y-2">
          <p>{t("selectionOnly")}</p>
          <Button
            variant="outline"
            disabled={session.isFetching}
            onClick={() => session.refetch()}
          >
            {t("refresh")}
          </Button>
          <Button
            variant="outline"
            disabled={request.isPending}
            onClick={clearSessionSelection}
          >
            {t("leaveSession")}
          </Button>
        </div>
      )}
      {row && (
        <div className="space-y-3">
          <p>{row.state}</p>
          <p className="break-all">{row.id}</p>
          <p>
            {t("minimum")}: {row.minimum_amount ?? "—"} · {t("exact")}:{" "}
            {row.exact_amount ?? "—"} · {t("maximum")}: {row.max_amount}
          </p>
          <p>
            {t("expires")}: {new Date(row.expires_at_ms).toLocaleString()}
          </p>
          {row.reason && <p role="alert">{row.reason}</p>}
          {row.state === "prepared" && (
            <>
              <label className="block">
                <input
                  type="checkbox"
                  checked={confirmation}
                  onChange={(e) =>
                    setConfirmedRevision(e.target.checked ? revisionKey : "")
                  }
                />{" "}
                {t("confirmCanary")}
              </label>
              <Button
                disabled={pending || expired || !confirmation}
                onClick={() => request.mutate("authorize")}
              >
                {t("authorize")}
              </Button>
            </>
          )}
          {["consumed", "observed"].includes(row.state) && (
            <Button
              disabled={pending}
              onClick={() => request.mutate("validate")}
            >
              {t("validate")}
            </Button>
          )}
          {row.state === "validated" && (
            <>
              <label className="block">
                <input
                  type="checkbox"
                  checked={confirmation}
                  onChange={(e) =>
                    setConfirmedRevision(e.target.checked ? revisionKey : "")
                  }
                />{" "}
                {t("confirmPromote")}
              </label>
              <Button
                disabled={pending || !confirmation}
                onClick={() => request.mutate("promote")}
              >
                {t("promote")}
              </Button>
            </>
          )}
          <details>
            <summary>{t("evidence")}</summary>
            <pre className="overflow-auto text-xs">
              {JSON.stringify(
                {
                  binding: row.binding,
                  halt_id: row.halt_id,
                  evidence: row.evidence,
                  request_revision: row.request_revision,
                  processed_revision: row.processed_revision,
                },
                null,
                2,
              )}
            </pre>
          </details>
          <Button variant="outline" onClick={() => session.refetch()}>
            {t("refresh")}
          </Button>
          {["blocked", "expired", "promoted"].includes(row.state) && (
            <Button
              variant="outline"
              disabled={request.isPending}
              onClick={clearSessionSelection}
            >
              {t("newSession")}
            </Button>
          )}
        </div>
      )}
      {request.error && <p role="alert">{request.error.message}</p>}
    </section>
  );
}
