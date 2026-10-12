import { useEffect, useState } from "react";
import { ArrowUpRight, Sparkles, X } from "lucide-react";
import { ipc, type UpdateInfo } from "../lib/ipc";
import { useT } from "../store";

// 「有新版」横幅：挂载时静默查一次 GitHub 最新 release。
// 只提示不自动安装——点「前往下载」用系统浏览器打开 release 页，下载/安装手动。
// 没新版 / 没网 / 用户关掉本次提示时不渲染任何东西。
export default function UpdateBanner() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const t = useT();

  useEffect(() => {
    let alive = true;
    // 后端 check_for_update 永不抛错；catch 只是兜 IPC 层意外，绝不打扰用户。
    ipc.checkForUpdate()
      .then((r) => {
        if (alive && r.hasUpdate) setInfo(r);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  if (!info || dismissed) return null;

  return (
    <div className="flex items-center gap-3 px-5 h-9 flex-shrink-0 bg-[var(--ink)] text-[var(--paper)] text-[12px] font-mono fade-in">
      <Sparkles size={13} strokeWidth={2.25} aria-hidden className="opacity-80" />
      <span>{t("update.available", { latest: info.latest, current: info.current })}</span>
      <button
        type="button"
        onClick={() => ipc.openReleasePage(info.url).catch(() => {})}
        className="inline-flex items-center gap-1 underline underline-offset-2 hover:opacity-70 transition-opacity"
      >
        {t("update.download")}
        <ArrowUpRight size={12} strokeWidth={2.5} aria-hidden />
      </button>
      <button
        type="button"
        onClick={() => setDismissed(true)}
        className="ml-auto hover:opacity-70 transition-opacity"
        aria-label={t("update.dismiss")}
      >
        <X size={14} strokeWidth={2.5} aria-hidden />
      </button>
    </div>
  );
}
