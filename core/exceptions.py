"""统一异常返回，让前端只需要处理一种错误结构。"""
from rest_framework.views import exception_handler as drf_exception_handler


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        # 交给 Django 走 500，由全局日志记录堆栈
        return None

    detail = response.data
    payload = {
        "code": response.status_code,
        "message": "请求失败",
        "errors": detail,
    }
    if isinstance(detail, dict) and "detail" in detail and len(detail) == 1:
        payload["message"] = str(detail["detail"])
    elif isinstance(detail, list):
        payload["message"] = "; ".join(str(item) for item in detail)
    response.data = payload
    return response
