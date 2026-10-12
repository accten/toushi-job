import { useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  Briefcase,
  CircleAlert,
  Copy,
  Eraser,
  FileText,
  Flag,
  Globe,
  LogIn,
  Play,
  RotateCcw,
  Send,
  Settings2,
  ShieldCheck,
  SkipForward,
  Square,
  Terminal,
  TriangleAlert,
  Upload,
} from "lucide-react";
import { Channel } from "@tauri-apps/api/core";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { ipc, type ProgressEvent, type RunConfig, type ResumeInfo, type LlmConfig, type Platform } from "../lib/ipc";
import { useRunStore, useT } from "../store";
import { Badge, EmptyState, Field, Panel } from "../components/ui";

// 运行页布局：
//   上：运行参数 / 简历（两栏）  →  动作条  →  出错卡片  →  实时（事件时间线 + 日志终端）
// 参数区在运行中整体禁用；实时区自己滚动，不把整页顶长。
export default function RunPage() {
  const t = useT();
  // AI 端点在「配置」tab 设置；运行页只读展示 + 校验有没有配好 key
  const [llmCfg, setLlmCfg] = useState<LlmConfig | null>(null);
  const [llmError, setLlmError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // 简历（拖拽上传）
  const [resume, setResumeInfo] = useState<ResumeInfo | null>(null);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  // 开始前校验的内联报错（替代生硬的原生 alert）
  const [startError, setStartError] = useState<string | null>(null);
  // 出错卡片：复制日志反馈 + 日志目录（懒加载）
  const [copyMsg, setCopyMsg] = useState<string | null>(null);
  const [logPaths, setLogPaths] = useState<{ dir: string } | null>(null);
  // 日志自动滚动：默认开；用户往上翻看历史时"永远滚到底"很烦，给个开关
  const [autoScroll, setAutoScroll] = useState(true);

  // Global form states
  const usrName = useRunStore((s) => s.formUsrName);
  const label = useRunStore((s) => s.formLabel);
  const dryRun = useRunStore((s) => s.formDryRun);
  const platform = useRunStore((s) => s.formPlatform);
  const setFormState = useRunStore((s) => s.setFormState);
  // 平台显示名：按钮和日志里都带上，一眼能看出这个按钮/这次点击对应哪个平台
  // （曾经出现过"选了智联却开了 BOSS 窗口"，界面上完全看不出它认的平台）
  const platformLabel =
    platform === "zhilian" ? t("run.platformZhilian") : t("run.platformBoss");

  const running = useRunStore((s) => s.running);
  const events = useRunStore((s) => s.events);
  const logs = useRunStore((s) => s.logs);
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
      const nameField = fields.find((f) => f.key === "BOSS_USR_NAME");
      if (nameField && nameField.value && !useRunStore.getState().formUsrName) {
        stateUpdate.formUsrName = nameField.value;
      }

      const labelField = fields.find((f) => f.key === "BOSS_LABEL");
      if (labelField && labelField.value && !useRunStore.getState().formLabel) {
        stateUpdate.formLabel = labelField.value;
      }

      const platformField = fields.find((f) => f.key === "PLATFORM");
      if (platformField && platformField.value && !useRunStore.getState().formPlatform) {
        // 校验平台值，别把脏值透传给后端
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

  // 挂载时读回当前简历（standalone 下持久化在 app 数据目录的 resume/）
  useEffect(() => {
    ipc.getResume()
      .then(({ resume }) => setResumeInfo(resume))
      .catch(() => {});
  }, []);

  // 拖拽上传：监听 webview 的 drag-drop 事件。这是 core API（@tauri-apps/api/webview），
  // 已被 capabilities 里的 core:default 覆盖，不需要额外的 plugin / ACL 配置。
  // 用 useRunStore.getState() 读 running 的最新值，避免把 running 放进 deps 反复重订阅。
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

  // 文件选择器上传：webview 里 <input type=file> 给不到真实路径，只能读字节再传回去。
  // 跟拖拽并存——已有简历时也能点「选择文件」换，不是只剩拖拽一条路。
  const fileInputRef = useRef<HTMLInputElement>(null);

  function arrayBufferToBase64(buf: ArrayBuffer): string {
    const bytes = new Uint8Array(buf);
    let binary = "";
    const chunk = 0x8000; // 分块避免 String.fromCharCode 参数过多爆栈
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }

  async function onFilePicked(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // 重置：允许再次选同一个文件也能触发 change
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setResumeError(t("run.onlyPdf"));
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

  // 自动滚到底（可关）
  useEffect(() => {
    if (autoScroll && logScrollRef.current) {
      logScrollRef.current.scrollTop = logScrollRef.current.scrollHeight;
    }
  }, [logs.length, autoScroll]);
  useEffect(() => {
    if (autoScroll && eventsScrollRef.current) {
      eventsScrollRef.current.scrollTop = eventsScrollRef.current.scrollHeight;
    }
  }, [events.length, autoScroll]);

  async function handleStart() {
    // 开始前的拦截全部走内联 startError（不再用生硬的原生 alert）。
    // 校验顺序跟后端 start_run 的 pre-flight 对齐，让用户秒级看到缺了啥，
    // 而不是 run 跑一下才把错误埋进日志面板。
    setStartError(null);
    if (!usrName.trim()) {
      setStartError(t("run.errNeedName"));
      return;
    }

    // 只有 BOSS 平台才校验 LLM 配置（因为需要发送招呼语）
    if (platform === "boss") {
      if (!llmCfg || !llmCfg.hasKey) {
        setStartError(t("run.errNeedAi"));
        return;
      }
      if (!llmCfg.model) {
        // 缺 model 时后端 _build_client 会在后段抛错 break 掉整个 run，提前拦
        setStartError(t("run.errNeedModel"));
        return;
      }
    }

    if (!resume) {
      // 后端 start_run 会在 current_resume() is None 时抛 ValueError；前端拦，
      // 避免 run 一闪 on→off、错误埋在日志里
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

  // 手动登录：开一个不带调试端口的干净 Chrome（当前平台各自的 profile），
  // 用户在里面扫码登录，然后把窗口关掉再点「开始」——脚本复用同一 profile 的 cookie。
  // 用干净窗口而不是拿脚本那个自动化窗口去登录：挂了 CDP 的窗口会被平台风控
  // 反复送去安全验证（BOSS 上踩过 security.html 死循环，登录页根本没法扫）。
  async function handleLogin() {
    setBusy(true);
    pushLog(t("run.loginBusy", { platform: platformLabel }));
    try {
      const r = await ipc.openLoginWindow(platform);
      // 把平台名一起打出来：日志里写明开的是哪个平台的窗口，
      // 免得出现"我明明选了智联却开了 BOSS"这种分不清是谁错的情况
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

  // 出错时给"把日志发我"的入口：探测进度事件里有没有 error / loop_ended(error)
  const hasError = events.some(
    (e) => e.kind === "error" || (e.kind === "loop_ended" && e.payload?.reason === "error")
  );

  // 出错后懒加载一次日志目录路径，告诉用户文件在哪、可手动附带
  useEffect(() => {
    if (hasError && !logPaths) {
      ipc.getLogDir().then((p) => setLogPaths({ dir: p.dir })).catch(() => {});
    }
  }, [hasError, logPaths]);

  async function copyLogs() {
    // 复制当前日志面板缓冲到剪贴板——用户自己粘给作者 / 贴 issues，绝不自动发送
    try {
      await navigator.clipboard.writeText(logs.join("\n"));
      setCopyMsg(t("run.copyOk"));
    } catch {
      setCopyMsg(t("run.copyFail"));
    }
    setTimeout(() => setCopyMsg(null), 4000);
  }

  // 「复制信息问 AI」：跟顶栏那个同源——app 上下文 + 实时日志打包成求助文本。
  // 出错时这里给个就近入口，用户不用回到顶栏找。
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

  return (
    <div className="h-full flex flex-col gap-5 p-5 min-h-0">
      {/* ===== 参数 + 简历 ===== */}
      <div className="grid gap-5 lg:grid-cols-2 items-start flex-shrink-0">
        <Panel
          title={t("run.sectionParams")}
          icon={<Settings2 size={14} strokeWidth={2.25} aria-hidden />}
          actions={<span className="field-hint">{t("run.sectionParamsHint")}</span>}
        >
          <div className="grid sm:grid-cols-2 gap-x-6 gap-y-5">
            <Field
              label={t("run.fieldName")}
              right={t("run.hintRequired")}
              hint={t("run.fieldNamePlaceholder")}
            >
              <input
                type="text"
                value={usrName}
                onChange={(e) => setFormState({ formUsrName: e.target.value })}
                disabled={running}
                className="field-input"
              />
            </Field>

            <Field label={t("run.fieldPlatform")} right={t("run.hintPlatform")}>
              <select
                value={platform}
                onChange={(e) => setFormState({ formPlatform: e.target.value as Platform })}
                disabled={running}
                className="field-input"
              >
                <option value="boss">{t("run.platformBoss")}</option>
                <option value="zhilian">{t("run.platformZhilian")}</option>
              </select>
            </Field>

            {platform === "boss" && (
              <Field
                label={t("run.fieldLabel")}
                right={t("run.hintOptional")}
                hint={t("run.fieldLabelPlaceholder")}
              >
                <input
                  type="text"
                  value={label}
                  onChange={(e) => setFormState({ formLabel: e.target.value })}
                  disabled={running}
                  className="field-input"
                />
              </Field>
            )}

            <Field label={t("run.fieldEndpoint")} right={t("run.hintSetInConfig")}>
              {llmError ? (
                <div className="flex items-center gap-2 py-2 text-[13px] min-w-0">
                  <Badge variant="invert">ERR</Badge>
                  <span className="font-mono text-[12px] truncate" title={llmError}>
                    {t("run.llmReadFailed", { err: llmError })}
                  </span>
                </div>
              ) : !llmCfg || !llmCfg.hasKey ? (
                <div className="flex items-center gap-2 py-2 text-[13px] text-[var(--muted-fg)] min-w-0">
                  <Badge variant="outline">{t("run.noKey")}</Badge>
                  <span className="truncate">{t("run.goConfigForKey")}</span>
                </div>
              ) : (
                <div className="flex items-center gap-2 py-2 min-w-0">
                  <Badge variant="invert" title={llmCfg.model || undefined}>
                    <span className="truncate max-w-[13rem] block">
                      {llmCfg.model || t("run.noModelTag")}
                    </span>
                  </Badge>
                  <span className="font-mono text-[11px] text-[var(--muted-fg)] truncate">
                    {llmCfg.baseUrl || t("run.defaultEndpoint")}
                  </span>
                </div>
              )}
            </Field>

            <div className="sm:col-span-2 pt-3 border-t border-[var(--border-light)]">
              <label className="inline-flex items-center gap-3 cursor-pointer py-0.5">
                {/* 原生 checkbox：黑边方块，选中后填充墨色并显示 ×（比 ✓ 更贴这套排版） */}
                <span
                  className={[
                    "inline-flex items-center justify-center w-4 h-4 border-2 border-[var(--ink)] transition-colors duration-100",
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
                  <span className="text-[11px] font-bold leading-none">×</span>
                </span>
                <span className="text-[13px] font-medium">{t("run.dryRunDesc")}</span>
              </label>
            </div>
          </div>
        </Panel>

        <Panel
          title={t("run.sectionResume")}
          icon={<FileText size={14} strokeWidth={2.25} aria-hidden />}
          actions={<span className="field-hint">{t("run.resumeHint")}</span>}
        >
          {/* 隐藏的原生文件选择器：拖拽之外的另一条上传路径 */}
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
            onKeyDown={(e) => {
              if (!running && (e.key === "Enter" || e.key === " ")) fileInputRef.current?.click();
            }}
            className={[
              "border border-dashed p-5 transition-colors duration-100 min-h-[136px] flex flex-col justify-center",
              dragging ? "border-[var(--ink)] bg-[var(--muted-2)]" : "border-[var(--border)]",
              running
                ? "opacity-50"
                : "cursor-pointer hover:border-[var(--ink)] hover:bg-[var(--paper-2)]",
            ].join(" ")}
          >
            {resumeBusy ? (
              <p className="text-[13px] text-[var(--muted-fg)]">{t("run.uploading")}</p>
            ) : resume ? (
              <div className="flex items-center gap-3 min-w-0">
                <FileText size={22} strokeWidth={1.5} aria-hidden className="flex-shrink-0" />
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] font-medium truncate" title={resume.path}>
                    {resume.filename}
                  </p>
                  <p className="field-hint truncate">{t("run.replaceHint")}</p>
                </div>
                <PickFileButton
                  onPick={() => fileInputRef.current?.click()}
                  disabled={running}
                />
              </div>
            ) : (
              <div className="text-center">
                <Upload
                  size={20}
                  strokeWidth={1.5}
                  aria-hidden
                  className="mx-auto mb-2 text-[var(--muted-fg)]"
                />
                <p className="text-[13px] leading-relaxed max-w-sm mx-auto">
                  {t("run.dropPrompt")}
                </p>
                <div className="mt-3">
                  <PickFileButton
                    onPick={() => fileInputRef.current?.click()}
                    disabled={running}
                  />
                </div>
                <p className="field-hint mt-2">{t("run.noResumeWarn")}</p>
              </div>
            )}
            {resumeError && (
              <p className="mt-3 flex items-center gap-2 text-[13px]">
                <Badge variant="invert">ERR</Badge>
                <span className="font-mono text-[12px]">{resumeError}</span>
              </p>
            )}
          </div>
        </Panel>
      </div>

      {/* ===== 动作条 ===== */}
      <section className="card flex-shrink-0">
        <div className="card-body flex items-center gap-3 flex-wrap">
          <button onClick={handleStart} disabled={running || busy} className="btn">
            <Play size={15} strokeWidth={2.5} aria-hidden />
            {t("btn.start")}
          </button>
          <button onClick={handleStop} disabled={!running || busy} className="btn-outline">
            <Square size={14} strokeWidth={2.5} aria-hidden />
            {t("btn.stop")}
          </button>
          <button onClick={handleLogin} disabled={running || busy} className="btn-outline">
            <LogIn size={15} strokeWidth={2.25} aria-hidden />
            {t("btn.login")} · {platformLabel}
          </button>
          <button onClick={handleReset} disabled={running || busy} className="btn-ghost">
            <RotateCcw size={14} strokeWidth={2.25} aria-hidden />
            {t("btn.resetChrome")}
          </button>
          <span className="field-hint ml-auto">{t("run.actionsHint")}</span>
        </div>
        {/* 开始前校验区：内联报错（替代原生 alert），点哪缺哪一目了然 */}
        {startError && (
          <div className="px-5 pb-5 -mt-1">
            <div className="flex items-start gap-2.5 border border-[var(--ink)] px-3 py-2 bg-[var(--muted)] fade-in">
              <TriangleAlert
                size={14}
                strokeWidth={2.25}
                aria-hidden
                className="mt-0.5 flex-shrink-0"
              />
              <span className="text-[13px]">{startError}</span>
            </div>
          </div>
        )}
      </section>

      {/* ===== 出错了？把日志发我（手动，不自动上报） ===== */}
      {hasError && (
        <section className="card border-[var(--ink)] fade-up flex-shrink-0">
          <div className="card-body space-y-3">
            <div className="flex items-center gap-2.5 flex-wrap">
              <CircleAlert size={15} strokeWidth={2.25} aria-hidden />
              <span className="text-[13px] font-semibold">{t("run.errorTitle")}</span>
              <span className="field-hint">{t("run.errorDesc")}</span>
            </div>
            <div className="flex items-center gap-2.5 flex-wrap">
              <button type="button" onClick={copyAiHelp} className="btn text-[13px]">
                {t("askai.btn")}
              </button>
              <button type="button" onClick={copyLogs} className="btn-outline text-[13px]">
                <Copy size={14} strokeWidth={2.25} aria-hidden />
                {t("btn.copyLogs")}
              </button>
              <button
                type="button"
                onClick={() => ipc.openIssuesPage().catch(() => {})}
                className="btn-outline text-[13px]"
              >
                {t("btn.openIssues")}
              </button>
              {copyMsg && <span className="field-hint fade-in">{copyMsg}</span>}
            </div>
            {logPaths && (
              <p className="field-hint break-all">
                {t("run.logDir", { dir: logPaths.dir })}
                <button
                  type="button"
                  onClick={() => navigator.clipboard.writeText(logPaths.dir).catch(() => {})}
                  className="ml-2 underline underline-offset-2 hover:opacity-70"
                >
                  {t("btn.copyPath")}
                </button>
              </p>
            )}
          </div>
        </section>
      )}

      {/* ===== 实时：事件时间线 + 日志终端，占满剩余高度 ===== */}
      <div className="grid gap-5 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)] items-stretch flex-1 min-h-0">
        <Panel
          title={t("run.panelProgress")}
          icon={<Activity size={14} strokeWidth={2.25} aria-hidden />}
          actions={
            <span className="field-hint tabular-nums">
              {t("run.eventsCount", { n: events.length })}
            </span>
          }
          bodyClassName="p-0"
        >
          <div ref={eventsScrollRef} className="h-full overflow-y-auto">
            {events.length === 0 ? (
              <EmptyState
                icon={<Activity size={22} strokeWidth={1.5} aria-hidden />}
                title={t("run.noEvents")}
                hint={t("run.liveIdle")}
              />
            ) : (
              events.map((ev, i) => <EventRow key={i} ev={ev} />)
            )}
          </div>
        </Panel>

        <Panel
          title={t("run.panelLog")}
          icon={<Terminal size={14} strokeWidth={2.25} aria-hidden />}
          actions={
            <LogToolbar
              autoScroll={autoScroll}
              onToggleAutoScroll={() => setAutoScroll((v) => !v)}
              onCopy={copyLogs}
              onClear={() => {
                clear();
                setCopyMsg(t("run.cleared"));
                setTimeout(() => setCopyMsg(null), 2000);
              }}
              count={t("run.logsCount", { n: logs.length })}
            />
          }
          bodyClassName="p-0"
        >
          <pre
            ref={logScrollRef}
            className="h-full overflow-y-auto whitespace-pre-wrap break-words bg-[var(--ink)] text-[var(--paper)] px-4 py-3 text-[12px] leading-relaxed log-body m-0"
          >
            {logs.length === 0 ? (
              <span className="opacity-45 italic">{t("run.noLogs")}</span>
            ) : (
              logs.join("\n")
            )}
          </pre>
        </Panel>
      </div>
    </div>
  );
}

function LogToolbar({
  autoScroll,
  onToggleAutoScroll,
  onCopy,
  onClear,
  count,
}: {
  autoScroll: boolean;
  onToggleAutoScroll: () => void;
  onCopy: () => void;
  onClear: () => void;
  count: string;
}) {
  const t = useT();
  return (
    <>
      <span className="field-hint tabular-nums mr-1">{count}</span>
      <button
        type="button"
        className="icon-btn"
        data-on={autoScroll}
        onClick={onToggleAutoScroll}
        title={t("run.autoScroll")}
        aria-label={t("run.autoScroll")}
      >
        <ArrowDownToLine size={13} strokeWidth={2} aria-hidden />
      </button>
      <button
        type="button"
        className="icon-btn"
        onClick={onCopy}
        title={t("run.copyShort")}
        aria-label={t("run.copyShort")}
      >
        <Copy size={13} strokeWidth={2} aria-hidden />
      </button>
      <button
        type="button"
        className="icon-btn"
        onClick={onClear}
        title={t("run.clearShort")}
        aria-label={t("run.clearShort")}
      >
        <Eraser size={13} strokeWidth={2} aria-hidden />
      </button>
    </>
  );
}

// 简历上传的「选择文件…」按钮——有/无简历两种状态共用，避免两处改一处漏。
// stopPropagation 是因为它常嵌在可点击的虚线框里，冒泡会再次触发 file picker。
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
      className="btn-outline text-[12px] px-3 py-1.5 flex-shrink-0"
    >
      {t("btn.pickFile")}
    </button>
  );
}

/* ============ 事件时间线 ============ */

type Tone = "muted" | "normal" | "strong" | "danger";

const KIND_META: Record<string, { icon: React.ReactNode; tone: Tone; key: string }> = {
  browser_started: { icon: <Globe size={13} strokeWidth={2} />, tone: "muted", key: "ev.browser_started" },
  login_ok: { icon: <ShieldCheck size={13} strokeWidth={2} />, tone: "normal", key: "ev.login_ok" },
  job_found: { icon: <Briefcase size={13} strokeWidth={2} />, tone: "normal", key: "ev.job_found" },
  job_skipped: { icon: <SkipForward size={13} strokeWidth={2} />, tone: "muted", key: "ev.job_skipped" },
  scoring_degraded: { icon: <TriangleAlert size={13} strokeWidth={2} />, tone: "danger", key: "ev.scoring_degraded" },
  letter_sent: { icon: <Send size={13} strokeWidth={2} />, tone: "strong", key: "ev.letter_sent" },
  feed_exhausted: { icon: <Flag size={13} strokeWidth={2} />, tone: "strong", key: "ev.feed_exhausted" },
  loop_ended: { icon: <Flag size={13} strokeWidth={2} />, tone: "strong", key: "ev.loop_ended" },
  error: { icon: <CircleAlert size={13} strokeWidth={2} />, tone: "danger", key: "ev.error" },
};

const SKIP_REASON_KEY: Record<string, string> = {
  blacklist: "ev.reasonBlacklist",
  keyword: "ev.reasonKeyword",
  vector_search: "ev.reasonVector",
  score: "ev.reasonScore",
};

const LOOP_REASON_KEY: Record<string, string> = {
  completed: "ev.loopCompleted",
  cancelled: "ev.loopCancelled",
  error: "ev.loopError",
};

const STATUS_KEY: Record<string, string> = {
  sent: "ev.statusSent",
  dry_run: "ev.statusDryRun",
  blocked: "ev.statusBlocked",
};

type TFn = (k: string, v?: Record<string, string | number>) => string;

/** 把结构化事件翻成一句人话 + 一行细节。
 * 细节行保留 mono 小字，按 kind 拼装 payload 里能看懂的那几个字段。 */
function describeEvent(
  ev: ProgressEvent,
  t: TFn,
): { label: string; detail: string; tag?: string } {
  const p = ev.payload ?? {};
  const idx = typeof p.index === "number" ? "#" + p.index : "";

  switch (ev.kind) {
    case "job_found":
      return {
        label: (t("ev.job_found") + " " + idx).trim(),
        detail: typeof p.jd_preview === "string" ? p.jd_preview : "",
      };
    case "job_skipped": {
      const reasonKey = SKIP_REASON_KEY[String(p.reason)] ?? "ev.reasonUnknown";
      const bits: string[] = [];
      if (p.detail) bits.push(String(p.detail));
      if (typeof p.score === "number") {
        bits.push(t("ev.labelScore") + " " + p.score + "/" + (p.threshold ?? "?"));
      }
      if (Array.isArray(p.matched) && p.matched.length) {
        bits.push(t("ev.labelMatched") + " " + p.matched.join(", "));
      }
      return {
        label: (t("ev.job_skipped") + " " + idx).trim(),
        detail: bits.join(" · "),
        tag: t(reasonKey),
      };
    }
    case "letter_sent": {
      const statusKey = STATUS_KEY[String(p.status)] ?? "";
      const bits: string[] = [];
      if (typeof p.score === "number") bits.push(t("ev.labelScore") + " " + p.score);
      if (typeof p.letter_len === "number") bits.push(t("ev.labelLetterLen") + " " + p.letter_len);
      return {
        label: (t("ev.letter_sent") + " " + idx).trim(),
        detail: bits.join(" · "),
        tag: statusKey ? t(statusKey) : undefined,
      };
    }
    case "scoring_degraded":
      return { label: t("ev.scoring_degraded"), detail: String(p.detail ?? "") };
    case "feed_exhausted":
      return {
        label: t("ev.feed_exhausted"),
        detail: t("ev.labelTotal") + " " + (p.total ?? "?"),
      };
    case "loop_ended": {
      const reasonKey = LOOP_REASON_KEY[String(p.reason)] ?? "";
      return {
        label: t("ev.loop_ended"),
        detail: reasonKey ? t(reasonKey) : String(p.reason ?? ""),
      };
    }
    case "error":
      return {
        label: t("ev.error"),
        detail: [p.stage, p.message].filter(Boolean).join(" · "),
      };
    default:
      return { label: t(KIND_META[ev.kind]?.key ?? ev.kind), detail: "" };
  }
}

function EventRow({ ev }: { ev: ProgressEvent }) {
  const t = useT();
  const meta = KIND_META[ev.kind];
  const { label, detail, tag } = describeEvent(ev, t);

  const tone: Tone = meta?.tone ?? "normal";
  const iconColor =
    tone === "danger"
      ? "text-[var(--ink)]"
      : tone === "muted"
        ? "text-[var(--muted-fg)]"
        : "text-[var(--ink-soft)]";

  return (
    // fade-in：事件是追加渲染的，带上它只有新来那行淡入，不会整块闪
    <div
      className="flex items-start gap-2.5 px-4 py-2 border-b border-[var(--border-light)] last:border-b-0 fade-in"
      title={JSON.stringify(ev.payload)}
    >
      <span className={"mt-[3px] flex-shrink-0 " + iconColor}>{meta?.icon}</span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span
            className={
              tone === "danger"
                ? "text-[13px] font-semibold"
                : tone === "muted"
                  ? "text-[13px] text-[var(--muted-fg)]"
                  : "text-[13px]"
            }
          >
            {label}
          </span>
          {tag && <Badge variant={tone === "strong" ? "invert" : "outline"}>{tag}</Badge>}
        </div>
        {detail && (
          <p className="font-mono text-[11px] text-[var(--muted-fg)] leading-snug mt-0.5 break-all">
            {detail}
          </p>
        )}
      </div>
    </div>
  );
}
