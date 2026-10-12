"""Web 界面入口。

启动方式：
    uv run python -m toushi.web

访问地址：
    http://localhost:5000
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
import queue
import time
import logging

# 添加 src 到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from flask import Flask, render_template, request, jsonify, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# 日志记录器
log = logging.getLogger(__name__)

# 全局状态
_runner_state = {
    "running": False,
    "platform": "boss",
    "process": None,
    "logs": [],
    "log_queue": queue.Queue(),
}


def _read_output(process, log_queue):
    """在后台线程读取子进程输出"""
    try:
        while True:
            # 读取 stdout
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                log_queue.put(("INFO", line.strip()))
            
            # 读取 stderr
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break
            if line:
                log_queue.put(("WARNING", line.strip()))
    except Exception as e:
        log_queue.put(("ERROR", f"读取输出出错: {e}"))
    
    # 进程结束
    return_code = process.poll()
    log_queue.put(("INFO", f"进程已结束，返回码: {return_code}"))


def _monitor_log_queue():
    """监控日志队列，更新到状态中"""
    print("[DEBUG] _monitor_log_queue started")
    while _runner_state["running"] or not _runner_state["log_queue"].empty():
        try:
            level, msg = _runner_state["log_queue"].get(timeout=0.1)
            print(f"[DEBUG] Got log: {level} - {msg[:50]}")
            _runner_state["logs"].append(f"[{level}] {msg}")
            # 保留最近500条日志
            if len(_runner_state["logs"]) > 500:
                _runner_state["logs"] = _runner_state["logs"][-500:]
        except queue.Empty:
            continue
    
    print("[DEBUG] _monitor_log_queue exiting, running=" + str(_runner_state["running"]))
    _runner_state["running"] = False


@app.route("/")
def index():
    """首页 - 重定向到运行页"""
    return redirect(url_for("run_page"))


@app.route("/run")
def run_page():
    """运行页面"""
    return render_template("run.html", state=_runner_state)


@app.route("/config")
def config_page():
    """配置页面"""
    return render_template("config.html")


@app.route("/history")
def history_page():
    """历史记录页面"""
    return render_template("history.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    """获取当前配置"""
    llm_api_key = os.getenv("LLM_API_KEY")
    log.info(f"LLM_API_KEY from os.getenv: set={bool(llm_api_key)}, length={len(llm_api_key or '')}")
    return jsonify({
        "llm_api_key": bool(llm_api_key),
        "llm_model": os.getenv("LLM_MODEL", ""),
        "llm_base_url": os.getenv("LLM_BASE_URL", ""),
        "boss_usr_name": os.getenv("BOSS_USR_NAME", ""),
        "boss_label": os.getenv("BOSS_LABEL", ""),
        "boss_resume_path": os.getenv("BOSS_RESUME_PATH", ""),
        "boss_dry_run": os.getenv("BOSS_DRY_RUN", "").lower() in ("1", "true", "yes"),
        "zhilian_usr_name": os.getenv("ZHILIAN_USR_NAME", ""),
        "zhilian_label": os.getenv("ZHILIAN_LABEL", ""),
        "zhilian_resume_path": os.getenv("ZHILIAN_RESUME_PATH", ""),
        "resume_path": os.getenv("RESUME_PATH", "resume/my_cover.pdf"),
        "dry_run": os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes"),
    })


@app.route("/api/config", methods=["POST"])
def save_config():
    """保存配置到 .env"""
    data = request.json
    env_path = Path(".env")
    
    # 项目根目录
    project_root = Path(__file__).parent.parent.parent.parent
    
    # 处理简历路径：相对路径转完整路径
    def to_absolute(path_str):
        if not path_str:
            return ""
        p = Path(path_str)
        if p.is_absolute():
            return str(p)
        # 相对路径，基于项目根目录
        return str(project_root / path_str)
    
    # 更新配置 - 转为完整路径
    updates = {
        "LLM_MODEL": data.get("llm_model", ""),
        "LLM_BASE_URL": data.get("llm_base_url", ""),
        "BOSS_USR_NAME": data.get("boss_usr_name", ""),
        "BOSS_LABEL": data.get("boss_label", ""),
        "BOSS_RESUME_PATH": to_absolute(data.get("boss_resume_path", "")),
        "BOSS_DRY_RUN": "1" if data.get("boss_dry_run") else "",
        "ZHILIAN_USR_NAME": data.get("zhilian_usr_name", ""),
        "ZHILIAN_LABEL": data.get("zhilian_label", ""),
        "ZHILIAN_RESUME_PATH": to_absolute(data.get("zhilian_resume_path", "")),
        # 兼容旧字段
        "RESUME_PATH": to_absolute(data.get("resume_path", "resume/my_cover.pdf")),
        "DRY_RUN": "1" if data.get("dry_run") else "",
    }
    # Special handling for LLM_API_KEY: only update if a non-empty value is provided
    # to avoid accidentally clearing it when the field is masked in the frontend
    llm_api_key = data.get("llm_api_key")
    if llm_api_key is not None and llm_api_key.strip() != "":
        updates["LLM_API_KEY"] = llm_api_key
    
    # 读取现有配置
    env_lines = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()
    
    # 合并配置
    updated_keys = set()
    new_lines = []
    for line in env_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key = stripped.split("=")[0].strip()
            if key in updates:
                updated_keys.add(key)
                new_lines.append(f"{key}={updates[key]}")
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    
    # 添加新配置
    for key, value in updates.items():
        if key not in updated_keys and value:
            new_lines.append(f"{key}={value}")
    
    # 写入文件
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    
    return jsonify({"success": True})


@app.route("/api/upload_resume", methods=["POST"])
def upload_resume():
    """上传简历文件到 resume/ 目录"""
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "没有文件"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "message": "未选择文件"}), 400
    
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({"success": False, "message": "只支持 PDF 文件"}), 400
    
    # 保存到 resume/ 目录
    resume_dir = Path(__file__).parent.parent.parent.parent / "resume"
    resume_dir.mkdir(exist_ok=True)
    
    # 生成唯一文件名（避免覆盖）
    import time
    timestamp = int(time.time())
    filename = f"resume_{timestamp}_{file.filename}"
    file_path = resume_dir / filename
    
    file.save(str(file_path))
    
    return jsonify({
        "success": True,
        "path": str(file_path),
        "filename": filename
    })


@app.route("/api/start", methods=["POST"])
def start_runner():
    """启动自动投递"""
    if _runner_state["running"]:
        return jsonify({"success": False, "message": "已在运行中"})
    
    data = request.json
    platform = data.get("platform", "boss")
    dry_run = data.get("dry_run", False)
    
    # 加载 .env 文件到环境变量
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    
    # 读取当前配置，作为环境变量传给子进程
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    
    # 根据平台选择对应的配置
    if platform == "zhilian":
        # 智联招聘配置
        if os.getenv("ZHILIAN_USR_NAME"):
            env["ZHILIAN_USR_NAME"] = os.getenv("ZHILIAN_USR_NAME")
            env["BOSS_USR_NAME"] = os.getenv("ZHILIAN_USR_NAME")  # 兼容
        if os.getenv("ZHILIAN_LABEL"):
            env["ZHILIAN_LABEL"] = os.getenv("ZHILIAN_LABEL")
            env["BOSS_LABEL"] = os.getenv("ZHILIAN_LABEL")  # 兼容
        if os.getenv("ZHILIAN_RESUME_PATH"):
            env["ZHILIAN_RESUME_PATH"] = os.getenv("ZHILIAN_RESUME_PATH")
            env["RESUME_PATH"] = os.getenv("ZHILIAN_RESUME_PATH")  # 兼容
    else:
        # BOSS 直聘配置
        if os.getenv("BOSS_USR_NAME"):
            env["BOSS_USR_NAME"] = os.getenv("BOSS_USR_NAME")
        if os.getenv("BOSS_LABEL"):
            env["BOSS_LABEL"] = os.getenv("BOSS_LABEL")
        if os.getenv("BOSS_RESUME_PATH"):
            env["BOSS_RESUME_PATH"] = os.getenv("BOSS_RESUME_PATH")
            env["RESUME_PATH"] = os.getenv("BOSS_RESUME_PATH")  # 兼容
        elif os.getenv("RESUME_PATH"):
            env["RESUME_PATH"] = os.getenv("RESUME_PATH")
        if dry_run or os.getenv("BOSS_DRY_RUN", "").lower() in ("1", "true", "yes"):
            env["DRY_RUN"] = "1"
            env["BOSS_DRY_RUN"] = "1"
    
    # 构建命令 - 直接运行 python 避免 uv 缓冲
    cmd = [sys.executable, "-m", "toushi"]
    cmd.extend(["--platform", platform])
    # 设置 PYTHONUNBUFFERED 确保子进程实时输出
    env["PYTHONUNBUFFERED"] = "1"
    # dry_run 通过环境变量传递，不需要命令行参数
    
    try:
        # 启动子进程 - 使用行缓冲 (bufsize=1) 确保实时输出
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # 行缓冲，实时输出
            env=env,
            cwd=str(Path(__file__).parent.parent.parent.parent),  # 项目根目录
        )
        
        _runner_state["running"] = True
        _runner_state["platform"] = platform
        _runner_state["logs"] = []
        _runner_state["process"] = process
        
        # 启动读取输出的线程
        output_thread = threading.Thread(
            target=_read_output,
            args=(process, _runner_state["log_queue"]),
            daemon=True,
        )
        output_thread.start()
        
        # 启动监控日志的线程
        monitor_thread = threading.Thread(
            target=_monitor_log_queue,
            daemon=True,
        )
        monitor_thread.start()
        
        return jsonify({"success": True, "message": f"已启动 {platform} 模式"})
    
    except Exception as e:
        return jsonify({"success": False, "message": f"启动失败: {str(e)}"})


@app.route("/api/stop", methods=["POST"])
def stop_runner():
    """停止自动投递"""
    if not _runner_state["running"]:
        return jsonify({"success": True, "message": "未在运行"})
    
    process = _runner_state.get("process")
    if process and process.poll() is None:
        try:
            process.terminate()
            # 等待进程结束
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            _runner_state["logs"].append("[INFO] 已停止进程")
        except Exception as e:
            _runner_state["logs"].append(f"[ERROR] 停止进程失败: {e}")
    
    _runner_state["running"] = False
    _runner_state["process"] = None
    
    return jsonify({"success": True, "message": "已停止"})


@app.route("/api/status")
def get_status():
    """获取运行状态"""
    # 检查进程是否还在运行
    process = _runner_state.get("process")
    if process and _runner_state["running"]:
        if process.poll() is not None:
            _runner_state["running"] = False
            _runner_state["logs"].append(f"[INFO] 进程已结束，返回码: {process.returncode}")
    
    return jsonify({
        "running": _runner_state["running"],
        "platform": _runner_state["platform"],
    })


@app.route("/api/logs")
def get_logs():
    """获取日志"""
    return jsonify({"logs": _runner_state["logs"][-100:]})  # 最近100条


@app.route("/api/history")
def get_history():
    """获取历史记录"""
    logs_path = Path("logs/letters.jsonl")
    if not logs_path.exists():
        return jsonify({"history": []})
    
    history = []
    for line in logs_path.read_text(encoding="utf-8").splitlines()[-50:]:  # 最近50条
        try:
            import json
            entry = json.loads(line)
            history.append(entry)
        except:
            pass
    
    return jsonify({"history": history})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
