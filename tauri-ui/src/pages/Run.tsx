import { useEffect, useRef, useState } from "react";
import {
  Activity,
  Copy,
  ExternalLink,
  FileText,
  LogIn,
  Play,
  RotateCcw,
  Square,
  TriangleAlert,
} from "lucide-react";
import { Channel } from "@tauri-apps/api/core";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { ipc, type ProgressEvent, type RunConfig, type ResumeInfo, type LlmConfig, type Platform } from "../lib/ipc";
import { useRunStore, useT } from "../store";

// 运行页：editorial 三栏式布局
// 1) 顶部 H2 + 状态副标题
// 2) 表单（底部边框输入，无圆角）
// 3) 控制按钮行（黑底白字反色按钮）
// 4) 双面板：进度事件 + 日志（日志保留黑底白字，刚好契合 monochrome 风格）
export default function RunPage() {
  const t = useT();
  // AI ?在?配??tab 设；运?页只读展?+ 校验?配好?key?
  const [llmCfg, setLlmCfg] = useState<LlmConfig | null>(null);
  const [llmError, setLlmError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // ?历（拖拽上传?
  const [resume, setResumeInfo] = useState<ResumeInfo | null>(null);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  // ?始前校验的内联报错（替代生硬?alert?
  const [startError, setStartError] = useState<string | null>(null);
  // 出错卡片：?制日志反?+ 日志?（懒加载?
  const [copyMsg, setCopyMsg] = useState<string | null>(null);
  const [logPaths, setLogPaths] = useState<{ dir: string } | null>(null);

  // Global form states
  const usrName = useRunStore((s) => s.formUsrName);
  const label = useRunStore((s) => s.formLabel);
  const dryRun = useRunStore((s) => s.formDryRun);
  const platform = useRunStore((s) => s.formPlatform);
  const setFormState = useRunStore((s) => s.setFormState);
  // 平台显示名：按钮和日志里都带上，?眼能看出这个按钮/这?点击对应?平台
  // （曾经出现过"选了智联却开?BOSS 窗口"，界?完全看不出它认的??台）?
  const platformLabel =
    platform === "zhilian" ? t("run.platformZhilian") : t("run.platformBoss");

  const running = useRunStore((s) => s.running);
  const events = useRunStore((s) => s.events);
  const logs = useRunStore((s) => s.logs);
  const currentIndex = useRunStore((s) => s.currentIndex);
  const setRunning = useRunStore((s) => s.setRunning);
  const pushEvent = useRunStore((s) => s.pushEvent);
  const pushLog = useRunStore((s) => s.pushLog);
  const clear = useRunStore((s) => s.clear);

  const logScrollRef = useRef<HTMLPreElement>(null);
  const eventsScrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    ipc.getLlmConfig()
      .then((cfg) => setLlmCfg(cfg))
      .catch((e) => setLlmError(String(e)));

    ipc.getEnvFields().then(({ fields }) => {
      const stateUpdate: Record<string, any> = {};
      const nameField = fields.find(f => f.key === "BOSS_USR_NAME");
      if (nameField && nameField.value && !useRunStore.getState().formUsrName) {
        stateUpdate.formUsrName = nameField.value;
      }

      const labelField = fields.find(f => f.key === "BOSS_LABEL");
      if (labelField && labelField.value && !useRunStore.getState().formLabel) {
        stateUpdate.formLabel = labelField.value;
      }

      const platformField = fields.find(f => f.key === "PLATFORM");
      if (platformField && platformField.value && !useRunStore.getState().formPlatform) {
        // Validate platform value
        const platformValue = platformField.value.toLowerCase();
        if (platformValue === "boss" || platformValue === "zhilian") {
          stateUpdate.formPlatform = platformValue as Platform;
        }
      }

      if (Object.keys(stateUpdate).length > 0) {
        setFormState(stateUpdate);
      }
    }).catch(() => {});
  }, []);

  // 挂载时?回当前简历（standalone 下持久化?app 数据??resume/?
  useEffect(() => {
    ipc.getResume()
      .then(({ resume }) => setResumeInfo(resume))
      .catch(() => {});
  }, []);

  // 拖拽上传：监?webview ?drag-drop 事件。这?core API（@tauri-apps/api/webview），
  // 已? capabilities ?core:default 覆盖，不?要??plugin / ACL 配置?
  // ?useRunStore.getState() ?running ?新?，避免?running 放进 deps 反?重?阅??
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    getCurrentWebview()
      .onDragDropEvent((event) => {
        const p = event.payload;
        const isRunning = useRunStore.getState().running;
        if (p.type === "enter" || p.type === "over") {
          if (!isRunning) setDragging(true);
        } else if (p.type === "leave") {
          setDragging(false);
        } else if (p.type === "drop") {
          setDragging(false);
          if (isRunning) return;
          const pdf = p.paths.find((x) => x.toLowerCase().endsWith(".pdf"));
          if (!pdf) {
            setResumeError(t("run.onlyPdf"));
            return;
          }
          setResumeBusy(true);
          setResumeError(null);
          ipc.setResume(pdf)
            .then((info) => setResumeInfo(info))
            .catch((e) => setResumeError(String(e)))
            .finally(() => setResumeBusy(false));
        }
      })
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  // 文件选择器上传：webview ?<input type=file> 给不到真实路径，?读字节再传??
  // 跟拖拽并存??已有简历时也能点??择文件」换，不?剩拖拽??
  const fileInputRef = useRef<HTMLInputElement>(null);

  function arrayBufferToBase64(buf: ArrayBuffer): string {
    const bytes = new Uint8Array(buf);
    let binary = "";
    const chunk = 0x8000; // 分块避免 String.fromCharCode 参数过?爆栈
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }

  async function onFilePicked(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // 重置：允许再次?同??件也能触?change
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setResumeError("只接受 PDF 文件");
      return;
    }
    setResumeBusy(true);
    setResumeError(null);
    try {
      const b64 = arrayBufferToBase64(await file.arrayBuffer());
      const info = await ipc.setResumeBytes(file.name, b64);
      setResumeInfo(info);
    } catch (err) {
      setResumeError(String(err));
    } finally {
      setResumeBusy(false);
    }
  }

  // ?滚动到底
  useEffect(() => {
    if (logScrollRef.current) {
      logScrollRef.current.scrollTop = logScrollRef.current.scrollHeight;
    }
  }, [logs.length]);
  useEffect(() => {
    if (eventsScrollRef.current) {
      eventsScrollRef.current.scrollTop = eventsScrollRef.current.scrollHeight;
    }
  }, [events.length]);

  async function handleStart() {
    // ?始前的拦??走内?startError（不再用生硬的原?alert）??
    // 校验顺序跟后?start_run ?pre-flight 对齐，?用户秒级看到缺了啥，
    // 而不?run ?下?错?进日志面板??
    setStartError(null);
    if (!usrName.trim()) {
      setStartError(t("run.errNeedName"));
      return;
    }

    // ? BOSS 平台??LLM 配置（因为需要发送招呼??
    if (platform === "boss") {
      if (!llmCfg || !llmCfg.hasKey) {
        setStartError(t("run.errNeedAi"));
        return;
      }
      if (!llmCfg.model) {
        // ?model 时后?_build_client 会在??位抛?break 掉整?run，提前拦
        setStartError(t("run.errNeedModel"));
        return;
      }
    }

    if (!resume) {
      // 后? start_run ?道就?current_resume() is None ?ValueError；前?拦，
      // 避免 run ??on→off、错?埋在日志里??
      setStartError(t("run.errNeedResume"));
      return;
    }
    setBusy(true);
    clear();
    setRunning(true);

    const progressChannel = new Channel<ProgressEvent>((ev) => pushEvent(ev));
    const logChannel = new Channel<string>((line) => pushLog(line));

    const config: RunConfig = {
      usrName: usrName.trim(),
      label: platform === "boss" ? label.trim() : "", // BOSS 使用 label，智联不使用
      dryRun,
    };

    try {
      await ipc.startRun({ ...config, platform }, progressChannel, logChannel);
    } catch (e) {
      pushLog(t("run.logStartFailed", { err: String(e) }));
      setRunning(false);
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    setBusy(true);
    try {
      await ipc.stopRun();
    } catch (e) {
      pushLog(t("run.logStopFailed", { err: String(e) }));
    } finally {
      setBusy(false);
    }
  }

  // 手动登录：开??带调试?口的干净 Chrome（当前平台各? profile），
  // 用户在里面扫?登录，然后把窗口关掉再点??—??脚本复用同一 profile ?cookie?
  // 用干?窗口而不??脚本?那个?化窗口去登录：挂?CDP 的窗口会?台??
  // 反?送去安全验证（BOSS 上踩?security.html 死循?，登录页根本没法???
  async function handleLogin() {
    setBusy(true);
    pushLog(t("run.loginBusy", { platform: platformLabel }));
    try {
      const r = await ipc.openLoginWindow(platform);
      // 把平台名?起打出来：日志里写明??的是?平台的窗??
      // 免得出现"我明明?了智联却开?BOSS"这?分不清是谁的错的情况?
      pushLog(t("run.loginOpened", { platform: platformLabel, profile: r.profile }));
    } catch (e) {
      pushLog(t("run.loginFailed", { err: String(e) }));
    } finally {
      setBusy(false);
    }
  }

  async function handleReset() {
    setBusy(true);
    try {
      await ipc.shutdownBrowser();
      pushLog(t("run.logChromeClosed"));
    } catch (e) {
      pushLog(t("run.logShutdownFailed", { err: String(e) }));
    } finally {
      setBusy(false);
    }
  }

  // 出错时的"把日志发?入口：?测进度事件里有没?error / loop_ended(error)?
  const hasError = events.some(
    (e) => e.kind === "error" || (e.kind === "loop_ended" && e.payload?.reason === "error")
  );

  // 出错后懒加载?次日志目录路径，告诉用户文件在哪、可手动附带?
  useEffect(() => {
    if (hasError && !logPaths) {
      ipc.getLogDir().then((p) => setLogPaths({ dir: p.dir })).catch(() => {});
    }
  }, [hasError, logPaths]);

  async function copyLogs() {
    // 复制当前日志面板缓冲到剪贴板—?用户自己粘给作?/ ?issues，绝不自动发送??
    try {
      await navigator.clipboard.writeText(logs.join("\n"));
      setCopyMsg(t("run.copyOk"));
    } catch {
      setCopyMsg(t("run.copyFail"));
    }
    setTimeout(() => setCopyMsg(null), 4000);
  }

  // 「?制信??AI」：?header 那个同源—?app 上下?+ 实时日志打包成求助文???
  // 出错时这里给??近入口，用户不用回到 header 找??
  async function copyAiHelp() {
    try {
      const { text } = await ipc.getAiHelpReport(logs);
      await navigator.clipboard.writeText(text);
      setCopyMsg(t("askai.ok"));
    } catch {
      setCopyMsg(t("askai.fail"));
    }
    setTimeout(() => setCopyMsg(null), 6000);
  }

  // 状?副标?：替代原?右侧 idle/running 小字
  const statusLine = running
    ? currentIndex !== null
      ? t("run.statusRunningJob", { index: currentIndex })
      : t("run.statusStarting")
    : t("run.statusIdle");

  return (
    <div className="space-y-10">
      {/* === 段落 1：标?+ 状??=== */}
      <section className="flex items-end justify-between gap-6 pb-4 border-b-2 border-[var(--ink)]">
        <div>
          <h2 className="font-serif text-5xl leading-none tracking-tight">
            {t("run.title")}
          </h2>
          <p className="mono-tag mt-3">{t("run.subtitle")}</p>
        </div>
        <div className="text-right">
          <span className="mono-tag block">{t("run.status")}</span>
          <span
            className={[
              "font-mono text-sm uppercase tracking-widest mt-1 inline-block px-2 py-1",
              running
                ? "bg-[var(--ink)] text-[var(--paper)]"
                : "text-[var(--muted-fg)]",
            ].join(" ")}
          >
            {statusLine}
          </span>
        </div>
      </section>

      {/* === 段落 2：参数表?=== */}
      <section>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-12 gap-y-8">
          <Field label={t("run.fieldName")} hint={t("run.hintRequired")}>
            <input
              type="text"
              value={usrName}
              onChange={(e) => setFormState({ formUsrName: e.target.value })}
              disabled={running}
              className="field-input"
              placeholder={t("run.fieldNamePlaceholder")}
            />
          </Field>

          <Field label={t("run.fieldPlatform")} hint={t("run.hintPlatform")}>
            <select
              value={platform}
              onChange={(e) => setFormState({ formPlatform: e.target.value as Platform })}
              disabled={running}
              className="field-input font-mono"
            >
              <option value="boss">{t("run.platformBoss")}</option>
              <option value="zhilian">{t("run.platformZhilian")}</option>
            </select>
          </Field>

          {platform === "boss" && (
            <Field label={t("run.fieldLabel")} hint={t("run.hintOptional")}>
              <input
                type="text"
                value={label}
                onChange={(e) => setFormState({ formLabel: e.target.value })}
                disabled={running}
                className="field-input"
                placeholder={t("run.fieldLabelPlaceholder")}
              />
            </Field>
          )}

          <Field label={t("run.fieldEndpoint")} hint={t("run.hintSetInConfig")}>
            {llmError ? (
              <div className="text-sm font-mono py-2 border-b-2 border-[var(--ink)]">
                <span className="badge-outline mr-2">ERROR</span>
                {t("run.llmReadFailed", { err: llmError })}
              </div>
            ) : !llmCfg || !llmCfg.hasKey ? (
              <div className="text-sm font-mono py-2 border-b-2 border-[var(--ink)]">
                <span className="badge-invert mr-2">{t("run.noKey")}</span>
                {t("run.goConfigForKey")}
              </div>
            ) : (
              // ??展示：??+ model 在配?设，这里不再有下?
              <div className="text-sm font-mono py-2 border-b-2 border-[var(--ink)] flex items-center gap-2 flex-wrap">
                <span className="badge-invert">{llmCfg.model || t("run.noModelTag")}</span>
                <span className="text-[var(--muted-fg)] text-sm break-all">
                  {llmCfg.baseUrl || t("run.defaultEndpoint")}
                </span>
              </div>
            )}
          </Field>

          <Field label={t("run.fieldDryRun")}>
            <label className="inline-flex items-center gap-3 text-sm cursor-pointer py-2">
              {/* ??checkbox：黑边方块，选中后填充黑?*/}
              <span
                className={[
                  "inline-flex items-center justify-center w-5 h-5 border-2 border-[var(--ink)] transition-colors duration-100",
                  dryRun
                    ? "bg-[var(--ink)] text-[var(--paper)]"
                    : "bg-[var(--paper)] text-transparent",
                ].join(" ")}
              >
                <input
                  type="checkbox"
                  checked={dryRun}
                  onChange={(e) => setFormState({ formDryRun: e.target.checked })}
                  disabled={running}
                  className="sr-only"
                />
                {/* ?× 符号表示勾?，??更??editorial ?*/}
                <span className="text-sm font-bold leading-none">×</span>
              </span>
              <span className="font-mono uppercase tracking-widest text-sm">
                {t("run.dryRunDesc")}
              </span>
            </label>
          </Field>
        </div>
      </section>

      {/* === 段落 2.5：简历拖拽上?=== */}
      <section>
        <div className="flex items-baseline justify-between">
          <label className="field-label">{t("run.fieldResume")}</label>
          <span className="text-[13px] font-mono uppercase tracking-widest text-[var(--muted-fg)] italic">
            {t("run.resumeHint")}
          </span>
        </div>
        {/* 隐藏的原生文件?择?拖拽之?的另?条上传路?*/}
        <input
          ref={fileInputRef}
          type="file"
          accept="application/pdf,.pdf"
          className="hidden"
          onChange={onFilePicked}
        />
        <div
          onClick={() => {
            if (!running) fileInputRef.current?.click();
          }}
          role="button"
          tabIndex={running ? -1 : 0}
          className={[
            "mt-2 border-2 border-dashed p-6 transition-colors duration-100",
            dragging
              ? "border-[var(--ink)] bg-[var(--ink)] text-[var(--paper)]"
              : "border-[var(--border-light)]",
            running ? "opacity-50" : "cursor-pointer hover:border-[var(--ink)]",
          ].join(" ")}
        >
          {resumeBusy ? (
            <p className="font-mono text-sm">{t("run.uploading")}</p>
          ) : resume ? (
            <div className="flex items-center justify-between gap-4 flex-wrap">
              <span className="font-mono text-sm">
                <span className="mr-2">■</span>{t("run.currentResume", { filename: resume.filename })}
              </span>
              <div className="flex items-center gap-3 flex-wrap">
                <span className="mono-tag">{t("run.replaceHint")}</span>
                <PickFileButton onPick={() => fileInputRef.current?.click()} disabled={running} />
              </div>
            </div>
          ) : (
            <>
              <p className="font-mono text-sm">
                <span className="mr-2">⤓</span>{t("run.dropPrompt")}
              </p>
              <div className="mt-3 flex items-center gap-3 flex-wrap">
                <PickFileButton onPick={() => fileInputRef.current?.click()} disabled={running} />
              </div>
              <p className="mt-2 text-sm font-mono text-[var(--muted-fg)] italic">
                {t("run.noResumeWarn")}
              </p>
            </>
          )}
          {resumeError && (
            <p className="mt-3 text-sm font-mono">
              <span className="badge-outline mr-2">ERROR</span>
              {resumeError}
            </p>
          )}
        </div>
      </section>

      {/* === 段落 3：动作按?=== */}
      <section className="pt-6 border-t border-[var(--border-light)]">
        <div className="flex items-center gap-4 flex-wrap">
          <button
            onClick={handleStart}
            disabled={running || busy}
            className="btn"
          >
            <Play size={16} strokeWidth={2.5} aria-hidden />
            {t("btn.start")} <span className="ml-1">→</span>
          </button>
          <button
            onClick={handleStop}
            disabled={!running || busy}
            className="btn-outline"
          >
            <Square size={15} strokeWidth={2.5} aria-hidden />
            {t("btn.stop")}
          </button>
          <button
            onClick={handleLogin}
            disabled={running || busy}
            className="btn-outline"
          >
            <LogIn size={16} strokeWidth={2.25} aria-hidden />
            {t("btn.login")} · {platformLabel}
          </button>
          <button
            onClick={handleReset}
            disabled={running || busy}
            className="btn-ghost"
          >
            <RotateCcw size={15} strokeWidth={2.25} aria-hidden />
            {t("btn.resetChrome")}
          </button>
        </div>
        {/* ?始前校验?：内联报错（替代原生 alert），点哪?段缺???*/}
        {startError && (
          <div className="mt-4 border-2 border-[var(--ink)] p-3 flex items-start gap-3">
            <span className="badge-invert flex-shrink-0">{t("run.checkBadge")}</span>
            <span className="text-sm font-mono">{startError}</span>
          </div>
        )}
      </section>

      {/* === 段落 3.5：出错了？把日志发我（手?不自动上报）=== */}
      {hasError && (
        <section className="border-2 border-[var(--ink)] p-5 space-y-3 fade-in">
          <div className="flex items-center gap-3">
            <span className="badge-invert">
              <TriangleAlert size={13} strokeWidth={2.5} className="mr-1.5" aria-hidden />
              {t("run.errorTitle")}
            </span>
            <span className="text-sm font-serif italic text-[var(--muted-fg)]">
              {t("run.errorDesc")}
            </span>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <button type="button" onClick={copyAiHelp} className="btn text-sm">
              {t("askai.btn")}
            </button>
            <button type="button" onClick={copyLogs} className="btn-outline text-sm">
              <Copy size={15} strokeWidth={2.25} aria-hidden />
              {t("btn.copyLogs")}
            </button>
            <button
              type="button"
              onClick={() => ipc.openIssuesPage().catch(() => {})}
              className="btn-outline text-sm"
            >
              {t("btn.openIssues")}
            </button>
            {copyMsg && (
              <span className="text-sm font-mono text-[var(--muted-fg)]">{copyMsg}</span>
            )}
          </div>
          {logPaths && (
            <p className="text-sm font-mono text-[var(--muted-fg)] break-all">
              {t("run.logDir", { dir: logPaths.dir })}
              <button
                type="button"
                onClick={() =>
                  navigator.clipboard.writeText(logPaths.dir).catch(() => {})
                }
                className="ml-2 underline underline-offset-2 hover:opacity-70"
              >
                {t("btn.copyPath")}
              </button>
            </p>
          )}
        </section>
      )}

      {/* === 段落 4：双面板 === */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-0 border-2 border-[var(--ink)]">
        <Panel
          title={
            <span className="inline-flex items-center gap-2">
              <Activity size={15} strokeWidth={2.5} aria-hidden />
              {t("run.panelProgress")}
            </span>
          }
          rightDivider
        >
          <div
            ref={eventsScrollRef}
            className="text-sm font-mono log-body h-80 overflow-y-auto"
          >
            {events.length === 0 ? (
              <div className="text-[var(--muted-fg)] italic">
                {t("run.noEvents")}
              </div>
            ) : (
              events.map((ev, i) => <EventRow key={i} ev={ev} />)
            )}
          </div>
        </Panel>

        <Panel
          title={
            <span className="inline-flex items-center gap-2">
              <FileText size={15} strokeWidth={2.5} aria-hidden />
              {t("run.panelLog")}
            </span>
          }
        >
          {/* 日志区：黑底白字反色块，强化 monochrome 对比 */}
          <pre
            ref={logScrollRef}
            className="bg-[var(--ink)] text-[var(--paper)] p-4 text-sm font-mono h-80 overflow-y-auto whitespace-pre-wrap leading-relaxed"
          >
            {logs.length === 0 ? (
              <span className="opacity-50 italic">{t("run.noLogs")}</span>
            ) : (
              logs.join("\n")
            )}
          </pre>
        </Panel>
      </section>
    </div>
  );
}

// ?历上传的「?择文件…?按???有/无简历两???避免两?改一处漏?
// stopPropagation ?为它常嵌在可点击的虚线?里，?泡再触发??picker?
function PickFileButton({ onPick, disabled }: { onPick: () => void; disabled: boolean }) {
  const t = useT();
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onPick();
      }}
      disabled={disabled}
      className="btn-outline text-sm"
    >
      {t("btn.pickFile")}
    </button>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between">
        {label && <label className="field-label">{label}</label>}
        {hint && (
          <span className="text-[13px] font-mono uppercase tracking-widest text-[var(--muted-fg)] italic">
            {hint}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}

function Panel({
  title,
  children,
  rightDivider,
}: {
  title: React.ReactNode;
  children: React.ReactNode;
  rightDivider?: boolean;
}) {
  // panel 之间用一条黑色竖线分隔（desktop 下）
  return (
    <div
      className={[
        "p-6",
        rightDivider ? "lg:border-r-2 lg:border-[var(--ink)]" : "",
        "border-b-2 lg:border-b-0 border-[var(--ink)] last:border-b-0",
      ].join(" ")}
    >
      <h3 className="mono-tag mb-4 text-[var(--ink)]">{title}</h3>
      {children}
    </div>
  );
}

// 事件类型 ?视?表达
// monochrome 风：去掉?有?色?义，改用字重 + 反色 + 前缀符号
// - 重?正向（letter_sent / feed_exhausted / loop_ended）：反色徽章
// - 错?（error）：粗黑?+ 前缀 ?
// - 跳过类（job_skipped）：虚弱化文?
// - 其他：默?
type EventStyle = {
  variant: "default" | "invert" | "error" | "weak";
  prefix?: string;
};

const KIND_STYLES: Record<string, EventStyle> = {
  browser_started: { variant: "weak", prefix: "·" },
  login_ok: { variant: "default", prefix: "✓" },
  job_found: { variant: "default", prefix: "→" },
  job_skipped: { variant: "weak", prefix: "—" },
  scoring_degraded: { variant: "error", prefix: "⚠️" },
  letter_sent: { variant: "invert", prefix: "■" },
  feed_exhausted: { variant: "invert", prefix: "■" },
  loop_ended: { variant: "invert", prefix: "■" },
  error: { variant: "error", prefix: "▌" },
};

function EventRow({ ev }: { ev: ProgressEvent }) {
  const style = KIND_STYLES[ev.kind] ?? { variant: "default" };
  const payload = Object.entries(ev.payload)
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(" ");

  const classes = {
    default: "py-1 text-[var(--ink)]",
    invert:
      "py-1 px-2 -mx-2 my-0.5 bg-[var(--ink)] text-[var(--paper)] font-medium",
    error:
      "py-1 px-2 -mx-2 my-0.5 border-l-4 border-[var(--ink)] font-medium",
    weak: "py-1 text-[var(--muted-fg)]",
  }[style.variant];

  return (
    // fade-in：事件是追加渲染的，带上它只有**新来那行**淡入，不会整块闪
    <div className={`${classes} fade-in`}>
      {style.prefix && <span className="mr-2">{style.prefix}</span>}
      <span className="uppercase tracking-widest text-[13px]">[{ev.kind}]</span>{" "}
      <span className="opacity-80">{payload}</span>
    </div>
  );
}
