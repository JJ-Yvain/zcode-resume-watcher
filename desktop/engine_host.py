# -*- coding: utf-8 -*-
"""把单文件引擎装进桌面壳:导入、读配置、按需隔离运行环境。

引擎仍是唯一判定/守卫实现;这里只负责加载与路径重定向,不改判定逻辑。
"""
import importlib.util
import os
import sys


def import_engine(root):
    """只导入模块,不读配置、不写日志。单实例互斥体要在读配置之前抢。"""
    root = os.path.abspath(root)
    path = os.path.join(root, "zcode_resume_watcher.py")
    spec = importlib.util.spec_from_file_location("zcode_resume_watcher", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def configure_engine(mod, root, config_path=None, log_file=None, log_dir=None,
                     rollout_dir=None, db_path=None, suppress_config_log=False):
    """加载配置;路径参数用于隔离测试。

    必须在 ``load_config`` 之前设置 LOG_FILE/NAV_LOG,否则配置加载那一行会
    写进真实日志;加载后再覆盖一次,防 config.json 里的 log_dir/rollout_dir/
    db_path 把隔离环境带回真实目录。
    """
    root = os.path.abspath(root)

    if log_file:
        mod.LOG_FILE = os.path.abspath(log_file)
        mod.NAV_LOG = os.path.join(os.path.dirname(mod.LOG_FILE), "navigate.log")
    if log_dir:
        mod.LOG_DIR = os.path.abspath(log_dir)
    if rollout_dir:
        mod.ROLLOUT_DIR = os.path.abspath(rollout_dir)
    if db_path:
        mod.DB_PATH = os.path.abspath(db_path)

    original = mod.log
    if suppress_config_log:
        mod.log = lambda _msg: None
    try:
        mod.load_config(config_path or os.path.join(root, "config.json"))
    finally:
        mod.log = original

    if log_file:
        mod.LOG_FILE = os.path.abspath(log_file)
        mod.NAV_LOG = os.path.join(os.path.dirname(mod.LOG_FILE), "navigate.log")
    if log_dir:
        mod.LOG_DIR = os.path.abspath(log_dir)
    if rollout_dir:
        mod.ROLLOUT_DIR = os.path.abspath(rollout_dir)
    if db_path:
        mod.DB_PATH = os.path.abspath(db_path)
    return mod


def load_engine(root, config_path=None, log_file=None, log_dir=None,
                rollout_dir=None, db_path=None, suppress_config_log=False):
    """导入并配置(预览/一次性工具用);桌面 app 走 import + configure 两步。"""
    mod = import_engine(root)
    return configure_engine(
        mod, root, config_path=config_path, log_file=log_file, log_dir=log_dir,
        rollout_dir=rollout_dir, db_path=db_path,
        suppress_config_log=suppress_config_log)
