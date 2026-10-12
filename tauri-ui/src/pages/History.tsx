import { useEffect, useMemo, useState } from "react";
import { ChevronRight, Coins, Inbox, RefreshCw, Search } from "lucide-react";
import { ipc, type LetterRecord, type TelemetrySummary } from "../lib/ipc";
import { useT } from "../store";
import { Badge, EmptyState, Panel, Segmented } from "../components/ui";

// 历史页：先概览（成本 / 条数），再明细（可过滤、可搜索、点行展开 JD）。
// 状态表达仍然延续 monochrome：徽章 + 符号，不用红绿。
type Filter = "all" | "sent" | "dry" | "failed";

export default function HistoryPage() {
  const [letters, setLetters] = useState<LetterRecord[]>([]);
  const [summary, setSummary] = useState<TelemetrySummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const t = useT();

  useEffect(() => {
    refresh();
  }, []);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [{ letters }, { summary }] = await Promise.all([
        ipc.getLetters(200),
        ipc.getTelemetrySummary(),
      ]);
      setLetters(letters.slice().reverse()); // 最新的在最上
      setSummary(summary);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  const counts = useMemo(
    () => ({
      all: letters.length,
      sent: letters.filter((l) => l.sent).length,
      dry: letters.filter((l) => !l.sent && l.dry_run).length,
      failed: letters.filter((l) => !l.validation_ok).length,
    }),
    [letters],
  );

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return letters.filter((l) => {
      if (filter === "sent" && !l.sent) return false;
      if (filter === "dry" && !(!l.sent && l.dry_run)) return false;
      if (filter === "failed" && l.validation_ok) return false;
      if (q && !(l.letter + " " + l.job_description).toLowerCase().includes(q)) return false;
      return true;
    });
  }, [letters, filter, query]);

  return (
    <div className="h-full overflow-y-auto p-5">
      <div className="space-y-5">
      {/* ===== 工具栏 ===== */}
      <section className="card">
        <div className="card-body flex items-center gap-3 flex-wrap">
          <span className="field-hint">{t("history.expandHint")}</span>
          <div className="ml-auto flex items-center gap-3">
            <span className="field-hint tabular-nums">{t("history.rows", { n: shown.length })}</span>
            <button onClick={refresh} disabled={loading} className="btn-outline">
              <RefreshCw size={14} strokeWidth={2.25} aria-hidden />
              {t("btn.refresh")}
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

      {/* ===== 成本汇总 ===== */}
      {summary && (
        <Panel
          title={t("history.costHeading")}
          icon={<Coins size={14} strokeWidth={2.25} aria-hidden />}
          bodyClassName="card-body"
        >
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-x-8 gap-y-6">
            <Stat label={t("history.statLetters")} value={String(counts.all)} emphasis />
            <Stat label={t("history.statTotalCalls")} value={String(summary.total_calls)} emphasis />
            <Stat
              label={t("history.statTotalCost")}
              value={summary.total_cost_cny.toFixed(4)}
              emphasis
            />
            {Object.entries(summary.by_provider).map(([provider, s]) => (
              <Stat
                key={provider}
                label={t("history.statProviderCalls", { provider, calls: s.calls })}
                value={"¥ " + s.cost_cny.toFixed(4)}
                sub={"in " + s.input_tokens + " · out " + s.output_tokens}
              />
            ))}
          </div>
        </Panel>
      )}

      {/* ===== 招呼语明细 ===== */}
      <Panel
        title={t("history.lettersHeading")}
        icon={<Inbox size={14} strokeWidth={2.25} aria-hidden />}
        actions={
          <div className="flex items-center gap-2">
            <div className="relative">
              <Search
                size={13}
                strokeWidth={2}
                aria-hidden
                className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--muted-fg)]"
              />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t("history.searchPlaceholder")}
                className="w-52 bg-transparent border border-[var(--border-light)] pl-7 pr-2 py-1 text-[12px] outline-none focus:border-[var(--border)]"
              />
            </div>
            <Segmented<Filter>
              value={filter}
              onChange={setFilter}
              options={[
                { value: "all", label: t("history.filterAll"), count: counts.all },
                { value: "sent", label: t("history.filterSent"), count: counts.sent },
                { value: "dry", label: t("history.filterDry"), count: counts.dry },
                { value: "failed", label: t("history.filterFailed"), count: counts.failed },
              ]}
            />
          </div>
        }
        bodyClassName="p-0"
      >
        {shown.length === 0 ? (
          <EmptyState
            icon={<Inbox size={22} strokeWidth={1.5} aria-hidden />}
            title={loading ? t("common.loading") : letters.length === 0 ? t("history.empty") : t("history.noMatch")}
          />
        ) : (
          <table className="w-full text-[13px] border-collapse">
            <thead>
              <tr className="border-b border-[var(--border)]">
                <Th>{t("history.thTime")}</Th>
                <Th>Provider</Th>
                <Th>Model</Th>
                <Th align="center">{t("history.thValidation")}</Th>
                <Th align="center">{t("history.thSent")}</Th>
                <Th>{t("history.thLetter")}</Th>
                <Th align="center"> </Th>
              </tr>
            </thead>
            <tbody>
              {shown.map((l, i) => (
                <LetterRow key={l.ts + "-" + i} l={l} />
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      </div>
    </div>
  );
}

/** 后端可能给 "none" / 空串（智联记录没走 LLM），统一显示成占位符。 */
function dashIfEmpty(v: string | null | undefined): string {
  if (!v || v === "none") return "—";
  return v;
}

function Th({ children, align = "left" }: { children: React.ReactNode; align?: "left" | "center" }) {
  return (
    <th
      className={[
        "px-4 py-2.5 font-normal text-[11px] uppercase tracking-wider text-[var(--muted-fg)] bg-[var(--paper-2)]",
        align === "center" ? "text-center" : "text-left",
      ].join(" ")}
    >
      {children}
    </th>
  );
}

function Stat({
  label,
  value,
  sub,
  emphasis,
}: {
  label: string;
  value: string;
  sub?: string;
  emphasis?: boolean;
}) {
  // emphasis 项加左侧 2px 墨线，强化层级（不用加粗描边）
  return (
    <div className={emphasis ? "border-l-2 border-[var(--ink)] pl-3" : "pl-3"}>
      <div className="mono-tag">{label}</div>
      <div className="font-serif text-[26px] leading-none tracking-tight mt-2 tabular-nums">
        {value}
      </div>
      {sub && <div className="font-mono text-[11px] text-[var(--muted-fg)] mt-1.5">{sub}</div>}
    </div>
  );
}

function LetterRow({ l }: { l: LetterRecord }) {
  const [expanded, setExpanded] = useState(false);
  const t = useT();
  const time = new Date(l.ts).toLocaleString();

  return (
    <>
      <tr
        className="border-b border-[var(--border-light)] cursor-pointer transition-colors duration-100 hover:bg-[var(--muted)] align-top"
        onClick={() => setExpanded(!expanded)}
      >
        <td className="px-4 py-2.5 font-mono text-[12px] whitespace-nowrap">{time}</td>
        <td className="px-4 py-2.5 font-mono text-[12px]">{dashIfEmpty(l.provider)}</td>
        <td className="px-4 py-2.5 font-mono text-[12px] text-[var(--muted-fg)]">
          {dashIfEmpty(l.model)}
        </td>
        <td className="px-4 py-2.5 text-center">
          {l.validation_ok ? (
            <span className="font-mono text-[12px]">✓ OK</span>
          ) : (
            <span
              className="font-mono text-[12px] border-b border-current"
              title={l.validation_reasons.join(", ")}
            >
              ✗ FAIL
            </span>
          )}
        </td>
        <td className="px-4 py-2.5 text-center">
          {l.sent ? (
            <Badge variant="invert">{t("history.sentBadge")}</Badge>
          ) : l.dry_run ? (
            <Badge variant="outline">{t("history.dryBadge")}</Badge>
          ) : (
            <span className="font-mono text-[12px] text-[var(--muted-fg)]">—</span>
          )}
        </td>
        <td className="px-4 py-2.5 leading-snug">
          {l.letter ? (
            expanded ? (
              l.letter
            ) : (
              l.letter.slice(0, 60) + (l.letter.length > 60 ? "…" : "")
            )
          ) : (
            // 智联只投递不发招呼语，这类记录 letter 是空串——别让整列看着像坏了
            <span className="text-[var(--muted-fg)]">—</span>
          )}
        </td>
        <td className="px-4 py-2.5 text-center text-[var(--muted-fg)]">
          <ChevronRight
            size={13}
            strokeWidth={2.25}
            aria-hidden
            className={"inline transition-transform duration-100 " + (expanded ? "rotate-90" : "")}
          />
        </td>
      </tr>
      {expanded && (
        <tr className="bg-[var(--paper-2)] border-b border-[var(--border)]">
          <td colSpan={7} className="px-4 py-4">
            <div className="space-y-3 max-w-3xl">
              {!l.validation_ok && (
                <div className="border-l-2 border-[var(--ink)] pl-3">
                  <Badge variant="invert">VALIDATION FAILED</Badge>
                  <p className="font-mono text-[12px] mt-1.5">
                    {l.validation_reasons.join(", ")}
                  </p>
                </div>
              )}
              <div>
                <span className="mono-tag block mb-1.5">{t("history.jdPreview")}</span>
                <p className="text-[13px] leading-relaxed text-[var(--ink-soft)]">
                  {l.job_description.slice(0, 300)}
                  {l.job_description.length > 300 && "…"}
                </p>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
