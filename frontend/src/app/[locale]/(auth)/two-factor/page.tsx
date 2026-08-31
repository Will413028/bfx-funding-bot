import { Suspense } from "react";
import { TwoFactorForm } from "@/features/auth/components/two-factor-form";

export default function TwoFactorPage() {
  return (
    <Suspense fallback={null}>
      <TwoFactorForm />
    </Suspense>
  );
}
