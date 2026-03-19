"use client";

import { Check, ChevronRight, KeyRound, Settings, Zap } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

interface SetupStep {
  id: string;
  title: string;
  description: string;
  href: string;
  icon: React.ElementType;
  done: boolean;
}

interface SetupChecklistProps {
  hasVerifiedKey: boolean;
  hasStrategy: boolean;
  engineReady: boolean;
}

export function SetupChecklist({
  hasVerifiedKey,
  hasStrategy,
  engineReady,
}: SetupChecklistProps) {
  const params = useParams();
  const locale = (params.locale as string) || "en";

  const steps: SetupStep[] = [
    {
      id: "api-key",
      title: "Connect your Bitfinex API key",
      description:
        "Add and verify your Bitfinex API key to allow the bot to manage funding offers on your behalf.",
      href: `/${locale}/api-keys`,
      icon: KeyRound,
      done: hasVerifiedKey,
    },
    {
      id: "strategy",
      title: "Configure your lending strategy",
      description:
        "Set your preferred currency, amount range, rate bounds, and lending period to match your risk profile.",
      href: `/${locale}/strategy`,
      icon: Settings,
      done: hasStrategy,
    },
    {
      id: "engine",
      title: "Start earning",
      description:
        "Once your API key is verified and strategy is configured, the lending engine will start working automatically.",
      href: `/${locale}/overview`,
      icon: Zap,
      done: engineReady,
    },
  ];

  const completedCount = steps.filter((s) => s.done).length;

  if (completedCount === steps.length) {
    return null; // All done — don't show checklist
  }

  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.02] p-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="font-semibold text-base">Get started</h2>
          <p className="mt-0.5 text-sm text-zinc-500">
            Complete these steps to start earning funding interest.
          </p>
        </div>
        <span className="text-sm text-zinc-500">
          {completedCount}/{steps.length}
        </span>
      </div>

      {/* Progress bar */}
      <div className="mb-5 h-1 overflow-hidden rounded-full bg-white/5">
        <div
          className="h-full rounded-full bg-emerald-500 transition-all duration-500"
          style={{ width: `${(completedCount / steps.length) * 100}%` }}
        />
      </div>

      <div className="space-y-3">
        {steps.map((step) => {
          const Icon = step.icon;
          const isNext = !step.done && steps.indexOf(step) === completedCount;

          return (
            <Link
              key={step.id}
              href={step.done ? "#" : step.href}
              className={`flex items-center gap-4 rounded-lg border p-4 transition-colors ${
                step.done
                  ? "border-emerald-500/20 bg-emerald-500/5 cursor-default"
                  : isNext
                    ? "border-white/10 bg-white/[0.03] hover:bg-white/[0.05]"
                    : "border-white/5 bg-white/[0.01] opacity-50 cursor-default"
              }`}
              onClick={(e) => {
                if (step.done || (!isNext && !step.done)) e.preventDefault();
              }}
            >
              <div
                className={`flex size-9 shrink-0 items-center justify-center rounded-full ${
                  step.done
                    ? "bg-emerald-500/20 text-emerald-400"
                    : "bg-white/5 text-zinc-500"
                }`}
              >
                {step.done ? (
                  <Check className="size-4" />
                ) : (
                  <Icon className="size-4" />
                )}
              </div>

              <div className="min-w-0 flex-1">
                <p
                  className={`text-sm font-medium ${step.done ? "text-emerald-400" : "text-zinc-200"}`}
                >
                  {step.title}
                </p>
                <p className="mt-0.5 text-xs text-zinc-500 leading-relaxed">
                  {step.description}
                </p>
              </div>

              {isNext && (
                <ChevronRight className="size-4 shrink-0 text-zinc-500" />
              )}
            </Link>
          );
        })}
      </div>
    </div>
  );
}
