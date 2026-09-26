"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  useCurrencyToggleRequest,
  useTradingControl,
  useTradingControlRequest,
} from "@/features/dashboard/hooks/use-trading-control";
import { ApiError } from "@/lib/api-client";
import type {
  CurrencyAction,
  CurrencyPolicy,
  TradingControlOverview,
} from "@/types";

const KILL_PHRASE = "KILL";
const KNOWN_ERRORS = new Set([
  "request_pending",
  "forbidden",
  "policy_unavailable",
]);

function humanize(code: string): string {
  return code.replaceAll("_", " ");
}

function time(ms: number): string {
  return new Date(ms).toLocaleString();
}

/**
 * Trading state, resume, the kill switch and each currency's enable/disable
 * (lending envelope ADR D4).
 */
export function TradingControlPanel({
  exchangeAccountId,
}: {
  exchangeAccountId: string;
}) {
  const t = useTranslations("tradingControl");
  const overview = useTradingControl(exchangeAccountId);
  const control = useTradingControlRequest(exchangeAccountId);
  const id = useId();
  const [reason, setReason] = useState("");

  if (overview.isError && !overview.data)
    // Nothing to act on but the stop: a kill never depends on reading the state.
    return (
      <section className="space-y-4 rounded-xl border p-5">
        <p role="alert">{t("unavailable")}</p>
        <KillControl control={control} halted={false} queued={false} />
        {control.error && <ErrorView error={control.error} />}
      </section>
    );
  if (!overview.data) return <p>{t("loading")}</p>;
  const data = overview.data;
  const state = data.trading_state;
  const pending = data.requests.find(
    (request) => request.state === "requested" && request.action !== "kill",
  );
  const pendingKill = data.requests.find(
    (request) => request.state === "requested" && request.action === "kill",
  );
  const latest = data.requests.find((request) => request.state !== "requested");
  // Stale data after a failed re-read: show it, but act only on what is current.
  const blocked = overview.isError || control.isPending;

  function resume() {
    control.mutate(
      { action: "resume", reason: reason.trim() },
      { onSuccess: () => setReason("") },
    );
  }

  return (
    <section
      className="space-y-4 rounded-xl border p-5"
      aria-labelledby={`${id}-title`}
    >
      <h2 id={`${id}-title`} className="text-lg font-semibold">
        {t("title")}
      </h2>
      {overview.isError && <p role="alert">{t("unavailable")}</p>}
      {state === null ? (
        <output className="block">{t("noDecision")}</output>
      ) : (
        <div className="space-y-1">
          <p className="text-base font-semibold">
            {t(`state.${state.state}`)} · {t(`cause.${state.cause}`)}
          </p>
          <p>{t("by", { actor: state.actor, reason: state.reason })}</p>
          <p className="text-sm text-muted-foreground">
            {t("since", { time: time(state.at_ms) })}
          </p>
        </div>
      )}
      {state?.state === "HALTED" && <CancelAllView data={data} />}

      <p className="text-sm text-muted-foreground">{t("mfa")}</p>
      {pending && (
        <output className="block">
          {t("waiting", { action: t(`action.${pending.action}`) })}
        </output>
      )}
      {pendingKill && <output className="block">{t("waitingKill")}</output>}
      {latest && (
        <p role={latest.state === "applied" ? undefined : "alert"}>
          {t(`outcome.${latest.state as "applied" | "rejected" | "failed"}`, {
            action: t(`action.${latest.action}`),
          })}
          {latest.outcome_reason ? ` — ${humanize(latest.outcome_reason)}` : ""}
        </p>
      )}

      {state?.state !== "ACTIVE" && (
        <div className="space-y-2 border-t pt-3">
          <label className="block" htmlFor={`${id}-reason`}>
            {t("reason")}
          </label>
          <Input
            id={`${id}-reason`}
            value={reason}
            maxLength={500}
            onChange={(event) => setReason(event.target.value)}
          />
          <Button
            disabled={blocked || Boolean(pending) || !reason.trim()}
            onClick={resume}
          >
            {t("resume")}
          </Button>
          <p className="text-sm text-muted-foreground">{t("resumeHint")}</p>
        </div>
      )}

      <KillControl
        control={control}
        halted={state?.state === "HALTED"}
        queued={Boolean(pendingKill)}
      />
      {control.error && <ErrorView error={control.error} />}

      <CurrenciesView
        exchangeAccountId={exchangeAccountId}
        currencies={data.currencies ?? []}
        stale={overview.isError}
      />
    </section>
  );
}

