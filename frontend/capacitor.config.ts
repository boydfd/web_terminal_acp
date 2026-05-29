import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.webterminal.acp",
  appName: "Web Terminal ACP",
  webDir: "dist",
  server: {
    androidScheme: "https",
    cleartext: true
  },
  android: {
    allowMixedContent: true,
    backgroundColor: "#0f172a",
    captureInput: true
  }
};

export default config;
