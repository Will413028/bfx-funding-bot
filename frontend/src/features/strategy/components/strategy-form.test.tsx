import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it } from "vitest";
import { render, screen } from "@/lib/test-utils";
import { StrategyForm } from "./strategy-form";

it("labels draft edits as separate from applied funding policy", () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <StrategyForm userConfig={null} />
    </QueryClientProvider>,
  );
  expect(
    screen.getByText(
      /Saving this draft does not change the applied capital policy/,
    ),
  ).toBeTruthy();
});
