type TerminalOutputChunk = string | Uint8Array;

type TerminalOutputBufferOptions = {
  write: (data: TerminalOutputChunk) => void;
  schedule?: (callback: () => void) => number;
  cancel?: (handle: number) => void;
  maxFlushCharacters?: number;
};

const DEFAULT_MAX_FLUSH_CHARACTERS = 64 * 1024;

function chunkLength(chunk: TerminalOutputChunk): number {
  return typeof chunk === "string" ? chunk.length : chunk.byteLength;
}

function joinChunks(chunks: TerminalOutputChunk[]): TerminalOutputChunk[] {
  const joined: TerminalOutputChunk[] = [];
  let stringParts: string[] = [];
  let byteParts: Uint8Array[] = [];

  const flushStrings = () => {
    if (stringParts.length > 0) {
      joined.push(stringParts.join(""));
      stringParts = [];
    }
  };

  const flushBytes = () => {
    if (byteParts.length === 0) {
      return;
    }
    const totalLength = byteParts.reduce((total, chunk) => total + chunk.byteLength, 0);
    const merged = new Uint8Array(totalLength);
    let offset = 0;
    for (const chunk of byteParts) {
      merged.set(chunk, offset);
      offset += chunk.byteLength;
    }
    joined.push(merged);
    byteParts = [];
  };

  for (const chunk of chunks) {
    if (typeof chunk === "string") {
      flushBytes();
      stringParts.push(chunk);
    } else {
      flushStrings();
      byteParts.push(chunk);
    }
  }
  flushStrings();
  flushBytes();
  return joined;
}

function defaultSchedule(callback: () => void): number {
  return window.requestAnimationFrame(callback);
}

function defaultCancel(handle: number): void {
  window.cancelAnimationFrame(handle);
}

export function createTerminalOutputBuffer(options: TerminalOutputBufferOptions) {
  const queue: TerminalOutputChunk[] = [];
  const schedule = options.schedule ?? defaultSchedule;
  const cancel = options.cancel ?? defaultCancel;
  const maxFlushCharacters = options.maxFlushCharacters ?? DEFAULT_MAX_FLUSH_CHARACTERS;
  let scheduledHandle: number | null = null;
  let disposed = false;

  const scheduleFlush = () => {
    if (disposed || scheduledHandle !== null) {
      return;
    }
    scheduledHandle = schedule(flush);
  };

  const flush = () => {
    scheduledHandle = null;
    if (disposed) {
      queue.length = 0;
      return;
    }

    const pending: TerminalOutputChunk[] = [];
    let pendingLength = 0;
    while (queue.length > 0) {
      const next = queue[0];
      const nextLength = chunkLength(next);
      if (pending.length > 0 && pendingLength + nextLength > maxFlushCharacters) {
        break;
      }
      queue.shift();
      pending.push(next);
      pendingLength += nextLength;
    }

    for (const chunk of joinChunks(pending)) {
      options.write(chunk);
    }

    if (queue.length > 0) {
      scheduleFlush();
    }
  };

  return {
    enqueue(data: TerminalOutputChunk): void {
      if (disposed) {
        return;
      }
      queue.push(data);
      scheduleFlush();
    },
    dispose(): void {
      disposed = true;
      queue.length = 0;
      if (scheduledHandle !== null) {
        cancel(scheduledHandle);
        scheduledHandle = null;
      }
    },
  };
}
