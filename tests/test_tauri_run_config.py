"""RunConfig 的字段契约回归测试。

踩过的坑：**前端选智联、实际跑 BOSS** —— 因为 RunConfig 里没声明 platform，
而 pydantic 对没声明的字段默认是静默丢弃（extra=ignore），后端那句
getattr(config, "platform", None) 就永远拿到 None，一路 fallback 成 boss。
"""
from __future__ import annotations


def test_run_config_keeps_platform_from_frontend():
    from toushi.tauri import RunConfig

    # 前端 pyInvoke("start_run", { config: {...config, platform}, ... }) 就是这个形状
    cfg = RunConfig(
        **{"usrName": "测试", "label": "", "dryRun": True, "platform": "zhilian"}
    )
    assert cfg.platform == "zhilian"
    assert cfg.dry_run is True
    assert cfg.usr_name == "测试"


def test_run_config_platform_defaults_to_boss():
    from toushi.tauri import RunConfig

    assert RunConfig(usr_name="x").platform == "boss"