/** Every currency's everyday stop: the policy's enabled flag (ADR D4). */
function CurrenciesView({
  exchangeAccountId,
  currencies,
  stale,
}: {
  exchangeAccountId: string;
  currencies: CurrencyPolicy[];
  stale: boolean;
}) {
  const t = useTranslations("tradingControl.currencies");
  return (
    <div className="space-y-3 border-t pt-3">
      <h3 className="font-semibold">{t("title")}</h3>
      <p className="text-sm text-muted-foreground">{t("hint")}</p>
      {currencies.length === 0 ? (
        <p>{t("none")}</p>
      ) : (
        <ul className="space-y-3">
          {currencies.map((currency) => (
            <CurrencyControl
              key={currency.symbol}
              exchangeAccountId={exchangeAccountId}
              currency={currency}
              stale={stale}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function percent(fraction: string): string {
  return `${Number((Number(fraction) * 100).toFixed(4))}%`;
}

function CurrencyControl({
  exchangeAccountId,
  currency,
  stale,
}: {
  exchangeAccountId: string;
  currency: CurrencyPolicy;
  stale: boolean;
}) {
  const t = useTranslations("tradingControl");
  const c = useTranslations("tradingControl.currencies");
  const toggle = useCurrencyToggleRequest(exchangeAccountId);
  const id = useId();
  const [reason, setReason] = useState("");
  const { symbol, envelope } = currency;
  const pending = currency.requests.filter(
    (request) => request.state === "requested",
  );
  const latest = currency.requests.find(
    (request) => request.state !== "requested",
  );
  const action: CurrencyAction | null =
    currency.enabled === null ? null : currency.enabled ? "disable" : "enable";
  const waiting = pending.some((request) => request.action === action);
  // Stale data after a failed re-read: a disable only narrows trading (and is
  // a no-op if already in force), so it stays available, like the kill.
  const held = (stale && action === "enable") || toggle.isPending || waiting;

  return (
    <li
      className="space-y-1 rounded-lg border p-3"
      aria-labelledby={`${id}-symbol`}
    >
      <p id={`${id}-symbol`} className="font-semibold">
        {symbol} ·{" "}
        {currency.policy_error !== null
          ? c("unreadable")
          : currency.enabled
            ? c("enabled")
            : c("disabled")}
      </p>
      {currency.policy_error !== null && (
        <p role="alert">
          {c("unreadableBody", { code: humanize(currency.policy_error) })}
        </p>
      )}
      {currency.policy_error === null &&
        (envelope === null ? (
          <p role="alert">{c("envelopeUnset")}</p>
        ) : (
          <p className="text-sm tabular-nums">
            {c("envelope", {
              max: currency.max_offer_amount ?? "—",
              minDays: envelope.min_period_days,
              maxDays: envelope.max_period_days,
              offers: envelope.max_open_offers,
              apr: percent(envelope.min_rate_apr),
              ratio: envelope.rate_floor_ratio,
            })}
          </p>
        ))}
      {pending.map((request) => (
        <output key={request.request_id} className="block">
          {t("waiting", { action: c(`action.${request.action}`) })}
        </output>
      ))}
      {latest && (
        <p role={latest.state === "applied" ? undefined : "alert"}>
          {t(`outcome.${latest.state as "applied" | "rejected" | "failed"}`, {
            action: c(`action.${latest.action}`),
          })}
          {latest.outcome_reason ? ` — ${humanize(latest.outcome_reason)}` : ""}
        </p>
      )}
      {action !== null && (
        <div className="space-y-2 pt-1">
          <label className="block" htmlFor={`${id}-reason`}>
            {c("reason", { symbol })}
          </label>
          <Input
            id={`${id}-reason`}
            value={reason}
            maxLength={500}
            onChange={(event) => setReason(event.target.value)}
          />
          <Button
            variant={action === "disable" ? "outline" : "default"}
            disabled={held || !reason.trim()}
            onClick={() =>
              toggle.mutate(
                { symbol, action, reason: reason.trim() },
                { onSuccess: () => setReason("") },
              )
            }
          >
            {c(action, { symbol })}
          </Button>
          {toggle.error && <ErrorView error={toggle.error} />}
        </div>
      )}
    </li>
  );
}

/** The stop, with its own explicit second confirmation: a reason and the typed phrase. */
function KillControl({
  control,
  halted,
  queued,
}: {
  control: ReturnType<typeof useTradingControlRequest>;
  halted: boolean;
  queued: boolean;
}) {
  const t = useTranslations("tradingControl");
  const id = useId();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [phrase, setPhrase] = useState("");

  function close() {
    setOpen(false);
    setReason("");
    setPhrase("");
  }

  if (!open)
    return (
      <Button
        variant="destructive"
        disabled={queued}
        onClick={() => setOpen(true)}
      >
        {halted ? t("killRetry") : t("kill")}
      </Button>
    );
  return (
    <fieldset className="space-y-2 rounded-lg border border-destructive p-3">
      <legend className="font-semibold">{t("killTitle")}</legend>
      <p>{t("killBody")}</p>
      <label className="block" htmlFor={`${id}-reason`}>
        {t("reason")}
      </label>
      <Input
        id={`${id}-reason`}
        value={reason}
        maxLength={500}
        onChange={(event) => setReason(event.target.value)}
      />
      <label className="block" htmlFor={`${id}-phrase`}>
        {t("killPhrase")}
      </label>
      <Input
        id={`${id}-phrase`}
        value={phrase}
        autoComplete="off"
        onChange={(event) => setPhrase(event.target.value)}
      />
      <div className="flex gap-2">
        <Button
          variant="destructive"
          disabled={
            control.isPending || phrase !== KILL_PHRASE || !reason.trim()
          }
          onClick={() =>
            control.mutate(
              { action: "kill", reason: reason.trim() },
              { onSuccess: close },
            )
          }
        >
          {t("killConfirm")}
        </Button>
        <Button variant="outline" onClick={close}>
          {t("killCancel")}
        </Button>
      </div>
    </fieldset>
  );
}

function CancelAllView({ data }: { data: TradingControlOverview }) {
  const t = useTranslations("tradingControl");
  const incomplete =
    data.cancel_all.length === 0 ||
    data.cancel_all.some((phase) => phase.phase !== "acknowledged");
  return (
    <div className="space-y-1 border-t pt-3">
      <h3 className="font-semibold">{t("cancelAllTitle")}</h3>
      {data.cancel_all.length === 0 ? (
        <p role="alert">{t("cancelAllNone")}</p>
      ) : (
        <ul className="text-sm">
          {data.cancel_all.map((phase) => (
            <li key={phase.currency}>
              {phase.currency}: {t(`phase.${phase.phase}`)}
              {phase.detail ? ` — ${phase.detail}` : ""}
            </li>
          ))}
        </ul>
      )}
      {incomplete && data.cancel_all.length > 0 && (
        <p role="alert">{t("cancelAllIncomplete")}</p>
      )}
    </div>
  );
}

function ErrorView({ error }: { error: Error }) {
  const t = useTranslations("tradingControl");
  const code =
    error instanceof ApiError
      ? error.status === 403
        ? "forbidden"
        : error.code
      : error.message;
  return (
    <p role="alert">
      {KNOWN_ERRORS.has(code)
        ? t(
            `error.${code as "request_pending" | "forbidden" | "policy_unavailable"}`,
          )
        : t("error.generic", { code })}
    </p>
  );
}
