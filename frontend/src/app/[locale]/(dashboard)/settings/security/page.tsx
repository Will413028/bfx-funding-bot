import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { TwoFactorEnrollment } from "@/features/settings/components/two-factor-enrollment";
import { getOperatorEnrollmentState } from "@/lib/operator-enrollment";

export default async function SecurityPage() {
  const state = await getOperatorEnrollmentState(
    await headers(),
    process.env.BFX_OPERATOR_USER_ID,
  );
  if (!state.allowed) notFound();

  return <TwoFactorEnrollment enrolled={state.enrolled} />;
}
