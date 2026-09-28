import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Testing Library only auto-registers cleanup when test globals are enabled,
// which this config does not do. Without it, components stay mounted after a
// test and React's scheduler can run after jsdom is torn down, failing the run
// with "window is not defined".
afterEach(() => {
	cleanup();
});
