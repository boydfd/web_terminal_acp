import type { ITheme } from "@xterm/xterm";

export type ThemeSkinId = "default" | "linear" | "notion" | "vercel" | "stripe" | "raycast";

type ThemeSkin = {
  id: ThemeSkinId;
  label: string;
  source: string;
  summary: string;
  cssClass: string;
  terminalTheme: ITheme;
};

export const DEFAULT_THEME_SKIN: ThemeSkinId = "default";

export const THEME_SKINS: ThemeSkin[] = [
  {
    id: "default",
    label: "Default",
    source: "Web Terminal",
    summary: "当前深色工作台风格",
    cssClass: "theme-skin-default",
    terminalTheme: {
      background: "#020617",
      foreground: "#e5e7eb",
      cursor: "#38bdf8",
      cursorAccent: "#020617",
      selectionBackground: "rgba(56, 189, 248, 0.35)"
    }
  },
  {
    id: "linear",
    label: "Linear",
    source: "design-md/linear.app",
    summary: "近黑工艺感、薰衣草蓝焦点、精细边线",
    cssClass: "theme-skin-linear",
    terminalTheme: {
      background: "#010102",
      foreground: "#f7f8f8",
      cursor: "#828fff",
      cursorAccent: "#010102",
      selectionBackground: "rgba(94, 106, 210, 0.38)"
    }
  },
  {
    id: "notion",
    label: "Notion",
    source: "design-md/notion",
    summary: "纸感浅色工作区、墨色文字、紫色主操作",
    cssClass: "theme-skin-notion",
    terminalTheme: {
      background: "#fbfaf8",
      foreground: "#1a1a1a",
      cursor: "#5645d4",
      cursorAccent: "#ffffff",
      selectionBackground: "rgba(86, 69, 212, 0.22)"
    }
  },
  {
    id: "vercel",
    label: "Vercel",
    source: "design-md/vercel",
    summary: "黑白高对比、极简边框、克制单色控件",
    cssClass: "theme-skin-vercel",
    terminalTheme: {
      background: "#000000",
      foreground: "#fafafa",
      cursor: "#ffffff",
      cursorAccent: "#000000",
      selectionBackground: "rgba(255, 255, 255, 0.24)"
    }
  },
  {
    id: "stripe",
    label: "Stripe",
    source: "design-md/stripe",
    summary: "浅色金融界面、靛蓝操作、柔和蓝灰面板",
    cssClass: "theme-skin-stripe",
    terminalTheme: {
      background: "#f6f9fc",
      foreground: "#0d253d",
      cursor: "#533afd",
      cursorAccent: "#ffffff",
      selectionBackground: "rgba(83, 58, 253, 0.22)"
    }
  },
  {
    id: "raycast",
    label: "Raycast",
    source: "design-md/raycast",
    summary: "命令面板式深色界面、白色主按钮、红色强调",
    cssClass: "theme-skin-raycast",
    terminalTheme: {
      background: "#07080a",
      foreground: "#f4f4f6",
      cursor: "#ffffff",
      cursorAccent: "#07080a",
      selectionBackground: "rgba(255, 97, 97, 0.32)"
    }
  }
];

const THEME_SKIN_IDS = new Set<ThemeSkinId>(THEME_SKINS.map((skin) => skin.id));

export function isThemeSkinId(value: string | null): value is ThemeSkinId {
  return value !== null && THEME_SKIN_IDS.has(value as ThemeSkinId);
}

export function themeSkinClassName(themeSkin: ThemeSkinId): string {
  return THEME_SKINS.find((skin) => skin.id === themeSkin)?.cssClass ?? "theme-skin-default";
}

export function terminalThemeForSkin(themeSkin: ThemeSkinId): ITheme {
  return THEME_SKINS.find((skin) => skin.id === themeSkin)?.terminalTheme ?? THEME_SKINS[0].terminalTheme;
}
