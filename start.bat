@echo off
:: BossZhiPin_Job_Search 一键启动脚本 (Windows批处理版)
:: 交互式选择平台、运行模式和启动方式

:: 颜色定义（仅在支持的终端中有效）
:: 0=黑, 1=蓝, 2=绳, 3=青, 4=红, 5=紫, 6=黄, 7=白, 8=灰, 9=淡蓝, A=淡绿, B=淡青, C=淡红, D=淡紫, E=淡黄, F=亮白
:: 我们使用简单的echo，不做复杂着色

:: 设置local环境，避免影响全局
setlocal

:: 检测是否在Windows环境中（显然是）
:: 为BOSS直聘设置沙箱禁用（Windows通常需要）
set "BOSS_NO_SANDBOX=1"
echo [INFO] 检测到 Windows 环境，已设置 BOSS_NO_SANDBOX=1

:: 检查 .env 文件
if not exist .env (
    echo [WARN] .env 文件不存在，请先复制 .env.example 为 .env 并填写必要的配置
    echo [WARN] 例如：copy .env.example .env 然后编辑 .env 填写 LLM_API_KEY 等
    :: 不退出，因为智联平台不需要 LLM API Key
) else (
    :: 检查 LLM_API_KEY 是否已设置（但注意：智联平台不需要）
    findstr /i "LLM_API_KEY" .env >nul
    if errorlevel 1 (
        echo [WARN] .env 中未找到 LLM_API_KEY
        echo [WARN] 如果您选择 BOSS 直聘平台，这将导致失败
        echo [WARN] 智联招聘平台不需要 LLM API Key
    ) else (
        for /f "tokens=2 delims==" %%a in ('findstr /i "LLM_API_KEY" .env') do set "LLM_VAL=%%a"
        if defined LLM_VAL if "%%LLM_VAL%%"=="" (
            echo [WARN] .env 中 LLM_API_KEY 值为空
            echo [WARN] 如果您选择 BOSS 直聘平台，这将导致失败
        )
    )
)

:: 交互式选择平台
echo.
echo 请选择求职平台：
echo   1) BOSS 直聘 (发送招呼语)
echo   2) 智联招聘 (仅投递)
set /p "platform_choice=请输入选项 [1/2，默认1]: "
if "%platform_choice%"=="" set "platform_choice=1"

if "%platform_choice%"=="1" (
    set "platform=boss"
    set "platform_name=BOSS 直聘"
) else if "%platform_choice%"=="2" (
    set "platform=zhilian"
    set "platform_name=智联招聘"
) else (
    echo [WARN] 无效选项，默认使用 BOSS 直聘
    set "platform=boss"
    set "platform_name=BOSS 直聘"
)

:: 交互式选择运行模式
echo.
echo 请选择运行模式：
echo   1) DRY-RUN 模式（仅生成招呼语，不发送）
echo   2) 真实发送模式
set /p "dry_run_choice=请输入选项 [1/2，默认1]: "
if "%dry_run_choice%"=="" set "dry_run_choice=1"

if "%dry_run_choice%"=="1" (
    set "DRY_RUN=1"
    echo [INFO] 已设置 DRY_RUN=1（仅生成不发送）
) else (
    set "DRY_RUN=0"
    echo [INFO] 已设置 DRY_RUN=0（将真实发送招呼语）
)

:: 交互式选择启动方式
echo.
echo 请选择启动方式：
echo   1) 图形界面 (GUI) - 推荐给新用户
echo   2) 命令行界面 (CLI)
set /p "mode_choice=请输入选项 [1/2，默认1]: "
if "%mode_choice%"=="" set "mode_choice=1"

if "%mode_choice%"=="1" (
    :: 启动 GUI
    echo [INFO] 正在启动 %platform_name% 图形界面...
    echo [INFO] 请在配置页填写 API Key 等信息后保存
    echo [INFO] 然后切换到运行页，填写用户名并点击开始
    uv run python -m toushi.tauri
) else (
    :: 启动 CLI
    echo [INFO] 正在启动 %platform_name% 命令行界面...
    echo [INFO] 请确保 .env 中已正确配置 LLM_API_KEY（如果选择 BOSS 直聘）
    echo [INFO] 首次运行需要手动扫码登录，此后将免扫码
    uv run main.py --platform %platform%
)

:: 提示用户
echo.
echo [INFO] 脚本执行完毕。如果遇到问题，请查看上方日志或使用「问 AI 帮忙」功能。
endlocal