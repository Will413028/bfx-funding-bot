import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@/lib/test-utils";

const auth = vi.hoisted(() => ({
  enable: vi.fn(),
  verifyTotp: vi.fn(),
  getSession: vi.fn(),
}));

vi.mock("@/lib/auth-client", () => ({
  authClient: {
    twoFactor: {
      enable: auth.enable,
      verifyTotp: auth.verifyTotp,
    },
    getSession: auth.getSession,
  },
}));

import { TwoFactorEnrollment } from "../two-factor-enrollment";

const enrollmentData = {
  totpURI:
    "otpauth://totp/BFX%20Funding%20Bot:fixture@example.com?secret=FIXTURESECRET&issuer=BFX%20Funding%20Bot",
  backupCodes: ["fixture-backup-code", "fixture-second-code"],
};

function submitPassword(password = "fixture-password") {
  fireEvent.change(screen.getByLabelText("Current password"), {
    target: { value: password },
  });
  fireEvent.submit(
    screen
      .getByRole("button", { name: "Set up two-factor authentication" })
      .closest("form") as HTMLFormElement,
  );
}

async function advanceToCodeEntry() {
  auth.enable.mockResolvedValue({ data: enrollmentData, error: null });
  submitPassword();
  await screen.findByText("fixture-backup-code");
  fireEvent.click(
    screen.getByRole("checkbox", { name: "I saved my backup codes" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Continue to verification" }),
  );
}

function submitCode(code = "012345") {
  fireEvent.change(screen.getByLabelText("Six-digit authentication code"), {
    target: { value: code },
  });
  fireEvent.submit(
    screen
      .getByRole("button", { name: "Verify and finish" })
      .closest("form") as HTMLFormElement,
  );
}

describe("TwoFactorEnrollment", () => {
  afterEach(cleanup);

  beforeEach(() => {
    auth.enable.mockReset();
    auth.verifyTotp.mockReset();
    auth.getSession.mockReset();
  });

  it("shows status without offering enrollment when already enrolled", () => {
    render(<TwoFactorEnrollment enrolled />);

    expect(
      screen.getByText("Two-factor authentication is enabled"),
    ).toBeDefined();
    expect(
      screen.queryByRole("button", {
        name: "Set up two-factor authentication",
      }),
    ).toBeNull();
  });

  it("submits the current password and shows a stable failure", async () => {
    auth.enable.mockResolvedValue({
      data: null,
      error: { message: "fixture-sensitive-password-error" },
    });
    render(<TwoFactorEnrollment enrolled={false} />);

    submitPassword();

    await waitFor(() =>
      expect(auth.enable).toHaveBeenCalledWith({
        password: "fixture-password",
      }),
    );
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Could not start two-factor setup. Check your password and try again.",
    );
    expect(screen.queryByText("fixture-sensitive-password-error")).toBeNull();
    expect(
      (screen.getByLabelText("Current password") as HTMLInputElement).value,
    ).toBe("");
    expect(auth.verifyTotp).not.toHaveBeenCalled();
  });

  it("renders the TOTP URI as a local SVG and clears all secrets after acknowledgment", async () => {
    auth.enable.mockResolvedValue({ data: enrollmentData, error: null });
    render(<TwoFactorEnrollment enrolled={false} />);

    submitPassword();
    await screen.findByText("fixture-backup-code");

    const qr = screen.queryByTitle("Authenticator QR code");
    expect(qr?.closest("svg")?.tagName.toLowerCase()).toBe("svg");
    fireEvent.click(
      screen.getByRole("checkbox", { name: "I saved my backup codes" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Continue to verification" }),
    );
    expect(screen.queryByText("fixture-backup-code")).toBeNull();
    expect(screen.queryByText(enrollmentData.totpURI)).toBeNull();
    expect(screen.queryByTitle("Authenticator QR code")).toBeNull();
    expect(
      screen.getByLabelText("Six-digit authentication code"),
    ).toBeDefined();
  });

  it("accepts a leading-zero six-digit code only and completes after a fresh enrolled session", async () => {
    auth.verifyTotp.mockResolvedValue({ data: { status: true }, error: null });
    auth.getSession.mockResolvedValue({
      data: { user: { twoFactorEnabled: true } },
      error: null,
    });
    render(<TwoFactorEnrollment enrolled={false} />);
    await advanceToCodeEntry();

    fireEvent.change(screen.getByLabelText("Six-digit authentication code"), {
      target: { value: "12345" },
    });
    fireEvent.submit(
      screen
        .getByRole("button", { name: "Verify and finish" })
        .closest("form") as HTMLFormElement,
    );
    expect(auth.verifyTotp).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toBe(
      "Enter a six-digit code.",
    );

    fireEvent.change(screen.getByLabelText("Six-digit authentication code"), {
      target: { value: "012345" },
    });
    fireEvent.submit(
      screen
        .getByRole("button", { name: "Verify and finish" })
        .closest("form") as HTMLFormElement,
    );

    await waitFor(() =>
      expect(auth.verifyTotp).toHaveBeenCalledWith({
        code: "012345",
        trustDevice: false,
      }),
    );
    expect(auth.getSession).toHaveBeenCalledWith({
      query: { disableCookieCache: true },
    });
    expect(
      await screen.findByText("Two-factor authentication is enabled"),
    ).toBeDefined();
    expect(screen.queryByText("fixture-backup-code")).toBeNull();
  });

  it("keeps verification active after failure without redisplaying secrets", async () => {
    auth.verifyTotp.mockResolvedValue({
      data: null,
      error: { message: "fixture-sensitive-verification-error" },
    });
    render(<TwoFactorEnrollment enrolled={false} />);
    await advanceToCodeEntry();

    fireEvent.change(screen.getByLabelText("Six-digit authentication code"), {
      target: { value: "000000" },
    });
    fireEvent.submit(
      screen
        .getByRole("button", { name: "Verify and finish" })
        .closest("form") as HTMLFormElement,
    );

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Invalid authentication code. Try again.",
    );
    expect(
      screen.getByLabelText("Six-digit authentication code"),
    ).toBeDefined();
    expect(screen.queryByText("fixture-backup-code")).toBeNull();
    expect(
      screen.queryByText("fixture-sensitive-verification-error"),
    ).toBeNull();
    expect(auth.getSession).not.toHaveBeenCalled();
  });

  it("does not complete when verification lacks authoritative enrolled state", async () => {
    auth.verifyTotp.mockResolvedValue({ data: { status: true }, error: null });
    auth.getSession.mockResolvedValue({
      data: { user: { twoFactorEnabled: false } },
      error: null,
    });
    render(<TwoFactorEnrollment enrolled={false} />);
    await advanceToCodeEntry();

    submitCode();

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Verification could not be confirmed. Sign in again and retry.",
    );
    expect(
      screen.queryByText("Two-factor authentication is enabled"),
    ).toBeNull();
    expect(screen.queryByLabelText("Current password")).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: "Set up two-factor authentication",
      }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Verify and finish" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel setup" })).toBeNull();
  });

  it("blocks cancellation while verification is in flight and honors the committed result", async () => {
    let resolveVerify:
      | ((value: { data: { status: true }; error: null }) => void)
      | undefined;
    auth.verifyTotp.mockReturnValue(
      new Promise((resolve) => {
        resolveVerify = resolve;
      }),
    );
    auth.getSession.mockResolvedValue({
      data: { user: { twoFactorEnabled: true } },
      error: null,
    });
    render(<TwoFactorEnrollment enrolled={false} />);
    await advanceToCodeEntry();

    submitCode();
    await waitFor(() => expect(auth.verifyTotp).toHaveBeenCalledOnce());

    const cancel = screen.getByRole("button", { name: "Cancel setup" });
    expect((cancel as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(cancel);
    expect(screen.queryByLabelText("Current password")).toBeNull();

    resolveVerify?.({ data: { status: true }, error: null });

    expect(
      await screen.findByText("Two-factor authentication is enabled"),
    ).toBeDefined();
    expect(screen.queryByLabelText("Current password")).toBeNull();
  });

  it("requires a fresh sign-in when session confirmation rejects after successful verification", async () => {
    auth.verifyTotp.mockResolvedValue({ data: { status: true }, error: null });
    auth.getSession.mockRejectedValue(new Error("fixture-session-transport"));
    render(<TwoFactorEnrollment enrolled={false} />);
    await advanceToCodeEntry();

    submitCode();

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Verification could not be confirmed. Sign in again and retry.",
    );
    expect(screen.queryByLabelText("Current password")).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: "Set up two-factor authentication",
      }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Verify and finish" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel setup" })).toBeNull();
  });

  it("requires a fresh sign-in when verification transport outcome is unknown", async () => {
    auth.verifyTotp.mockRejectedValue(new Error("fixture-verify-transport"));
    render(<TwoFactorEnrollment enrolled={false} />);
    await advanceToCodeEntry();

    submitCode();

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Verification could not be confirmed. Sign in again and retry.",
    );
    expect(screen.queryByLabelText("Current password")).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: "Set up two-factor authentication",
      }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Verify and finish" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel setup" })).toBeNull();
    expect(auth.getSession).not.toHaveBeenCalled();
  });

  it("ignores an in-flight enable result after cancellation", async () => {
    let resolveEnable: ((value: unknown) => void) | undefined;
    auth.enable.mockReturnValue(
      new Promise((resolve) => {
        resolveEnable = resolve;
      }),
    );
    render(<TwoFactorEnrollment enrolled={false} />);

    submitPassword();
    fireEvent.click(screen.getByRole("button", { name: "Cancel setup" }));
    resolveEnable?.({ data: enrollmentData, error: null });

    await waitFor(() => expect(auth.enable).toHaveBeenCalledOnce());
    expect(screen.getByLabelText("Current password")).toBeDefined();
    expect(screen.queryByText("fixture-backup-code")).toBeNull();
    expect(screen.queryByTitle("Authenticator QR code")).toBeNull();
  });
});
