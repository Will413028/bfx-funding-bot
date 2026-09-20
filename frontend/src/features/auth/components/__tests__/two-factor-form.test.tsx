import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@/lib/test-utils";

const auth = vi.hoisted(() => ({
  verifyTotp: vi.fn(),
  verifyBackupCode: vi.fn(),
}));
const router = vi.hoisted(() => ({
  push: vi.fn(),
  refresh: vi.fn(),
}));
const searchParams = vi.hoisted(() => ({
  get: vi.fn(() => "/overview"),
}));

vi.mock("@/lib/auth-client", () => ({
  authClient: {
    twoFactor: {
      verifyTotp: auth.verifyTotp,
      verifyBackupCode: auth.verifyBackupCode,
    },
  },
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
    auth.verifyBackupCode.mockReset();
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
      expect(auth.verifyTotp).toHaveBeenCalledWith({
        code: "123456",
        trustDevice: false,
      }),
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

  it("uses a backup code without trusting the device and keeps the safe redirect", async () => {
    auth.verifyBackupCode.mockResolvedValue({
      data: { status: true },
      error: null,
    });
    render(<TwoFactorForm />);

    fireEvent.click(screen.getByRole("button", { name: "Use a backup code" }));
    fireEvent.change(screen.getByLabelText("Backup code"), {
      target: { value: " fixture-backup-code " },
    });
    const form = screen.getByRole("button", { name: "Verify" }).closest("form");
    expect(form).not.toBeNull();
    if (!form) return;
    fireEvent.submit(form);

    await waitFor(() =>
      expect(auth.verifyBackupCode).toHaveBeenCalledWith({
        code: "fixture-backup-code",
        trustDevice: false,
      }),
    );
    expect(auth.verifyTotp).not.toHaveBeenCalled();
    expect(router.push).toHaveBeenCalledWith("/overview");
    expect(router.refresh).toHaveBeenCalledOnce();
  });

  it("shows a stable recovery error without exposing the SDK error", async () => {
    auth.verifyBackupCode.mockResolvedValue({
      data: null,
      error: { message: "fixture-sensitive-sdk-error" },
    });
    render(<TwoFactorForm />);

    fireEvent.click(screen.getByRole("button", { name: "Use a backup code" }));
    fireEvent.change(screen.getByLabelText("Backup code"), {
      target: { value: "wrong-fixture-code" },
    });
    const form = screen.getByRole("button", { name: "Verify" }).closest("form");
    expect(form).not.toBeNull();
    if (!form) return;
    fireEvent.submit(form);

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Invalid backup code",
    );
    expect(screen.queryByText("fixture-sensitive-sdk-error")).toBeNull();
    expect(router.push).not.toHaveBeenCalled();
  });
});
