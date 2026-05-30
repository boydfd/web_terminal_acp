export type TerminalTouchScrollPosition = {
  col: number;
  row: number;
};

export type TerminalTouchScrollInput = {
  deltaY: number;
  clientX: number;
  clientY: number;
  hostRect: Pick<DOMRect, "left" | "top" | "width" | "height">;
  cols: number;
  rows: number;
  cellHeight: number;
  maxWheelEvents: number;
};

export type TerminalTouchScrollResult = {
  sequence: string;
  consumedY: number;
};

export const TMUX_MOUSE_WHEEL_UP = 64;
export const TMUX_MOUSE_WHEEL_DOWN = 65;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function terminalTouchScrollPosition(
  input: Omit<TerminalTouchScrollInput, "deltaY" | "cellHeight" | "maxWheelEvents">,
): TerminalTouchScrollPosition | null {
  const { hostRect, cols, rows, clientX, clientY } = input;
  if (hostRect.width <= 0 || hostRect.height <= 0 || cols <= 0 || rows <= 0) {
    return null;
  }

  return {
    col: clamp(Math.floor(((clientX - hostRect.left) / hostRect.width) * cols) + 1, 1, cols),
    row: clamp(Math.floor(((clientY - hostRect.top) / hostRect.height) * rows) + 1, 1, rows),
  };
}

export function tmuxSgrWheelSequence(button: number, position: TerminalTouchScrollPosition): string {
  return `\x1b[<${button};${position.col};${position.row}M`;
}

export function terminalTouchScrollSequence(input: TerminalTouchScrollInput): TerminalTouchScrollResult | null {
  if (input.cellHeight <= 0 || input.maxWheelEvents <= 0) {
    return null;
  }

  const position = terminalTouchScrollPosition(input);
  if (position === null) {
    return null;
  }

  const steps = clamp(
    Math.trunc(Math.abs(input.deltaY) / input.cellHeight),
    0,
    input.maxWheelEvents,
  );
  if (steps <= 0) {
    return null;
  }

  const button = input.deltaY > 0 ? TMUX_MOUSE_WHEEL_DOWN : TMUX_MOUSE_WHEEL_UP;
  return {
    sequence: tmuxSgrWheelSequence(button, position).repeat(steps),
    consumedY: steps * input.cellHeight * Math.sign(input.deltaY),
  };
}
