import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@/lib/test-utils";
import { SetupChecklist } from "../setup-checklist";

// Mock next/navigation
vi.mock("next/navigation", () => ({
  useParams: () => ({ locale: "en" }),
}));

describe("SetupChecklist", () => {
  it("renders both steps when nothing is complete", () => {
    render(<SetupChecklist hasVerifiedKey={false} hasStrategy={false} />);

    expect(screen.getByText("Get started")).toBeDefined();
    expect(screen.getByText("0/2")).toBeDefined();
    expect(screen.getByText("Connect your Bitfinex API key")).toBeDefined();
    expect(screen.getByText("Configure your lending strategy")).toBeDefined();
  });

  it("shows progress 1/2 when API key is verified", () => {
    render(<SetupChecklist hasVerifiedKey={true} hasStrategy={false} />);

    expect(screen.getByText("1/2")).toBeDefined();
  });

  it("returns null when all steps are complete", () => {
    const { container } = render(
      <SetupChecklist hasVerifiedKey={true} hasStrategy={true} />,
    );

    expect(container.innerHTML).toBe("");
  });

  it("links to api-keys page for first step", () => {
    const { container } = render(
      <SetupChecklist hasVerifiedKey={false} hasStrategy={false} />,
    );

    const links = container.querySelectorAll("a");
    const apiKeyLink = Array.from(links).find((a) =>
      a.textContent?.includes("Connect your Bitfinex API key"),
    );
    expect(apiKeyLink?.getAttribute("href")).toBe("/en/api-keys");
  });
});
