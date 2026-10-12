/** @type {import('tailwindcss').Config} */
// Editorial Monochrome v2 —— 三档明度 + 发丝线 + 零圆角。
// 颜色全部走 CSS 变量（index.css），深色模式只反转 ink / paper，
// 所以这里不需要 dark: 变体，组件样式自动跟着翻。
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        // 正文：系统无衬线，中文回退到苹方 / 微软雅黑
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          '"Segoe UI"',
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Microsoft YaHei"',
          '"Noto Sans SC"',
          "Roboto",
          "sans-serif",
        ],
        // 展示（标题 / 大数字）：衬线
        serif: [
          "Georgia",
          '"Times New Roman"',
          '"Source Han Serif SC"',
          '"Songti SC"',
          '"宋体"',
          "serif",
        ],
        // 数据（日志 / 变量名 / 计数）
        mono: [
          '"JetBrains Mono"',
          "Consolas",
          "Menlo",
          '"SFMono-Regular"',
          '"Noto Sans Mono CJK SC"',
          "monospace",
        ],
      },
      colors: {
        ink: "var(--ink)",
        "ink-soft": "var(--ink-soft)",
        paper: "var(--paper)",
        "paper-2": "var(--paper-2)",
        muted: "var(--muted)",
        "muted-2": "var(--muted-2)",
        "muted-fg": "var(--muted-fg)",
        border: "var(--border)",
        "border-light": "var(--border-light)",
      },
      letterSpacing: {
        widest: "0.16em",
      },
      borderRadius: {
        // 全站零圆角
        DEFAULT: "0",
        sm: "0",
        md: "0",
        lg: "0",
        xl: "0",
        "2xl": "0",
        "3xl": "0",
        full: "0",
      },
      boxShadow: {
        none: "none",
        DEFAULT: "none",
      },
      transitionDuration: {
        DEFAULT: "120ms",
      },
    },
  },
  plugins: [],
};
