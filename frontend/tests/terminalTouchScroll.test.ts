import { describe, expect, it } from "vitest";

import {
  terminalTouchScrollPosition,
  terminalTouchScrollSequence,
} from "../src/terminalTouchScroll";

const hostRect = {
  left: 0,
  top: 0,
  width: 400,
  height: 300,
};

describe("terminalTouchScroll", () => {
  it("maps touch coordinates to one-based tmux cell coordinates", () => {
    expect(terminalTouchScrollPosition({
      clientX: 200,
      clientY: 120,
      hostRect,
      cols: 80,
      rows: 24,
    })).toEqual({ col: 41, row: 10 });
  });

  it("converts upward finger movement into tmux wheel-down events", () => {
    expect(terminalTouchScrollSequence({
      deltaY: 60,
      clientX: 200,
      clientY: 120,
      hostRect,
      cols: 80,
      rows: 24,
      cellHeight: 12.5,
      maxWheelEvents: 12,
    })).toEqual({
      sequence: "\x1b[<65;41;10M\x1b[<65;41;10M\x1b[<65;41;10M\x1b[<65;41;10M",
      consumedY: 50,
    });
  });

  it("converts downward finger movement into tmux wheel-up events", () => {
    expect(terminalTouchScrollSequence({
      deltaY: -25,
      clientX: 200,
      clientY: 180,
      hostRect,
      cols: 80,
      rows: 24,
      cellHeight: 12.5,
      maxWheelEvents: 12,
    })).toEqual({
      sequence: "\x1b[<64;41;15M\x1b[<64;41;15M",
      consumedY: -25,
    });
  });

  it("caps generated wheel events per pointer move", () => {
    const result = terminalTouchScrollSequence({
      deltaY: 1000,
      clientX: 200,
      clientY: 120,
      hostRect,
      cols: 80,
      rows: 24,
      cellHeight: 10,
      maxWheelEvents: 12,
    });

    expect(result?.sequence.match(/\x1b\[<65;41;10M/g)).toHaveLength(12);
    expect(result?.consumedY).toBe(120);
  });
});
