import { beforeEach, describe, expect, it } from "vitest";

import {
  readTerminalTimeRange,
  readThemeSkin,
  writeTerminalTimeRange,
  writeThemeSkin
} from "../src/userPreferences";

beforeEach(() => {
  window.localStorage.clear();
});

describe("userPreferences terminal time range", () => {
  it("defaults terminal lists to 7 days", () => {
    expect(readTerminalTimeRange()).toBe("7d");
  });

  it("persists a supported terminal time range", () => {
    writeTerminalTimeRange("14d");

    expect(readTerminalTimeRange()).toBe("14d");
  });

  it("ignores unsupported stored terminal time ranges", () => {
    window.localStorage.setItem("web-terminal-acp:terminal-time-range", "2d");

    expect(readTerminalTimeRange()).toBe("7d");
  });
});

describe("userPreferences theme skin", () => {
  it("defaults to the existing application skin", () => {
    expect(readThemeSkin()).toBe("default");
  });

  it("persists a supported theme skin", () => {
    writeThemeSkin("raycast");

    expect(readThemeSkin()).toBe("raycast");
  });

  it("ignores unsupported stored theme skins", () => {
    window.localStorage.setItem("web-terminal-acp:theme-skin", "unsupported");

    expect(readThemeSkin()).toBe("default");
  });
});
