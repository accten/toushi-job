#!/usr/bin/env bash

# 投石 · Toushi 一键启动脚本
# 支持 Windows (Git Bash) 环境下的交互式启动

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}
log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}
log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检测操作系统并设置 Windows 特定环境变量
if [[ "$OSTYPE" == "msys"* ]] || [[ "$OSTYPE" == "cygwin"* ]]; then
    export BOSS_NO_SANDBOX=1
    log_info "检测到 Windows 环境，已设置 BOSS_NO_SANDBOX=1"
fi

# 检查 .env 文件
if [[ ! -f ".env" ]]; then
    log_warn ".env 文件不存在，请先复制 .env.example 为 .env 并填写必要的配置"
    log_warn "例如：cp .env.example .env 然后编辑 .env 填写 LLM_API_KEY 等"
    # 不退出，因为智联平台不需要 LLM API Key
else
    # 检查 LLM_API_KEY 是否已设置（但注意：智联平台不需要）
    if ! grep -q "LLM_API_KEY" .env || [[ -z $(grep "LLM_API_KEY" .env | cut -d'=' -f2) ]]; then
        log_warn ".env 中未找到 LLM_API_KEY 或值为空"
        log_warn "如果您选择 BOSS 直聘平台，这将导致失败"
        log_warn "智联招聘平台不需要 LLM API Key"
    fi
fi

# 交互式选择平台
echo
echo "请选择求职平台："
echo "  1) BOSS 直聘 (发送招呼语)"
echo "  2) 智联招聘 (仅投递)"
read -p "请输入选项 [1/2，默认1]: " platform_choice
platform_choice=${platform_choice:-1}

case $platform_choice in
    1)
        platform="boss"
        platform_name="BOSS 直聘"
        ;;
    2)
        platform="zhilian"
        platform_name="智联招聘"
        ;;
    *)
        log_warn "无效选项，默认使用 BOSS 直聘"
        platform="boss"
        platform_name="BOSS 直聘"
        ;;
esac

# 交互式选择运行模式
echo
echo "请选择运行模式："
echo "  1) DRY-RUN 模式（仅生成招呼语，不发送）"
echo "  2) 真实发送模式"
read -p "请输入选项 [1/2，默认1]: " dry_run_choice
dry_run_choice=${dry_run_choice:-1}

if [[ $dry_run_choice == "1" ]]; then
    export DRY_RUN=1
    log_info "已设置 DRY_RUN=1（仅生成不发送）"
else
    export DRY_RUN=0
    log_info "已设置 DRY_RUN=0（将真实发送招呼语）"
fi

# 交互式选择启动方式
echo
echo "请选择启动方式："
echo "  1) 图形界面 (GUI) - 推荐给新用户"
echo "  2) 命令行界面 (CLI)"
read -p "请输入选项 [1/2，默认1]: " mode_choice
mode_choice=${mode_choice:-1}

if [[ $mode_choice == "1" ]]; then
    # 启动 GUI
    log_info "正在启动 $platform_name 图形界面..."
    log_info "请在配置页填写 API Key 等信息后保存"
    log_info "然后切换到运行页，填写用户名并点击开始"
    uv run python -m toushi.tauri
else
    # 启动 CLI
    log_info "正在启动 $platform_name 命令行界面..."
    log_info "请确保 .env 中已正确配置 LLM_API_KEY（如果选择 BOSS 直聘）"
    log_info "首次运行需要手动扫码登录，此后将免扫码"
    uv run main.py --platform "$platform"
fi

# 提示用户
echo
log_info "脚本执行完毕。如果遇到问题，请查看上方日志或使用「问 AI 帮忙」功能。"