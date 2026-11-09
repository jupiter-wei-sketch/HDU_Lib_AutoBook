# -*- coding: utf-8 -*-
"""路径工具：兼容源码运行与 PyInstaller 打包后的运行环境。"""
import os
import sys


def resource_path(rel):
    """读取随程序分发的只读资源（如座位表）。"""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS  # PyInstaller 解压目录
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def data_dir():
    """用户数据目录：配置与日志都写在这里，避免安装目录无写权限。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "HDUAutoBook")
    os.makedirs(path, exist_ok=True)
    return path


def settings_path():
    return os.path.join(data_dir(), "settings.json")


def log_dir():
    path = os.path.join(data_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path
