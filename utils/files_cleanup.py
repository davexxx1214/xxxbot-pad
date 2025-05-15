#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
图片文件自动清理模块
根据配置的天数自动清理files目录中的图片文件
"""

import os
import time
import asyncio
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

# 配置日志
from loguru import logger

class FilesCleanup:
    """图片文件自动清理类"""

    def __init__(self, config=None, config_path="main_config.toml"):
        """初始化清理器

        Args:
            config: 配置字典，如果提供则直接使用，否则从config_path加载
            config_path: 配置文件路径，仅在config为None时使用
        """
        self.config_path = config_path
        self.files_dir = os.path.join(os.getcwd(), "files")

        # 如果提供了配置字典，直接使用
        if config is not None:
            self.cleanup_days = self._get_cleanup_days_from_config(config)
        else:
            # 否则从配置文件加载
            self.cleanup_days = self._load_config()

    def _get_cleanup_days_from_config(self, config):
        """从配置字典获取清理天数设置

        Args:
            config: 配置字典

        Returns:
            int: 清理天数，0表示禁用清理
        """
        try:
            # 获取XYBot部分的files-cleanup-days配置
            cleanup_days = config.get("XYBot", {}).get("files-cleanup-days", 7)
            logger.info(f"已加载图片文件清理配置: {cleanup_days}天")
            return cleanup_days
        except Exception as e:
            logger.error(f"从配置字典获取清理天数失败: {e}")
            # 默认返回7天
            return 7

    def _load_config(self):
        """从配置文件加载清理天数设置

        Returns:
            int: 清理天数，0表示禁用清理
        """
        try:
            with open(self.config_path, "rb") as f:
                config = tomllib.load(f)

            return self._get_cleanup_days_from_config(config)
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            # 默认返回7天
            return 7

    def should_cleanup(self):
        """检查是否应该执行清理

        Returns:
            bool: 是否应该执行清理
        """
        # 如果cleanup_days为0，表示禁用清理
        if self.cleanup_days <= 0:
            logger.info("图片文件自动清理功能已禁用")
            return False

        # 检查files目录是否存在
        if not os.path.exists(self.files_dir):
            logger.warning(f"files目录不存在: {self.files_dir}")
            return False

        return True

    async def cleanup(self):
        """执行文件清理操作"""
        if not self.should_cleanup():
            return

        logger.info(f"开始清理超过{self.cleanup_days}天的图片文件...")

        # 计算截止时间
        cutoff_time = time.time() - (self.cleanup_days * 24 * 60 * 60)
        cutoff_date = datetime.fromtimestamp(cutoff_time)

        # 获取files目录中的所有文件
        files_path = Path(self.files_dir)
        total_files = 0
        deleted_files = 0

        # 支持的图片扩展名
        image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']

        try:
            # 创建files目录（如果不存在）
            if not os.path.exists(self.files_dir):
                os.makedirs(self.files_dir, exist_ok=True)
                logger.info(f"已创建files目录: {self.files_dir}")

            for file_path in files_path.iterdir():
                if file_path.is_file():
                    total_files += 1

                    # 检查文件扩展名
                    if file_path.suffix.lower() in image_extensions:
                        # 获取文件修改时间
                        file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)

                        # 如果文件修改时间早于截止时间，则删除
                        if file_mtime < cutoff_date:
                            try:
                                file_path.unlink()
                                deleted_files += 1
                                logger.debug(f"已删除过期图片文件: {file_path.name}, 修改时间: {file_mtime}")
                                # 每删除10个文件暂停一下，避免系统负载过高
                                if deleted_files % 10 == 0:
                                    await asyncio.sleep(0.1)
                            except Exception as e:
                                logger.error(f"删除文件失败: {file_path}, 错误: {e}")

            logger.info(f"图片文件清理完成: 共检查{total_files}个文件, 删除{deleted_files}个过期文件")

        except Exception as e:
            logger.error(f"清理图片文件时发生错误: {e}")

    @staticmethod
    def schedule_cleanup(config=None):
        """计划定期执行清理

        Args:
            config: 配置字典，包含files-cleanup-days设置

        Returns:
            callable: 可以传递给定时任务的函数
        """
        async def _cleanup_task(bot=None):
            cleaner = FilesCleanup(config)
            await cleaner.cleanup()

        return _cleanup_task


# 如果直接运行此脚本，则执行一次清理
if __name__ == "__main__":
    import asyncio

    async def main():
        cleaner = FilesCleanup()
        await cleaner.cleanup()

    # 运行异步主函数
    asyncio.run(main())
