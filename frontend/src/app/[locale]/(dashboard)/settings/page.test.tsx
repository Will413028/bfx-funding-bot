import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@/lib/test-utils";

const useUser = vi.hoisted(() => vi.fn());

vi.mock("@/features/settings/hooks/use-user", () => ({ useUser }));
vi.mock("@/i18n/navigation", () => ({
  Link: ({
    href,
    children,
    ...props
  }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

import SettingsPage from "./page";

describe("SettingsPage", () => {
  afterEach(cleanup);

  beforeEach(() => {
    useUser.mockReset();
  });

  it("keeps the security enrollment link available when the backend profile fails", () => {
    useUser.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch: vi.fn(),
    });

    render(<SettingsPage />);

    expect(
      screen.getByRole("link", { name: "Security" }).getAttribute("href"),
    ).toBe("/settings/security");
    expect(screen.getByText("Failed to load user data")).toBeDefined();
  });
});
