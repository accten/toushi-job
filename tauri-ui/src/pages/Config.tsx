import { useEffect, useState } from "react";
import { Bot, Languages, MessageSquareQuote, SlidersHorizontal, User } from "lucide-react";
import { ipc, type EnvField, type LlmConfig } from "../lib/ipc";
import { useRunStore, useT } from "../store";
import { LANGS, type Lang } from "../lib/i18n";
import { Badge, EmptyState, KeyTag, Panel } from "../components/ui";

// 配置页：.env 编辑。
//
// 布局分四段：工具栏（保存）→ AI 端点 → 按用途分组的变量 → 招呼语 prompt。
// 「AI 端点」单独拎出来是因为它三件套（base_url / key / model）要走预设选择器，
// 不渲染成通用裸框；其余变量按用途分组，比一长条平铺好找。
const CUSTOM = ""; // 下拉里「自定义」的值

// 变量分组。key 不在这里出现的一律落到「其它变量」，所以新增后端字段不会漏渲染。
const GROUPS: { id: string; icon: React.ReactNode; keys: string[] }[] = [
  {
    id: "you",
    icon: <User size={14} strokeWidth={2.25} aria-hidden />,
    keys: ["BOSS_USR_NAME", "BOSS_LABEL"],
  },
  {
    id: "filter",
    icon: <SlidersHorizontal size={14} strokeWidth={2.25} aria-hidden />,
    keys: ["BOSS_MIN_MATCH_SCORE", "BOSS_MIN_KEYWORD_MATCH", "BOSS_EXCLUDE_KEYWORDS"],
  },
  {
    id: "browser",
    icon: <SlidersHorizontal size={14} strokeWidth={2.25} aria-hidden />,
    keys: [
      "BOSS_CHROME_PROFILE",
      "ZHILIAN_CHROME_PROFILE",
      "BOSS_RECOMMEND_URL",
      "ZHILIAN_RECOMMEND_URL",
    ],
  },
];

