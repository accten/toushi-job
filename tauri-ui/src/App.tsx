import { useEffect, useState } from "react";
import {
  History,
  LifeBuoy,
  Moon,
  Play,
  SlidersHorizontal,
  Sun,
} from "lucide-react";
import RunPage from "./pages/Run";
import ConfigPage from "./pages/Config";
import HistoryPage from "./pages/History";
import UpdateBanner from "./components/UpdateBanner";
import StatusBar from "./components/StatusBar";
import { useRunStore, useT } from "./store";
import { ipc } from "./lib/ipc";
import { isLang } from "./lib/i18n";

type Tab = "run" | "config" | "history";

// 外壳：左侧导航 + 顶栏 + 内容区 + 底部状态栏。
// 桌面 App 的常规骨架 —— 导航常驻不占内容高度，运行统计常驻底部一眼可见。
export default function App() {
  const [tab, setTab] = useState<Tab>("run");
  const [helpMsg, setHelpMsg] = useState<string | null>(null);
  const running = useRunStore((s) => s.running);
  const setLang = useRunStore((s) => s.setLang);
  const theme = useRunStore((s) => s.theme);
  const toggleTheme = useRunStore((s) => s.toggleTheme);
  const t = useT();

  // 「问 AI 帮忙」：把 app 上下文 + 当前实时日志缓冲打成一段求助文本复制到剪贴板，
  // 用户粘到任意聊天 AI 即可。常驻顶栏，每个 tab 都点得到（不只出错时）。
  async function askAi() {
    try {
      const logs = useRunStore.getState().logs;
      const { text } = await ipc.getAiHelpReport(logs);
      await navigator.clipboard.writeText(text);
      setHelpMsg(t("askai.ok"));
    } catch {
      setHelpMsg(t("askai.fail"));
    }
    setTimeout(() => setHelpMsg(null), 6000);
  }

  // 启动时读回 .env 里存的 UI 语言，覆盖系统探测的默认。
  // 没设过（首次启动）则把探测到的默认落进 .env，让后端报错（读 BOSS_LANG）跟
  // 前端展示的语言一致——否则非中文用户首次见到的后端串会是中文。读/写失败都
  // 静默：大不了下次启动再探测一次，不打断使用。
  useEffect(() => {
    ipc.getLanguage()
      .then(({ lang }) => {
        if (isLang(lang)) {
          setLang(lang);
        } else {
          const detected = useRunStore.getState().lang;
          ipc.setLanguage(detected).catch(() => {});
        }
      })
      .catch(() => {});
  }, [setLang]);

  const pageTitle = { run: t("tab.run"), config: t("tab.config"), history: t("tab.history") }[tab];
  const pageSub = {
    run: t("run.subtitle"),
    config: t("config.editPrefix") + " .env",
    history: t("history.subtitle"),
  }[tab];

  return (
    <div className="app-shell">
      {/* ===== 左侧导航 ===== */}
      <aside className="sidebar">
        <div className="px-4 pt-5 pb-4 border-b border-[var(--border-light)]">
          <div className="flex items-baseline gap-1.5 leading-none">
            <span className="font-serif text-[22px] tracking-tight">投石</span>
            <span className="font-serif italic text-[22px] tracking-tight">Toushi</span>
          </div>
          <p className="field-hint mt-2 leading-snug">{t("app.tagline")}</p>
        </div>

        <nav className="p-2 space-y-0.5 flex-1">
          <NavItem current={tab} value="run" onClick={setTab} icon={<Play size={15} strokeWidth={2} />}>
            {t("tab.run")}
            {running && <span className="live-dot ml-auto" aria-hidden />}
          </NavItem>
          <NavItem
            current={tab}
            value="config"
            onClick={setTab}
            icon={<SlidersHorizontal size={15} strokeWidth={2} />}
          >
            {t("tab.config")}
          </NavItem>
          <NavItem
            current={tab}
            value="history"
            onClick={setTab}
            icon={<History size={15} strokeWidth={2} />}
          >
            {t("tab.history")}
          </NavItem>
        </nav>

        <div className="border-t border-[var(--border-light)] p-2">
          <button
            type="button"
            onClick={toggleTheme}
            className="nav-item"
            title={theme === "dark" ? t("theme.toLight") : t("theme.toDark")}
          >
            {theme === "dark" ? <Sun size={15} strokeWidth={2} /> : <Moon size={15} strokeWidth={2} />}
            {theme === "dark" ? t("theme.toLight") : t("theme.toDark")}
          </button>
        </div>
      </aside>

      {/* ===== 右侧主区 ===== */}
      <div className="flex-1 flex flex-col min-w-0">
        <header className="flex items-center gap-3 px-5 h-12 flex-shrink-0 border-b border-[var(--border-light)] bg-[var(--paper-2)]">
          <h1 className="text-sm font-semibold tracking-tight">{pageTitle}</h1>
          <span className="field-hint truncate">{pageSub}</span>

          <div className="ml-auto flex items-center gap-3 min-w-0">
            {helpMsg && (
              <span className="text-[12px] text-[var(--ink)] truncate max-w-[380px] fade-in">
                {helpMsg}
              </span>
            )}
            <button
              type="button"
              onClick={askAi}
              title={t("header.askAiHint")}
              className="btn-outline text-[12px] px-3 py-1.5 flex-shrink-0"
            >
              <LifeBuoy size={14} strokeWidth={2} aria-hidden />
              {t("header.askAi")}
            </button>
          </div>
        </header>

        <UpdateBanner />

        {/* 内容区自己不滚：运行页要占满高度（日志常驻），配置/历史各自滚 */}
        <main className="flex-1 min-h-0 overflow-hidden">
          {tab === "run" && <RunPage />}
          {tab === "config" && <ConfigPage />}
          {tab === "history" && <HistoryPage />}
        </main>

        <StatusBar />
      </div>
    </div>
  );
}

function NavItem({
  current,
  value,
  onClick,
  icon,
  children,
}: {
  current: Tab;
  value: Tab;
  onClick: (t: Tab) => void;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  const active = current === value;
  return (
    <button
      type="button"
      onClick={() => onClick(value)}
      data-active={active}
      className="nav-item"
    >
      <span className="opacity-80">{icon}</span>
      {children}
    </button>
  );
}
