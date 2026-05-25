declare const process: { exitCode?: number };

import { createTerminalOutputBuffer } from "../src/terminalOutputBuffer.js";

function assert(condition: unknown, message: string): void {
  if (!condition) {
    throw new Error(message);
  }
}

async function testBatchesOutputUntilScheduledFlush(): Promise<void> {
  const writes: Array<string | Uint8Array> = [];
  const scheduled: Array<() => void> = [];
  const writer = createTerminalOutputBuffer({
    write: (data) => writes.push(data),
    schedule: (callback) => {
      scheduled.push(callback);
      return scheduled.length;
    },
    cancel: () => undefined,
  });

  writer.enqueue("a");
  writer.enqueue("b");
  writer.enqueue("c");

  assert(writes.length === 0, "output should not be written synchronously");
  assert(scheduled.length === 1, "multiple chunks should share one scheduled flush");

  scheduled.shift()?.();

  assert(writes.length === 1, "queued output should flush as one write");
  assert(writes[0] === "abc", "queued chunks should preserve ordering");
}

async function testYieldsBetweenLargeFlushes(): Promise<void> {
  const writes: Array<string | Uint8Array> = [];
  const scheduled: Array<() => void> = [];
  const writer = createTerminalOutputBuffer({
    write: (data) => writes.push(data),
    schedule: (callback) => {
      scheduled.push(callback);
      return scheduled.length;
    },
    cancel: () => undefined,
    maxFlushCharacters: 4,
  });

  writer.enqueue("abc");
  writer.enqueue("def");

  scheduled.shift()?.();

  assert(writes.length === 1, "first flush should stay under the configured budget");
  assert(writes[0] === "abc", "first chunk should flush before later output");
  assert(scheduled.length === 1, "remaining output should be deferred to a later frame");

  scheduled.shift()?.();

  assert(writes.length === 2, "deferred output should flush on the next schedule");
  assert(writes[1] === "def", "deferred chunk should preserve ordering");
}

async function testOversizedChunkFlushesWithoutDroppingData(): Promise<void> {
  const writes: Array<string | Uint8Array> = [];
  const scheduled: Array<() => void> = [];
  const writer = createTerminalOutputBuffer({
    write: (data) => writes.push(data),
    schedule: (callback) => {
      scheduled.push(callback);
      return scheduled.length;
    },
    cancel: () => undefined,
    maxFlushCharacters: 4,
  });

  writer.enqueue("abcdef");
  writer.enqueue("g");

  scheduled.shift()?.();
  scheduled.shift()?.();

  assert(writes.length === 2, "oversized chunk and following chunk should both flush");
  assert(writes[0] === "abcdef", "oversized chunk should be preserved exactly");
  assert(writes[1] === "g", "following chunk should not be dropped");
}

async function run(): Promise<void> {
  await testBatchesOutputUntilScheduledFlush();
  await testYieldsBetweenLargeFlushes();
  await testOversizedChunkFlushesWithoutDroppingData();
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
