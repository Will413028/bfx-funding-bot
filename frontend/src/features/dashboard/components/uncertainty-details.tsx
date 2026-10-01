"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { DecimalAmount } from "@/components/shared/decimal-amount";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useResolveUncertainty } from "@/features/dashboard/hooks/use-uncertainties";
import { useUser } from "@/features/settings/hooks/use-user";
import type { Uncertainty } from "@/types";

interface UncertaintyDetailsProps {
  uncertainty: Uncertainty;
}

function actionKey(kind: string): "bind" | "notAccepted" | "manual" {
  if (kind === "submit_outcome_unknown") return "bind";
  if (kind === "unattributed_venue_offer") return "manual";
  return "notAccepted";
}

/** Evidence-only detail view. It contains no venue-submit retry control. */
export function UncertaintyDetails({ uncertainty }: UncertaintyDetailsProps) {
  const t = useTranslations("overview");
  const user = useUser();
  const resolution = useResolveUncertainty(
    uncertainty.blockedScope.exchangeAccountId,
  );
  // The web API only queues the adjudication; the account daemon applies it.
  // The row's newest request is the one status source -- this tab's, another
  // tab's, or one found on reload -- and every action waits for its outcome.
  const latestRequest = uncertainty.resolutionRequest ?? null;
  const isQueued = latestRequest?.state === "requested";
  const isAwaitingDaemon = resolution.isPending || isQueued;
  const notApplied =
    latestRequest?.state === "rejected" || latestRequest?.state === "failed"
      ? (latestRequest.outcomeReason ?? latestRequest.state)
      : null;
  const [manualDecision, setManualDecision] = useState("");
  const [manualReason, setManualReason] = useState("");
  const observedAt = uncertainty.evidenceSummary.observedAtMs;
  const action = actionKey(uncertainty.kind);
  const context = uncertainty.resolutionContext;
  const operatorUuid = user.data?.id;
  const evidenceRef = context?.evidenceRef;
  const hasFreshContext =
    context !== null &&
    context !== undefined &&
    context.unavailableReason === null &&
    typeof evidenceRef === "string" &&
    evidenceRef.length > 0;
  const canSubmit =
    hasFreshContext && Boolean(operatorUuid) && !isAwaitingDaemon;
  const isSubmitUnknown = uncertainty.kind === "submit_outcome_unknown";
  const isManual =
    uncertainty.kind === "unattributed_venue_offer" ||
    uncertainty.kind === "unsupported_venue_exposure";

  function commonInput() {
    if (
      !operatorUuid ||
      typeof evidenceRef !== "string" ||
      evidenceRef.length === 0
    )
      return null;
    return {
      uncertaintyId: uncertainty.uncertaintyId,
      evidenceRef,
      operatorUuid,
    };
  }

  function bindExactCandidate() {
    const common = commonInput();
    const venueOfferId = context?.candidateVenueOfferIds[0];
    if (!common || context?.candidateCount !== 1 || !venueOfferId) return;
    resolution.mutate({
      ...common,
      action: "bind-to-venue",
      venueOfferId,
    });
  }

  function markNotAccepted() {
    const common = commonInput();
    if (!common || context?.candidateCount !== 0) return;
    resolution.mutate({
      ...common,
      action: "mark-not-accepted",
    });
  }

  function recordManualResolution() {
    const common = commonInput();
    const reason = manualReason.trim();
    if (!common || !isManual || !manualDecision || !reason) return;
    resolution.mutate({
      ...common,
      action: "manual-resolution",
      reason,
      evidence: { decision: manualDecision },
    });
  }

  return (
    <details className="mt-2 rounded-lg border border-amber-500/20 bg-black/10 p-3">
      <summary className="cursor-pointer text-xs font-medium text-amber-200">
        {t("uncertaintyDetails")}
      </summary>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <div>
          <dt className="text-zinc-500">{t("uncertaintyAmount")}</dt>
          <dd className="mt-0.5 text-zinc-200">
            <DecimalAmount value={uncertainty.intendedAmount} />
          </dd>
        </div>
        <div>
          <dt className="text-zinc-500">{t("uncertaintyScope")}</dt>
          <dd className="mt-0.5 text-zinc-200">
            {uncertainty.blockedScope.environment}
          </dd>
        </div>
        <div>
          <dt className="text-zinc-500">{t("uncertaintyEvidence")}</dt>
          <dd className="mt-0.5 text-zinc-200">
            {uncertainty.evidenceSummary.outcomeReason ?? uncertainty.kind}
          </dd>
        </div>
        <div>
          <dt className="text-zinc-500">{t("uncertaintyObserved")}</dt>
          <dd className="mt-0.5 text-zinc-200">
            {typeof observedAt === "number" ? (
              <time dateTime={new Date(observedAt).toISOString()}>
                {new Date(observedAt).toLocaleString()}
              </time>
            ) : (
              "—"
            )}
          </dd>
        </div>
      </dl>
      <p className="mt-3 text-xs leading-relaxed text-amber-100/80">
        {t(`uncertaintyNextAction.${action}`)}
      </p>
      {context?.candidateVenueOfferIds.length ? (
        <div className="mt-3 text-xs">
          <p className="text-zinc-500">{t("uncertaintyCandidates")}</p>
          <ul className="mt-1 space-y-1 text-zinc-200">
            {context.candidateVenueOfferIds.map((venueOfferId) => (
              <li key={venueOfferId}>
                <code>{venueOfferId}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {context?.unavailableReason ? (
        <output className="mt-3 block text-xs text-amber-300">
          {t("uncertaintyResolutionUnavailable")}:{" "}
          {context.unavailableReason.replaceAll("_", " ")}
        </output>
      ) : null}
      {isSubmitUnknown &&
      context?.candidateCount === 1 &&
      !context.unavailableReason ? (
        <Button
          type="button"
          size="sm"
          className="mt-3"
          disabled={!canSubmit || context.candidateVenueOfferIds.length !== 1}
          onClick={bindExactCandidate}
        >
          {t("uncertaintyBind")}
        </Button>
      ) : null}
      {isSubmitUnknown &&
      context?.candidateCount === 0 &&
      !context.unavailableReason ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="mt-3"
          disabled={!canSubmit}
          onClick={markNotAccepted}
        >
          {t("uncertaintyConfirmNotAccepted")}
        </Button>
      ) : null}
      {isManual && hasFreshContext ? (
        <div className="mt-3 space-y-3 border-t border-amber-500/20 pt-3">
          <div className="space-y-1">
            <Label htmlFor={`decision-${uncertainty.uncertaintyId}`}>
              {t("uncertaintyManualDecision")}
            </Label>
            <select
              id={`decision-${uncertainty.uncertaintyId}`}
              value={manualDecision}
              onChange={(event) => setManualDecision(event.target.value)}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
            >
              <option value="">
                {t("uncertaintyManualDecisionPlaceholder")}
              </option>
              <option value="accepted_external_exposure">
                {t("uncertaintyManualDecisionAccepted")}
              </option>
              <option value="closed_at_venue">
                {t("uncertaintyManualDecisionClosed")}
              </option>
            </select>
          </div>
          <div className="space-y-1">
            <Label htmlFor={`reason-${uncertainty.uncertaintyId}`}>
              {t("uncertaintyOperatorReason")}
            </Label>
            <Input
              id={`reason-${uncertainty.uncertaintyId}`}
              value={manualReason}
              maxLength={512}
              onChange={(event) => setManualReason(event.target.value)}
            />
          </div>
          <Button
            type="button"
            size="sm"
            disabled={!canSubmit || !manualDecision || !manualReason.trim()}
            onClick={recordManualResolution}
          >
            {t("uncertaintyRecordManual")}
          </Button>
        </div>
      ) : null}
      {!operatorUuid ? (
        <output className="mt-3 block text-xs text-amber-300">
          {t("uncertaintyOperatorUnavailable")}
        </output>
      ) : null}
      {isQueued ? (
        <output className="mt-3 block text-xs text-amber-200">
          {t("uncertaintyResolutionPending")}
        </output>
      ) : null}
      {notApplied ? (
        <p className="mt-3 text-xs text-rose-400" role="alert">
          {t("uncertaintyResolutionNotApplied")}:{" "}
          {notApplied.replaceAll("_", " ")}
        </p>
      ) : null}
      {resolution.isError ? (
        <p className="mt-3 text-xs text-rose-400" role="alert">
          {resolution.error?.message ?? t("uncertaintyResolutionFailed")}
        </p>
      ) : null}
    </details>
  );
}
