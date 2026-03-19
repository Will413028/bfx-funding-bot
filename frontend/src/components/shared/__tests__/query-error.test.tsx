import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QueryError } from "../query-error";

describe("QueryError", () => {
  it("renders default message", () => {
    const { container } = render(<QueryError />);
    expect(container.textContent).toContain("Failed to load data");
  });

  it("renders custom message", () => {
    const { container } = render(<QueryError message="Custom error" />);
    expect(container.textContent).toContain("Custom error");
  });

  it("shows retry button when onRetry provided", () => {
    const { container } = render(<QueryError onRetry={() => {}} />);
    expect(container.querySelector("button")).not.toBeNull();
    expect(container.textContent).toContain("Try again");
  });

  it("hides retry button when no onRetry", () => {
    const { container } = render(<QueryError />);
    expect(container.querySelector("button")).toBeNull();
  });

  it("calls onRetry when button clicked", () => {
    const onRetry = vi.fn();
    const { container } = render(<QueryError onRetry={onRetry} />);
    const button = container.querySelector("button");
    expect(button).not.toBeNull();
    fireEvent.click(button!);
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
