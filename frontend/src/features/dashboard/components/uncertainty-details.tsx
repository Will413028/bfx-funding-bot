"use client";

import { useTranslations } from "next-intl";
import { DecimalAmount } from "@/components/shared/decimal-amount";
import type { Uncertainty } from "@/types";

interface UncertaintyDetailsProps {
  uncertainty: Uncertainty;
}

function actionKey(kind: string): "bind" | "notAccepted" | "manual" {
  if (kind === "submit_outcome_unknown") return "bind";
  if (kind === "unattributed_venue_offer") return "manual";
  return "notAccepted";
}

function adjudicationCommand(uncertaintyId: string): string {
  return `sudo bfx-op bind-to-venue|mark-not-accepted|manual-resolution ${uncertaintyId} --evidence-ref …`;
}

/**
 * Evidence-only detail view. It contains no venue-submit retry control and no
 * verdict control: adjudications are queued only by the operator CLI (ADR
 * 2026-10-15 D4). A request the CLI queued still shows here until the account
 * daemon settles it.
 */
export function UncertaintyDetails({ uncertainty }: UncertaintyDetailsProps) {
  const t = useTranslations("overview");
  // The row's newest request is the one status source.
  const latestRequest = uncertainty.resolutionRequest ?? null;
  const isQueued = latestRequest?.state === "requested";
  const notApplied =
    latestRequest?.state === "rejected" || latestRequest?.state === "failed"
      ? (latestRequest.outcomeReason ?? latestRequest.state)
      : null;
  const observedAt = uncertainty.evidenceSummary.observedAtMs;
  const action = actionKey(uncertainty.kind);
  const context = uncertainty.resolutionContext;

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
      <p className="mt-2 text-xs text-zinc-400">
        {t("uncertaintyCli")}{" "}
        <code className="break-all text-zinc-200">
          {adjudicationCommand(uncertainty.uncertaintyId)}
        </code>
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
    </details>
  );
}
