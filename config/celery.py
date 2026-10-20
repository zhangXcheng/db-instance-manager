"""Celery 应用。

在 config/__init__.py 中导入，保证 Django 启动时 celery app 已完成初始化，
worker 通过 `celery -A config worker` 启动时可自动发现各 app 下的 tasks.py。
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("db_instance_manager")

# namespace='CELERY' 表示只读取 settings 中以 CELERY_ 开头的配置项
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):  # pragma: no cover
    print(f"Request: {self.request!r}")
