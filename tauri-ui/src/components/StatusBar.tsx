import { AlertTriangle } from "lucide-react";
import { useRunStats, useRunStore, useT } from "../store";

/** 底部状态栏（IDE 风格）：一眼看到"在不在跑 / 跑哪个平台 / 这轮拿了多少"。
 * 数字全部从事件流派生（store.useRunStats），不额外问后端要状态。 */
export default function StatusBar() {
  const t = useT();
  const running = useRunStore((s) => s.running);
  const currentIndex = useRunStore((s) => s.currentIndex);
  const platform = useRunStore((s) => s.formPlatform);
  const dryRun = useRunStore((s) => s.formDryRun);
  const stats = useRunStats();

  const platformLabel = platform === "zhilian" ? t("run.platformZhilian") : t("run.platformBoss");

  return (
    <footer className="statusbar">
      <span className="flex items-center gap-2 min-w-0">
        {running && <span className="live-dot" aria-hidden />}
        <span className={running ? "text-[var(--ink)]" : ""}>
          {running ? t("statusbar.running") : t("statusbar.idle")}
        </span>
        {running && currentIndex !== null && (
          <span className="opacity-70">{t("statusbar.job", { index: currentIndex })}</span>
        )}
      </span>

      <span className="opacity-30">|</span>
      <span>{t("statusbar.platform", { platform: platformLabel })}</span>
      <span className={dryRun ? "badge-outline" : "badge-invert"}>
        {dryRun ? t("statusbar.dryRunOn") : t("statusbar.dryRunOff")}
      </span>

      {running && (
        <>
          <span className="opacity-30">|</span>
          <span className="flex items-center gap-1.5 opacity-80">
            <AlertTriangle size={12} strokeWidth={2.25} aria-hidden />
            {t("statusbar.warn")}
          </span>
        </>
      )}

      <span className="ml-auto flex items-center gap-3 tabular-nums">
        <span>
          {t("statusbar.found")} <b className="text-[var(--ink)]">{stats.found}</b>
        </span>
        <span className="opacity-70">
          {t("statusbar.skipped")} {stats.skipped}
        </span>
        <span className="opacity-70">
          {t("statusbar.dry")} {stats.dry}
        </span>
        <span>
          {t("statusbar.sent")} <b className="text-[var(--ink)]">{stats.sent}</b>
        </span>
        {stats.blocked > 0 && (
          <span className="opacity-70">
            {t("statusbar.blocked")} {stats.blocked}
          </span>
        )}
        {stats.failed && <span className="badge-invert">{t("statusbar.failed")}</span>}
      </span>
    </footer>
  );
}
