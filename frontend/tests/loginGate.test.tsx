import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BackendConnectionGate, LoginGate } from "../src/components/LoginGate";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

function render(element: ReactElement): void {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(element);
  });
}

function changeInputValue(target: HTMLInputElement, value: string): void {
  const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  act(() => {
    descriptor?.set?.call(target, value);
    target.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  root = null;
  container = null;
  vi.restoreAllMocks();
});

describe("LoginGate", () => {
  it("lets users save a backend address before logging in", () => {
    const onSaveBackendAddress = vi.fn();
    render(
      <LoginGate
        backendAddress="http://127.0.0.1:8001"
        backendAddressError={null}
        error={null}
        isCheckingBackend={false}
        isSubmitting={false}
        onSaveBackendAddress={onSaveBackendAddress}
        onSubmit={async () => {}}
      />
    );

    const backendInput = Array.from(container?.querySelectorAll("input") ?? [])
      .find((input) => input.previousElementSibling?.textContent === "后端地址");
    expect(backendInput).toBeInstanceOf(HTMLInputElement);
    changeInputValue(backendInput as HTMLInputElement, "http://control.example.com:8001");

    const saveButton = Array.from(container?.querySelectorAll("button") ?? [])
      .find((button) => button.textContent === "保存后端地址");
    act(() => {
      saveButton?.click();
    });

    expect(onSaveBackendAddress).toHaveBeenCalledWith("http://control.example.com:8001");
  });

  it("shows backend configuration when the app cannot connect", () => {
    const onSaveBackendAddress = vi.fn();
    render(
      <BackendConnectionGate
        backendAddress="http://127.0.0.1:8001"
        backendAddressError="请输入有效的 HTTP/HTTPS 后端地址"
        connectionError="404 File not found"
        isCheckingBackend={false}
        onSaveBackendAddress={onSaveBackendAddress}
      />
    );

    expect(container?.textContent).toContain("无法连接后端：404 File not found");
    expect(container?.textContent).toContain("请输入有效的 HTTP/HTTPS 后端地址");

    const resetButton = Array.from(container?.querySelectorAll("button") ?? [])
      .find((button) => button.textContent === "恢复默认");
    act(() => {
      resetButton?.click();
    });

    expect(onSaveBackendAddress).toHaveBeenCalledWith("");
  });
});
