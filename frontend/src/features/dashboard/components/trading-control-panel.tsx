"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  useTradingControl,
  useTradingControlRequest,
} from "@/features/dashboard/hooks/use-trading-control";
import { ApiError } from "@/lib/api-client";
import type {
  TradingControlAction,
  TradingControlOverview,
  TradingProbation,
} from "@/types";

const KILL_PHRASE = "KILL";
const HOUR_MS = 3_600_000;
const KNOWN_ERRORS = new Set([
  "request_pending",
  "backend_digest_required",
  "forbidden",
]);

function humanize(code: string): string {
  return code.replaceAll("_", " ");
}

function time(ms: number): string {
  return new Date(ms).toLocaleString();
}

/** Trading state, the release flow's approval and probation, and the stop controls. */
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
  const running = data.running;
  const awaitingApproval =
    state?.state === "REDUCING" && state.cause === "material_deploy";
  const approved = data.approvals.some(
    (approval) => approval.backend_digest === running.backend_digest,
  );
  const canApprove =
    (awaitingApproval || running.change_class === "material") && !approved;
  const pending = data.requests.find(
    (request) => request.state === "requested" && request.action !== "kill",
  );
  const pendingKill = data.requests.find(
    (request) => request.state === "requested" && request.action === "kill",
  );
  const latest = data.requests.find((request) => request.state !== "requested");
  // Stale data after a failed re-read: show it, but act only on what is current.
  const blocked = overview.isError || control.isPending;

  function request(action: Exclude<TradingControlAction, "kill">) {
    control.mutate(
      { action, reason: reason.trim(), backendDigest: running.backend_digest },
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
      {state?.probation && <ProbationView probation={state.probation} />}
      {awaitingApproval && <AwaitingApproval data={data} />}
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
        <div className="flex flex-wrap gap-2">
          {canApprove && (
            <Button
              disabled={
                blocked ||
                Boolean(pending) ||
                !reason.trim() ||
                !running.backend_digest
              }
              onClick={() => request("approve")}
            >
              {t("approve")}
            </Button>
          )}
          {state?.state !== "ACTIVE" && !awaitingApproval && (
            <Button
              disabled={blocked || Boolean(pending) || !reason.trim()}
              onClick={() => request("resume")}
            >
              {t("resume")}
            </Button>
          )}
          {state?.state === "ACTIVE" && (
            <Button
              variant="outline"
              disabled={blocked || Boolean(pending) || !reason.trim()}
              onClick={() => request("pause")}
            >
              {t("pause")}
            </Button>
          )}
        </div>
        {canApprove && !running.backend_digest && (
          <p role="alert">{t("identityMissing")}</p>
        )}
        {state?.state !== "ACTIVE" && !awaitingApproval && (
          <p className="text-sm text-muted-foreground">{t("resumeHint")}</p>
        )}
      </div>

      <KillControl
        control={control}
        halted={state?.state === "HALTED"}
        queued={Boolean(pendingKill)}
      />
      {control.error && <ErrorView error={control.error} />}
    </section>
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

function ProbationView({ probation }: { probation: TradingProbation }) {
  const t = useTranslations("tradingControl");
  const elapsed = Math.min(
    Math.floor(probation.elapsed_ms / HOUR_MS),
    Math.floor(probation.required_ms / HOUR_MS),
  );
  const required = Math.floor(probation.required_ms / HOUR_MS);
  return (
    <div className="space-y-1 border-t pt-3">
      <h3 className="font-semibold">{t("probationTitle")}</h3>
      <p>
        {t("probationLimit", {
          percent: Number(probation.multiplier) * 100,
        })}
      </p>
      {Object.entries(probation.floor).map(([symbol, amount]) => (
        <p key={symbol} className="font-mono text-sm">
          {t("probationFloor", { symbol, amount })}
        </p>
      ))}
      <label className="block text-sm">
        {t("probationTime", { elapsed, required })}
        <progress
          className="block w-full"
          value={Math.min(probation.elapsed_ms, probation.required_ms)}
          max={probation.required_ms}
        />
      </label>
      <label className="block text-sm">
        {t("probationAcks", {
          count: probation.acknowledged,
          required: probation.required_acknowledged,
        })}
        <progress
          className="block w-full"
          value={Math.min(
            probation.acknowledged,
            probation.required_acknowledged,
          )}
          max={probation.required_acknowledged}
        />
      </label>
      <p className="text-sm text-muted-foreground">{t("probationLift")}</p>
    </div>
  );
}

function AwaitingApproval({ data }: { data: TradingControlOverview }) {
  const t = useTranslations("tradingControl");
  return (
    <div className="space-y-1 border-t pt-3">
      <h3 className="font-semibold">{t("awaitingTitle")}</h3>
      <p>{t("awaitingBody")}</p>
      <dl className="grid gap-1 text-sm">
        <dt>{t("digest")}</dt>
        <dd className="break-all font-mono">
          {data.running.backend_digest ?? "—"}
        </dd>
        <dt>{t("revision")}</dt>
        <dd className="break-all font-mono">
          {data.running.source_revision ?? "—"}
        </dd>
        <dt>{t("changeClass")}</dt>
        <dd>{data.running.change_class ?? "—"}</dd>
      </dl>
    </div>
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
            `error.${code as "request_pending" | "backend_digest_required" | "forbidden"}`,
          )
        : t("error.generic", { code })}
    </p>
  );
}
