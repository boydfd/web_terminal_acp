export type ElectronAPI = {
  isElectron: boolean;
  platform: NodeJS.Platform;
  readClipboardText?: () => Promise<string>;
  writeClipboardText?: (text: string) => Promise<void>;
};

declare global {
  interface Window {
    electronAPI?: ElectronAPI;
  }
}

export {};
