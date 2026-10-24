"""Adapters for observed DeepSeek website endpoints, not the model API."""

import asyncio
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx


class PaymentError(Exception):
    pass


class AuthenticationError(PaymentError):
    pass


def money(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("金额格式错误") from None
    if (
        not amount.is_finite()
        or amount < 1
        or amount > 100000
        or amount != amount.to_integral_value()
    ):
        raise ValueError("请输入 1 元至 100000 元之间的整数金额")
    return format(amount, ".2f")


class DeepSeekPayments:
    def __init__(self, token, client=None):
        self.routes = {
            "ALIPAY": ("CMB_UNIONPAY", "ALIPAY"),
            "WECHAT": ("CMB_UNIONPAY", "WECHAT"),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Origin": "https://platform.deepseek.com",
            "Referer": "https://platform.deepseek.com/top_up",
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-platform": "web",
            "x-client-version": "1.0.0",
            "x-client-locale": "zh_CN",
            "x-client-timezone-offset": "28800",
        }
        self.client = client or httpx.AsyncClient(
            base_url="https://platform.deepseek.com",
            headers=headers,
            timeout=20,
            follow_redirects=False,
        )

    async def close(self):
        await self.client.aclose()

    async def _request(self, method, path, **kwargs):
        try:
            response = await asyncio.wait_for(
                self.client.request(method, path, **kwargs), timeout=30
            )
            if response.status_code in (401, 403):
                raise AuthenticationError(
                    "DeepSeek 开放平台网页登录认证失败；请管理员更新配置后重载插件"
                )
            if response.status_code != 200:
                raise PaymentError("支付服务暂时不可用")
            body = response.json()
            if body["code"] != 0 or body["data"]["biz_code"] != 0:
                raise PaymentError("支付服务返回业务错误；请管理员检查登录状态")
            return body["data"]["biz_data"]
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            # Never echo upstream bodies, URLs with credentials, or login tokens.
            raise PaymentError("支付请求异常，结果尚未确认") from exc

    async def create(self, amount, request_id, channel="ALIPAY"):
        route = self.routes.get(channel)
        if not route or not all(route):
            raise ValueError("不支持的支付渠道")
        method, fallback = route
        payload = {
            "order_info": {
                "payment_method_type": method,
                "fallback_method_type": fallback,
                "amount": money(amount),
                "currency": "CNY",
                "request_id": request_id,
            }
        }
        data = await self._request(
            "POST",
            "/api/v1/payments",
            json=payload,
        )
        try:
            order_id, url = data["payment_order_id"], data["url"]
            parts = urlsplit(url)
            if (
                data["code"] != 0
                or money(data["amount"]) != money(amount)
                or not isinstance(order_id, str)
                or not order_id
                or parts.scheme != "https"
                or parts.hostname != "qr.95516.com"
                or parts.username
                or parts.password
                or parts.port not in (None, 443)
            ):
                raise ValueError("invalid payment")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PaymentError(
                "创建响应不符合已验证格式，需人工核对；请勿立即重复创建"
            ) from exc
        return order_id, url

    async def orders(self):
        data = await self._request("GET", "/auth-api/v0/users/get_all_invoice")
        try:
            orders = data["invoices"]["payment_orders"]
            if not isinstance(orders, list):
                raise TypeError()
            result = {}
            for order in orders:
                key = order["payment_order_id"]
                if not isinstance(key, str) or not key or key in result:
                    raise ValueError()
                result[key] = order
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise PaymentError("查单响应不符合已验证格式") from exc
