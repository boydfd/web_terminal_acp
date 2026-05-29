import { afterEach, describe, expect, it, vi } from "vitest";

import { fitTerminalToContainer } from "../src/terminalFit";

type MockTerminal = {
  cols: number;
  rows: number;
  options: { scrollback: number };
  element: HTMLElement;
  resize: ReturnType<typeof vi.fn>;
  clearTextureAtlas: ReturnType<typeof vi.fn>;
  refresh: ReturnType<typeof vi.fn>;
  _core: {
    _renderService: {
      dimensions: {
        css: {
          cell: { width: number; height: number };
        };
      };
    };
    viewport: { scrollBarWidth: number };
  };
};

function rect(width: number, height: number): DOMRect {
  return {
    x: 0,
    y: 0,
    width,
    height,
    top: 0,
    left: 0,
    right: width,
    bottom: height,
    toJSON: () => ({})
  } as DOMRect;
}

function createContainer(width: number, height: number): HTMLElement {
  const container = document.createElement("div");
  container.style.padding = "0px";
  Object.defineProperty(container, "clientWidth", { configurable: true, value: width });
  Object.defineProperty(container, "clientHeight", { configurable: true, value: height });
  container.getBoundingClientRect = () => rect(width, height);
  document.body.appendChild(container);
  return container;
}

function createTerminal(renderedHeight: number): MockTerminal {
  const element = document.createElement("div");
  const screen = document.createElement("div");
  const canvas = document.createElement("canvas");
  screen.className = "xterm-screen";
  canvas.getBoundingClientRect = () => rect(800, renderedHeight);
  screen.appendChild(canvas);
  element.appendChild(screen);

  const terminal: MockTerminal = {
    cols: 80,
    rows: 24,
    options: { scrollback: 1000 },
    element,
    resize: vi.fn((cols: number, rows: number) => {
      terminal.cols = cols;
      terminal.rows = rows;
    }),
    clearTextureAtlas: vi.fn(),
    refresh: vi.fn(),
    _core: {
      _renderService: {
        dimensions: {
          css: {
            cell: { width: 10, height: 10 }
          }
        }
      },
      viewport: { scrollBarWidth: 0 }
    }
  };
  return terminal;
}

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("terminalFit", () => {
  it("does not repeatedly force the same renderer resize while mobile viewport rendering is still stale", () => {
    const container = createContainer(800, 240);
    const terminal = createTerminal(80);

    expect(fitTerminalToContainer(terminal as never, container)).toBe(true);
    expect(terminal.resize).toHaveBeenCalledTimes(2);
    expect(terminal.resize).toHaveBeenNthCalledWith(1, 80, 23);
    expect(terminal.resize).toHaveBeenNthCalledWith(2, 80, 24);

    expect(fitTerminalToContainer(terminal as never, container)).toBe(true);

    expect(terminal.resize).toHaveBeenCalledTimes(2);
  });
});
