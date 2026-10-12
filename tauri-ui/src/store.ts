/** 全局状态：当前是否在 run、最近事件、最近日志、主题、表单。Zustand。
 * 之所以全局：跨 tab 切换不丢运行状态 + 不丢已收的事件。 */
import { create } from "zustand";
import { useCallback, useMemo } from "react";
import type { ProgressEvent, Platform } from "./lib/ipc";
import { type Lang, detectLang, translate } from "./lib/i18n";

const MAX_EVENTS = 500;
const MAX_LOGS = 1000;
const THEME_KEY = "toushi.theme";

export type Theme = "light" | "dark";

type RunState = {
  running: boolean;
  events: ProgressEvent[];
  logs: string[];
  currentIndex: number | null; // 当前 job_index（从 job_found 提取）

  // UI 语言：启动时按系统 locale 探测，App 挂载后用 .env 的 BOSS_LANG 覆盖。
  lang: Lang;
  // 主题：存 localStorage；首次启动跟随系统 prefers-color-scheme。
  // 进 store 而不是组件内 state，是因为顶栏切换 + 状态栏都要读它。
  theme: Theme;

  // 跨 tab 保留的表单状态
  formUsrName: string;
  formLabel: string;
  formDryRun: boolean;
  formPlatform: Platform;

  setRunning: (running: boolean) => void;
  pushEvent: (ev: ProgressEvent) => void;
  pushLog: (line: string) => void;
  clear: () => void;
  setLang: (lang: Lang) => void;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;

  setFormState: (state: Partial<Pick<RunState, "formUsrName" | "formLabel" | "formDryRun" | "formPlatform">>) => void;
};

/** 初始主题：localStorage 优先，其次跟随系统。 */
function detectTheme(): Theme {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

function applyTheme(theme: Theme) {
  try {
    document.documentElement.dataset.theme = theme;
  } catch {
    /* 非浏览器环境忽略 */
  }
}

const initialTheme = detectTheme();
applyTheme(initialTheme);

export const useRunStore = create<RunState>((set, get) => ({
  running: false,
  events: [],
  logs: [],
  currentIndex: null,

  lang: detectLang(),
  theme: initialTheme,

  formUsrName: "",
  formLabel: "",
  formDryRun: true,
  formPlatform: "boss" as Platform, // 默认 BOSS

  setRunning: (running) => set({ running }),
  pushEvent: (ev) =>
    set((s) => {
      const events = [...s.events, ev].slice(-MAX_EVENTS);
      let currentIndex = s.currentIndex;
      if (ev.kind === "job_found" && typeof ev.payload.index === "number") {
        currentIndex = ev.payload.index;
      }
      let running = s.running;
      if (ev.kind === "loop_ended") {
        running = false;
      }
      return { events, currentIndex, running };
    }),
  pushLog: (line) => set((s) => ({ logs: [...s.logs, line].slice(-MAX_LOGS) })),
  clear: () => set({ events: [], logs: [], currentIndex: null }),
  setLang: (lang) => set({ lang }),
  setTheme: (theme) => {
    applyTheme(theme);
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* 存不下就算了，本次会话仍然生效 */
    }
    set({ theme });
  },
  toggleTheme: () => get().setTheme(get().theme === "dark" ? "light" : "dark"),
  setFormState: (state) => set((s) => ({ ...s, ...state })),
}));

/** 组件里取翻译函数：const t = useT()，然后 t("run.title")。
 * 订阅 store.lang，切语言时所有用到的组件自动重渲染。 */
export function useT() {
  const lang = useRunStore((s) => s.lang);
  return useCallback(
    (key: string, vars?: Record<string, string | number>) => translate(lang, key, vars),
    [lang],
  );
}

/** 从事件流里滚出来的本轮统计。状态栏 + 进度面板共用。
 * 全部在客户端派生：后端不需要为"显示几个数"多加一份状态。 */
export type RunStats = {
  found: number;   // 抓到过多少个 JD
  skipped: number; // 被过滤掉的
  sent: number;    // 真的发出去了
  dry: number;     // dry run 里生成但没发的
  blocked: number; // 校验没过
  failed: boolean; // 本轮出过错
};

export function useRunStats(): RunStats {
  const events = useRunStore((s) => s.events);
  return useMemo(() => {
    const stats: RunStats = { found: 0, skipped: 0, sent: 0, dry: 0, blocked: 0, failed: false };
    for (const e of events) {
      switch (e.kind) {
        case "job_found":
          stats.found++;
          break;
        case "job_skipped":
          stats.skipped++;
          break;
        case "letter_sent": {
          const status = e.payload.status;
          if (status === "sent") stats.sent++;
          else if (status === "dry_run") stats.dry++;
          else stats.blocked++;
          break;
        }
        case "error":
          stats.failed = true;
          break;
        case "loop_ended":
          if (e.payload.reason === "error") stats.failed = true;
          break;
      }
    }
    return stats;
  }, [events]);
}
