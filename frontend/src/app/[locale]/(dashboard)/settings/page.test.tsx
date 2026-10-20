import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@/lib/test-utils";

const useUser = vi.hoisted(() => vi.fn());

vi.mock("@/features/settings/hooks/use-user", () => ({ useUser }));

import SettingsPage from "./page";

describe("SettingsPage", () => {
  afterEach(cleanup);

  beforeEach(() => {
    useUser.mockReset();
  });

  it("shows the load failure when the backend profile fails", () => {
    useUser.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch: vi.fn(),
    });

    render(<SettingsPage />);

    expect(screen.getByText("Failed to load user data")).toBeDefined();
  });

  it("shows the tailnet identity from the profile", () => {
    useUser.mockReturnValue({
      data: { userId: "4182", email: "op@github", role: "operator" },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<SettingsPage />);

    expect(screen.getByText("op@github")).toBeDefined();
    expect(screen.getByText("4182")).toBeDefined();
    expect(screen.getByText("operator")).toBeDefined();
  });
});
