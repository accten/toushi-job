import type { ReactNode } from "react";

/** 全站通用小组件。
 * 设计系统在 index.css 里（.card / .btn / .tag-key …），这里只做组合，
 * 避免每个页面重复六行 <div className="...">。
 */

/* ---------------- 卡片 ---------------- */

export function Panel({
  title,
  icon,
  actions,
  children,
  className = "",
  bodyClassName = "card-body",
}: {
  title?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={"card flex flex-col min-h-0 " + className}>
      {title !== undefined && (
        <header className="card-head">
          {icon && <span className="text-[var(--muted-fg)]">{icon}</span>}
          <h3 className="text-[13px] font-semibold tracking-wide">{title}</h3>
          {actions && <div className="ml-auto flex items-center gap-1">{actions}</div>}
        </header>
      )}
      <div className={bodyClassName + " flex-1 min-h-0"}>
        {children}
      </div>
    </section>
  );
}

/* ---------------- 表单 ---------------- */

export function Field({
  label,
  hint,
  right,
  children,
  className = "",
}: {
  label?: ReactNode;
  hint?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      {(label || right) && (
        <div className="flex items-baseline justify-between gap-3 mb-1">
          {label && <span className="field-label mb-0">{label}</span>}
          {right && <span className="field-hint">{right}</span>}
        </div>
      )}
      {children}
      {hint && <p className="field-hint mt-1">{hint}</p>}
    </div>
  );
}

/* ---------------- 徽章 ---------------- */

export function Badge({
  children,
  variant = "outline",
  title,
}: {
  children: ReactNode;
  variant?: "invert" | "outline" | "muted";
  title?: string;
}) {
  const cls = { invert: "badge-invert", outline: "badge-outline", muted: "badge-muted" }[variant];
  return (
    <span className={cls} title={title}>
      {children}
    </span>
  );
}

/** 变量名 chip，如 LLM_BASE_URL */
export function KeyTag({ children }: { children: ReactNode }) {
  return <span className="tag-key">{children}</span>;
}

/* ---------------- 空状态 ---------------- */

export function EmptyState({
  icon,
  title,
  hint,
}: {
  icon?: ReactNode;
  title: string;
  hint?: string;
}) {
  return (
    <div className="empty">
      {icon && <span className="opacity-40">{icon}</span>}
      <p className="text-sm">{title}</p>
      {hint && <p className="text-[12px] max-w-xs leading-relaxed opacity-80">{hint}</p>}
    </div>
  );
}

/* ---------------- 分段控件 ---------------- */

export function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: string; count?: number }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="segmented">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          data-active={value === o.value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
          {o.count !== undefined && (
            <span className="ml-1.5 opacity-60">{o.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}

/* ---------------- 运行中的不确定进度条 ---------------- */

export function ProgressBar({ active }: { active: boolean }) {
  return <div className="progress-track" data-active={active} />;
}
