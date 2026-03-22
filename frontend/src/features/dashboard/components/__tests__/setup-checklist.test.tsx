import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@/lib/test-utils";
import { SetupChecklist } from "../setup-checklist";

// Mock next/navigation
vi.mock("next/navigation", () => ({
  useParams: () => ({ locale: "en" }),
}));

describe("SetupChecklist", () => {
  it("renders all 3 steps when nothing is complete", () => {
    render(
      <SetupChecklist
        hasVerifiedKey={false}
        hasStrategy={false}
        engineReady={false}
      />,
    );

    expect(screen.getByText("Get started")).toBeDefined();
    expect(screen.getByText("0/3")).toBeDefined();
    expect(screen.getByText("Connect your Bitfinex API key")).toBeDefined();
    expect(screen.getByText("Configure your lending strategy")).toBeDefined();
    expect(screen.getByText("Start earning")).toBeDefined();
  });

  it("shows progress 1/3 when API key is verified", () => {
    render(
      <SetupChecklist
        hasVerifiedKey={true}
        hasStrategy={false}
        engineReady={false}
      />,
    );

    expect(screen.getByText("1/3")).toBeDefined();
  });

  it("shows progress 2/3 when key + strategy done", () => {
    render(
      <SetupChecklist
        hasVerifiedKey={true}
        hasStrategy={true}
        engineReady={false}
      />,
    );

    expect(screen.getByText("2/3")).toBeDefined();
  });

  it("returns null when all steps are complete", () => {
    const { container } = render(
      <SetupChecklist
        hasVerifiedKey={true}
        hasStrategy={true}
        engineReady={true}
      />,
    );

    expect(container.innerHTML).toBe("");
  });

  it("links to api-keys page for first step", () => {
    const { container } = render(
      <SetupChecklist
        hasVerifiedKey={false}
        hasStrategy={false}
        engineReady={false}
      />,
    );

    const links = container.querySelectorAll("a");
    const apiKeyLink = Array.from(links).find((a) =>
      a.textContent?.includes("Connect your Bitfinex API key"),
    );
    expect(apiKeyLink?.getAttribute("href")).toBe("/en/api-keys");
  });
});
