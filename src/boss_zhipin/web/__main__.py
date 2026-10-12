"""Web 界面入口模块。

启动方式：
    uv run python -m boss_zhipin.web

访问地址：
    http://localhost:5000
"""

from boss_zhipin.web import app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
