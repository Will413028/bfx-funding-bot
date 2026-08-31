import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@/lib/test-utils";

const auth = vi.hoisted(() => ({
  verifyTotp: vi.fn(),
}));
const router = vi.hoisted(() => ({
  push: vi.fn(),
  refresh: vi.fn(),
}));
const searchParams = vi.hoisted(() => ({
  get: vi.fn(() => "/overview"),
}));

vi.mock("@/lib/auth-client", () => ({
  authClient: { twoFactor: { verifyTotp: auth.verifyTotp } },
}));
vi.mock("@/i18n/navigation", () => ({
  useRouter: () => router,
}));
vi.mock("next/navigation", () => ({
  useSearchParams: () => searchParams,
}));

import { TwoFactorForm } from "../two-factor-form";

describe("TwoFactorForm", () => {
  afterEach(cleanup);

  beforeEach(() => {
    auth.verifyTotp.mockReset();
    router.push.mockReset();
    router.refresh.mockReset();
    searchParams.get.mockReturnValue("/overview");
  });

  it("submits the code and redirects after successful verification", async () => {
    auth.verifyTotp.mockResolvedValue({ data: { status: true }, error: null });
    render(<TwoFactorForm />);

    fireEvent.change(screen.getByLabelText("Authentication code"), {
      target: { value: "123456" },
    });
    const form = screen.getByRole("button", { name: "Verify" }).closest("form");
    expect(form).not.toBeNull();
    if (!form) return;
    fireEvent.submit(form);

    await waitFor(() =>
      expect(auth.verifyTotp).toHaveBeenCalledWith({ code: "123456" }),
    );
    expect(router.push).toHaveBeenCalledWith("/overview");
    expect(router.refresh).toHaveBeenCalledOnce();
  });

  it("shows a stable error and does not redirect when verification fails", async () => {
    auth.verifyTotp.mockResolvedValue({
      data: null,
      error: { message: "INVALID_CODE" },
    });
    render(<TwoFactorForm />);

    fireEvent.change(screen.getByLabelText("Authentication code"), {
      target: { value: "000000" },
    });
    const form = screen.getByRole("button", { name: "Verify" }).closest("form");
    expect(form).not.toBeNull();
    if (!form) return;
    fireEvent.submit(form);

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Invalid authentication code",
    );
    expect(router.push).not.toHaveBeenCalled();
  });
});