export default function ConfigPage() {
  const t = useT();
  const lang = useRunStore((s) => s.lang);
  const setLang = useRunStore((s) => s.setLang);
  const [fields, setFields] = useState<EnvField[]>([]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // LLM 端点段
  const [llmCfg, setLlmCfg] = useState<LlmConfig | null>(null);
  const [preset, setPreset] = useState<string>(CUSTOM);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  // null = 用户没动 key 框（保存时不传 key，不覆盖已存的）
  const [keyEdit, setKeyEdit] = useState<string | null>(null);
  // 明文/密文切换：粘贴 key 后能核对一眼，避免带错空格/漏字符跑到一半才报 401
  const [showKey, setShowKey] = useState(false);

  // 招呼语自定义 prompt 段（空字符串 = 用内置默认）
  const [letterPrompt, setLetterPrompt] = useState("");
  const [letterPromptInitial, setLetterPromptInitial] = useState("");
  const [letterPromptDefault, setLetterPromptDefault] = useState("");

  useEffect(() => {
    refresh();
  }, []);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [{ fields }, cfg, lp] = await Promise.all([
        ipc.getEnvFields(),
        ipc.getLlmConfig(),
        ipc.getLetterPrompt(),
      ]);
      setFields(fields);
      setEdits({});
      setLlmCfg(cfg);
      setBaseUrl(cfg.baseUrl);
      setModel(cfg.model);
      setKeyEdit(null);
      setLetterPrompt(lp.prompt);
      setLetterPromptInitial(lp.prompt);
      setLetterPromptDefault(lp.default);
      // 按 baseUrl 匹配预设来高亮下拉；匹配不上就是「自定义」
      const match = cfg.presets.find((p) => p.baseUrl === cfg.baseUrl);
      setPreset(match ? match.name : CUSTOM);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  function setField(key: string, value: string) {
    setSaved(false);
    setEdits((prev) => ({ ...prev, [key]: value }));
  }

  function valueFor(key: string, fallback: string): string {
    return key in edits ? edits[key] : fallback;
  }

  function pickPreset(name: string) {
    setSaved(false);
    setPreset(name);
    if (name === CUSTOM) return; // 自定义：保留用户当前填的
    const p = llmCfg?.presets.find((x) => x.name === name);
    if (p) {
      setBaseUrl(p.baseUrl);
      setModel(p.model);
    }
  }

  function editBaseUrl(v: string) {
    setSaved(false);
    setBaseUrl(v);
    setPreset(CUSTOM); // 手改即视为自定义
  }
  function editModel(v: string) {
    setSaved(false);
    setModel(v);
    setPreset(CUSTOM);
  }
  function editKey(v: string) {
    setSaved(false);
    setKeyEdit(v);
  }

  const llmDirty =
    llmCfg !== null &&
    (baseUrl !== llmCfg.baseUrl || model !== llmCfg.model || keyEdit !== null);
  const promptDirty = letterPrompt !== letterPromptInitial;
  const dirty = Object.keys(edits).length > 0 || llmDirty || promptDirty;

  async function save() {
    setSaving(true);
    setError(null);
    try {
      if (llmDirty) {
        await ipc.setLlmConfig(baseUrl.trim(), model.trim(), keyEdit ?? "");
      }
      if (Object.keys(edits).length > 0) {
        await ipc.writeEnvFields(edits);
      }
      if (promptDirty) {
        // 末尾留白没意义，trim 掉；空串 = 删除自定义 = 回退默认
        await ipc.setLetterPrompt(letterPrompt.trim());
      }
      setSaved(true);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  const selectedPreset = llmCfg?.presets.find((p) => p.name === preset);

  // 切 UI 语言：立刻切（setLang，全 App 重渲染）+ 落到 .env（独立保存，不进
  // 上面的 dirty/保存按钮流程，因为它是即时偏好不是表单字段）。持久化失败只是
  // 下次启动回到默认，不打断当前会话，所以静默兜错。
  function changeLang(next: Lang) {
    setLang(next);
    ipc.setLanguage(next).catch(() => {});
  }

  function editPrompt(v: string) {
    setSaved(false);
    setLetterPrompt(v);
  }

  // 按分组把 fields 切好；没匹配上任何分组的进「其它变量」
  const grouped = GROUPS.map((g) => ({
    ...g,
    fields: g.keys
      .map((k) => fields.find((f) => f.key === k))
      .filter((f): f is EnvField => Boolean(f)),
  }));
  const claimed = new Set(GROUPS.flatMap((g) => g.keys));
  const others = fields.filter((f) => !claimed.has(f.key));

  return (
    <div className="h-full overflow-y-auto p-5">
      <div className="space-y-5">
      {/* ===== 工具栏：保存 / 状态 ===== */}
      <section className="card">
        <div className="card-body flex items-center gap-3 flex-wrap">
          <p className="field-hint max-w-xl">{t("config.groupHint")}</p>
          <div className="ml-auto flex items-center gap-3">
            {saved && !dirty && <Badge variant="invert">{t("config.saved")}</Badge>}
            {dirty && <Badge variant="outline">{t("config.unsaved")}</Badge>}
            <button onClick={save} disabled={!dirty || saving} className="btn">
              {saving ? t("btn.saving") : t("btn.save")}
            </button>
          </div>
        </div>
      </section>

      {/* ===== 错误带 ===== */}
      {error && (
        <div className="card border-[var(--ink)] fade-in">
          <div className="card-body flex items-start gap-3">
            <Badge variant="invert">ERROR</Badge>
            <pre className="text-[12px] font-mono whitespace-pre-wrap flex-1 m-0">{error}</pre>
          </div>
        </div>
      )}

      {loading ? (
        <div className="card">
          <EmptyState title={t("common.loading")} />
        </div>
      ) : (
        <>
          {/* ===== AI 端点 ===== */}
          {llmCfg && (
            <Panel
              title={t("config.endpoint")}
              icon={<Bot size={14} strokeWidth={2.25} aria-hidden />}
              actions={<span className="field-hint">{t("config.endpointHint")}</span>}
              bodyClassName="card-body space-y-4"
            >
              {/* 常用快捷：默认「自定义」，选了自动填 base_url + model */}
              <Row label={t("config.preset")}>
                <select
                  value={preset}
                  onChange={(e) => pickPreset(e.target.value)}
                  className="field-input"
                >
                  <option value={CUSTOM}>{t("config.custom")}</option>
                  {llmCfg.presets.map((p) => (
                    <option key={p.name} value={p.name}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </Row>

              <Row label="LLM_BASE_URL">
                <input
                  type="text"
                  value={baseUrl}
                  onChange={(e) => editBaseUrl(e.target.value)}
                  className="field-input font-mono text-[13px]"
                  placeholder={t("config.baseUrlPlaceholder")}
                />
              </Row>

              <Row label="LLM_MODEL">
                <input
                  type="text"
                  value={model}
                  onChange={(e) => editModel(e.target.value)}
                  className="field-input font-mono text-[13px]"
                  placeholder={t("config.modelPlaceholder")}
                />
              </Row>

              <Row
                label="LLM_API_KEY"
                note={
                  selectedPreset
                    ? t("config.signup", { url: selectedPreset.signupUrl })
                    : undefined
                }
              >
                <div>
                  <div className="flex items-center gap-2">
                    <input
                      type={showKey ? "text" : "password"}
                      value={keyEdit ?? ""}
                      onChange={(e) => editKey(e.target.value)}
                      className="field-input font-mono text-[13px] flex-1"
                      placeholder={
                        llmCfg.hasKey
                          ? t("config.keyConfiguredPlaceholder")
                          : t("config.keyEmptyPlaceholder")
                      }
                    />
                    <button
                      type="button"
                      onClick={() => setShowKey((v) => !v)}
                      className="btn-ghost text-[12px] flex-shrink-0"
                      aria-label={showKey ? t("config.hideKeyAria") : t("config.showKeyAria")}
                    >
                      {showKey ? t("btn.hide") : t("btn.show")}
                    </button>
                  </div>
                  {/* 选了预设但还没配 key：提示去填，别等点开始才发现没 key */}
                  {selectedPreset && !llmCfg.hasKey && !keyEdit && (
                    <p className="field-hint mt-1.5">
                      {t("config.presetKeyHint", { label: selectedPreset.label })}
                    </p>
                  )}
                </div>
              </Row>
            </Panel>
          )}

          {/* ===== 按用途分组的通用变量 ===== */}
          <div className="grid gap-5 lg:grid-cols-2 items-start">
            {grouped.map((g) => (
              <Panel key={g.id} title={t("config.group." + g.id)} icon={g.icon} bodyClassName="p-0">
                {g.fields.length === 0 ? (
                  <EmptyState title="—" />
                ) : (
                  g.fields.map((f) => (
                    <EnvRow
                      key={f.key}
                      field={f}
                      value={valueFor(f.key, f.value)}
                      onChange={(v) => setField(f.key, v)}
                      t={t}
                    />
                  ))
                )}
              </Panel>
            ))}

            {/* 其它变量：后端新加的字段没归类时落这里，不会漏 */}
            <Panel
              title={t("config.group.other")}
              icon={<SlidersHorizontal size={14} strokeWidth={2.25} aria-hidden />}
              bodyClassName="p-0"
            >
              {others.length === 0 ? (
                <EmptyState title="—" />
              ) : (
                others.map((f) => (
                  <EnvRow
                    key={f.key}
                    field={f}
                    value={valueFor(f.key, f.value)}
                    onChange={(v) => setField(f.key, v)}
                    t={t}
                  />
                ))
              )}
            </Panel>

            {/* 界面语言：标签刻意双语，切错语言也能找回；选即生效，不走保存流程 */}
            <Panel
              title={t("config.language")}
              icon={<Languages size={14} strokeWidth={2.25} aria-hidden />}
              bodyClassName="card-body"
            >
              <select
                value={lang}
                onChange={(e) => changeLang(e.target.value as Lang)}
                className="field-input"
              >
                {LANGS.map((l) => (
                  <option key={l.value} value={l.value}>
                    {l.label}
                  </option>
                ))}
              </select>
              <p className="field-hint mt-3">{t("config.liveNote")}</p>
            </Panel>
          </div>

          {/* ===== 招呼语 Prompt ===== */}
          <Panel
            title={t("config.letterPrompt")}
            icon={<MessageSquareQuote size={14} strokeWidth={2.25} aria-hidden />}
            bodyClassName="card-body space-y-3"
          >
            <p className="field-hint">{t("config.letterPromptHint")}</p>
            <textarea
              value={letterPrompt}
              onChange={(e) => editPrompt(e.target.value)}
              rows={8}
              className="field-input text-[12px] leading-relaxed"
              placeholder={letterPromptDefault || t("config.letterPromptPlaceholder")}
            />
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => editPrompt("")}
                disabled={letterPrompt === ""}
                className="btn-ghost text-[12px]"
              >
                {t("btn.resetDefault")}
              </button>
              {letterPrompt === "" && (
                <span className="field-hint">{t("config.letterPromptPlaceholder")}</span>
              )}
            </div>
          </Panel>
        </>
      )}
      </div>
    </div>
  );
}

/** AI 端点那几个字段的行：左边标签固定宽，右边控件。
 * 标签要么是文案（预设），要么是变量名（显示成 chip）。 */
function Row({
  label,
  note,
  children,
}: {
  label: string;
  note?: string;
  children: React.ReactNode;
}) {
  const isEnvKey = label.includes("_");
  return (
    <div className="grid grid-cols-1 md:grid-cols-[180px_minmax(0,1fr)] gap-2 md:gap-6 md:items-start">
      <div className="md:pt-1.5">
        {isEnvKey ? <KeyTag>{label}</KeyTag> : <span className="field-label mb-0">{label}</span>}
        {note && <p className="field-hint mt-1.5 break-words">{note}</p>}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

/** 分组面板里的一行环境变量。 */
function EnvRow({
  field,
  value,
  onChange,
  t,
}: {
  field: EnvField;
  value: string;
  onChange: (v: string) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  return (
    <div className="px-5 py-4 border-b border-[var(--border-light)] last:border-b-0">
      <div className="flex items-baseline gap-2 flex-wrap mb-1">
        <span className="text-[13px] font-medium">{t("field." + field.key)}</span>
        <KeyTag>{field.key}</KeyTag>
        {field.isSecret && <Badge variant="muted">{t("config.secret")}</Badge>}
      </div>
      <input
        type={field.isSecret ? "password" : "text"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="field-input font-mono text-[12px]"
        placeholder={field.isSecret ? t("config.secretSetPlaceholder") : ""}
      />
    </div>
  );
}
