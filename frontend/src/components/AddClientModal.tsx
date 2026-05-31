import { useEffect, useState } from "react";

import type { BootstrapClientInput } from "../types";
import { BootstrapClientForm } from "./BootstrapClientForm";
import { ClientRegistrationKeyForm } from "./ClientRegistrationKeyForm";

type AddClientMode = "bootstrap" | "registration";

type AddClientModalProps = {
  isOpen: boolean;
  initialMode?: AddClientMode;
  bootstrapFailed: boolean;
  bootstrapPending: boolean;
  registrationKey: string | null;
  registrationKeyPending: boolean;
  registrationKeyError: string | null;
  onClose: () => void;
  onBootstrapSubmit: (payload: BootstrapClientInput) => void;
  onGenerateRegistrationKey: (label?: string | null) => void;
};

export function AddClientModal({
  isOpen,
  initialMode = "bootstrap",
  bootstrapFailed,
  bootstrapPending,
  registrationKey,
  registrationKeyPending,
  registrationKeyError,
  onClose,
  onBootstrapSubmit,
  onGenerateRegistrationKey
}: AddClientModalProps) {
  const [mode, setMode] = useState<AddClientMode>(initialMode);

  useEffect(() => {
    if (isOpen) {
      setMode(initialMode);
    }
  }, [initialMode, isOpen]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) {
        return;
      }
      event.preventDefault();
      onClose();
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) {
    return null;
  }

  return (
    <div
      className="add-client-modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        aria-label="Add client"
        aria-modal="true"
        className="add-client-modal"
        role="dialog"
      >
        <div className="add-client-modal-header">
          <div>
            <h2>Add client</h2>
            <p className="muted">Choose how this remote client should connect to Web Terminal ACP.</p>
          </div>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="add-client-mode-tabs" role="tablist" aria-label="Client add mode">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "bootstrap"}
            className={mode === "bootstrap" ? "active" : ""}
            data-onboarding-id="add-client-bootstrap-tab"
            onClick={() => setMode("bootstrap")}
          >
            SSH Bootstrap
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "registration"}
            className={mode === "registration" ? "active" : ""}
            data-onboarding-id="add-client-registration-tab"
            onClick={() => setMode("registration")}
          >
            Registration Key
          </button>
        </div>
        {mode === "bootstrap" ? (
          <>
            <BootstrapClientForm isSubmitting={bootstrapPending} onSubmit={onBootstrapSubmit} />
            {bootstrapFailed && (
              <p className="error" role="alert">
                Bootstrap failed. Check host, key, dependencies, and server URL.
              </p>
            )}
          </>
        ) : (
          <ClientRegistrationKeyForm
            registrationKey={registrationKey}
            registrationKeyPending={registrationKeyPending}
            registrationKeyError={registrationKeyError}
            onGenerateRegistrationKey={onGenerateRegistrationKey}
          />
        )}
      </div>
    </div>
  );
}
