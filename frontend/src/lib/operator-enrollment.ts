import { auth } from "@/lib/auth";

export type OperatorEnrollmentState =
  | { allowed: false }
  | { allowed: true; enrolled: boolean };

export async function getOperatorEnrollmentState(
  requestHeaders: Headers,
  operatorUserId: string | undefined,
): Promise<OperatorEnrollmentState> {
  if (!operatorUserId) return { allowed: false };

  try {
    const session = await auth.api.getSession({
      headers: requestHeaders,
      query: { disableCookieCache: true },
    });
    if (
      !session ||
      session.user.id !== operatorUserId ||
      (session.user as { banned?: boolean }).banned === true
    ) {
      return { allowed: false };
    }

    return {
      allowed: true,
      enrolled:
        (session.user as { twoFactorEnabled?: boolean }).twoFactorEnabled ===
        true,
    };
  } catch {
    return { allowed: false };
  }
}
