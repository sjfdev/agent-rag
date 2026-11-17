// Theme helpers for the admin console.
//
// Dark and light are CSS presets; the custom palette is derived from four
// base colors (background, panel, text, accent) and written as variables on
// :root. The chosen mode and palette live in localStorage so the console
// reopens with the same look.
// 管理台主题：深色/浅色由 CSS 预设决定；自定义主题由背景/面板/文字/主色四个
// 基础色推导出整套变量写到 :root。主题模式与配色存在 localStorage。

const THEME_KEY = "adminTheme";
const THEME_CUSTOM_KEY = "adminThemeCustom";
const THEME_DEFAULT_CUSTOM = {
  bg: "#101827",
  panel: "#1a2438",
  text: "#e8eefc",
  accent: "#4f8cff",
};

function themeHexRgb(hex) {
  let value = (hex || "").replace("#", "");
  if (value.length === 3) {
    value = value
      .split("")
      .map((c) => c + c)
      .join("");
  }
  const n = parseInt(value, 16);
  if (Number.isNaN(n)) return [0, 0, 0];
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function themeRgbHex(parts) {
  return (
    "#" +
    parts
      .map((n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0"))
      .join("")
  );
}

function themeMix(a, b, ratio) {
  const rgbA = themeHexRgb(a);
  const rgbB = themeHexRgb(b);
  return themeRgbHex(rgbA.map((value, index) => value + (rgbB[index] - value) * ratio));
}

function themeLuminance(hex) {
  const [r, g, b] = themeHexRgb(hex);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
}

function themePalette(custom) {
  const bg = custom.bg;
  const panel = custom.panel;
  const text = custom.text;
  const accent = custom.accent;
  const light = themeLuminance(bg) > 0.55;
  const ok = light ? "#12805c" : "#3ecf8e";
  const warn = light ? "#a4610a" : "#f5a524";
  const bad = light ? "#d13438" : "#ff6b6b";
  const changed = light ? "#b35c00" : "#ff9f43";
  return {
    "--bg": bg,
    "--panel": panel,
    "--panel-2": themeMix(panel, text, 0.07),
    "--text": text,
    "--muted": themeMix(text, bg, 0.42),
    "--accent": accent,
    "--accent-fg": themeLuminance(accent) > 0.6 ? "#0b1020" : "#ffffff",
    "--ok": ok,
    "--warn": warn,
    "--bad": bad,
    "--changed": changed,
    "--border": themeMix(panel, text, 0.18),
    "--border-strong": themeMix(panel, text, 0.28),
    "--border-hover": themeMix(panel, text, 0.38),
    "--accent-soft": themeMix(bg, accent, light ? 0.12 : 0.18),
    "--drop-border": themeMix(panel, accent, 0.45),
    "--bar-bg": themeMix(panel, text, 0.12),
    "--log-bg": themeMix(bg, panel, light ? 0.5 : 0.35),
    "--log-text": themeMix(text, bg, 0.25),
    "--danger-border": themeMix(panel, bad, 0.45),
    "--danger-zone": themeMix(panel, bad, 0.28),
    "--danger-tint": themeMix(panel, bad, 0.12),
    "--overlay": "rgba(0, 0, 0, " + (light ? 0.38 : 0.62) + ")",
    "--overlay-strong": "rgba(0, 0, 0, " + (light ? 0.55 : 0.82) + ")",
    "--ok-bg": themeMix(panel, ok, light ? 0.12 : 0.16),
    "--ok-border": themeMix(panel, ok, 0.4),
    "--warn-bg": themeMix(panel, warn, light ? 0.12 : 0.16),
    "--warn-border": themeMix(panel, warn, 0.4),
    "--changed-bg": themeMix(panel, changed, light ? 0.12 : 0.16),
    "--changed-border": themeMix(panel, changed, 0.4),
  };
}

const THEME_CUSTOM_VARS = Object.keys(themePalette(THEME_DEFAULT_CUSTOM));

function loadCustomTheme() {
  try {
    const saved = JSON.parse(localStorage.getItem(THEME_CUSTOM_KEY) || "null");
    if (saved && saved.bg && saved.panel && saved.text && saved.accent) {
      return saved;
    }
  } catch (error) {}
  return Object.assign({}, THEME_DEFAULT_CUSTOM);
}

function saveCustomTheme(custom) {
  try {
    localStorage.setItem(THEME_CUSTOM_KEY, JSON.stringify(custom));
  } catch (error) {}
}

function saveThemeMode(mode) {
  try {
    localStorage.setItem(THEME_KEY, mode);
  } catch (error) {}
}

function applyTheme(mode, custom) {
  const root = document.documentElement;
  const palette = themePalette(custom || loadCustomTheme());
  if (mode === "custom") {
    for (const [key, value] of Object.entries(palette)) {
      root.style.setProperty(key, value);
    }
    root.dataset.theme = "custom";
    root.style.colorScheme = themeLuminance(palette["--bg"]) > 0.55 ? "light" : "dark";
    return;
  }
  for (const key of THEME_CUSTOM_VARS) root.style.removeProperty(key);
  root.dataset.theme = mode === "light" ? "light" : "dark";
  root.style.colorScheme = mode === "light" ? "light" : "dark";
}

function currentThemeMode() {
  const mode = document.documentElement.dataset.theme;
  return mode === "light" || mode === "custom" ? mode : "dark";
}

(function bootTheme() {
  let mode = "dark";
  try {
    mode = localStorage.getItem(THEME_KEY) || "dark";
  } catch (error) {}
  if (mode !== "dark" && mode !== "light" && mode !== "custom") mode = "dark";
  applyTheme(mode, loadCustomTheme());
})();
