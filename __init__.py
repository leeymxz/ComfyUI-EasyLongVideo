# -*- coding: utf-8 -*-
"""ComfyUI-EasyLongVideo：零依赖的长音频分段生成插件。

- 节点注册：EasyLVUnified（长视频 · 音频分析与顺序生成）
- 前端面板：web/js/easy_long_video.js（自动加载）
- HTTP API：/elv/*（分段审核、试听、顺序生成、合成）
"""
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web/js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


def _register_routes():
    try:
        from .routes import register_routes
        register_routes()
    except Exception as exc:  # 面板与顺序生成不可用时，核心节点仍可手动使用
        print(f"[EasyLongVideo] API 路由注册失败（面板功能受限）：{exc}")


_register_routes()
