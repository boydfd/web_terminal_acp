import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

function cssRuleBody(selector: string): string {
  const css = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "../src/styles.css"),
    "utf8"
  );
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`).exec(css);
  if (!match) {
    throw new Error(`Missing CSS rule for ${selector}`);
  }
  return match[1];
}

describe("notification center layout", () => {
  it("opens the notification list on the left side with the sidebar bell", () => {
    expect(cssRuleBody(".notification-center-backdrop")).toContain("justify-content: flex-start");
  });
});
