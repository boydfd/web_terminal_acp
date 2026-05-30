import { beforeEach, describe, expect, it } from "vitest";

import {
  readThemeSkin,
  writeThemeSkin
} from "../src/userPreferences";

beforeEach(() => {
  window.localStorage.clear();
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
