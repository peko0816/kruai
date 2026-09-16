"""支付 Provider 抽象。

规范性文件 —— 原样复制到 backend/app/services/payments/base.py，签名不得改动。

设计目标：**换收单机构不应波及任何业务逻辑。**
ABA PayWay 只是"接入 KHQR 的一个通道"，不是锁定项 —— KHQR 是柬埔寨国家银行
的统一商户 QR 标准，经 Bakong 与所有参与银行和主要钱包互通。换通道时用户的
扫码体验不变，变的只是结算方。

=== 金额纪律（重要，与 v1 不同）===
柬埔寨是双币种经济：USD 有 2 位小数，KHR 有 0 位小数。因此**不能用 "cents"
这个语义**。约定：

  · 用户支付金额：`amount_minor` —— 该币种的**最小货币单位**整数
                  + `currency_minor_units` —— 该币种的小数位数
                  USD 1.99 → amount_minor=199, currency_minor_units=2
                  KHR 8000 → amount_minor=8000, currency_minor_units=0
  · 内部成本核算：一律 USD，字段名 `cost_usd_cents`，语义明确不需要额外元数据

出现 float / Decimal 即为 bug。

=== 续费形态 ===
两种路径都要支持，由 provider 能力决定走哪条（见 supports_recurring）：
  · auto   —— 渠道支持代扣：首次支付时建立 mandate，之后周期性 charge_recurring
  · manual —— 渠道不支持代扣：到期前推送提醒，用户一键再买一次

业务层据 `supports_recurring` 分支，**不得** 假设任一种。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PaymentStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"
    UNKNOWN = "unknown"


class ProductKind(str, Enum):
    SUBSCRIPTION = "subscription"   # Basic / Pro 订阅
    TOPUP = "topup"                 # 实时语音超量包


class RenewalMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


@dataclass(frozen=True)
class Money:
    """金额。amount_minor 是该币种最小单位的整数。"""
    amount_minor: int
    currency: str                   # "USD" | "KHR"
    currency_minor_units: int       # USD=2, KHR=0

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be int; float money is a bug")


@dataclass(frozen=True)
class CheckoutRequest:
    order_id: str                   # 我方唯一订单号，必须幂等
    money: Money
    product_kind: ProductKind
    product_name: str
    user_ref: str                   # 我方 user id，不得放 PII
    #: 请求在本次支付中同时建立代扣授权。provider 不支持时应忽略，
    #: 并在 CheckoutResult.mandate_ref 返回 None，不得报错。
    setup_recurring: bool = False
    return_url: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckoutResult:
    ok: bool
    #: 跳转支付页；与 qr_payload 至少有一个非空
    checkout_url: str | None = None
    #: KHQR 字符串，前端自行渲染二维码
    qr_payload: str | None = None
    provider_ref: str | None = None
    #: 代扣授权标识。仅当请求了 setup_recurring 且 provider 支持时非空。
    mandate_ref: str | None = None
    expires_at_epoch: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ChargeResult:
    """周期性代扣的结果。"""
    ok: bool
    status: PaymentStatus = PaymentStatus.UNKNOWN
    provider_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class CallbackResult:
    """回调验签与解析的结果。

    signature_valid 为 False 时**必须拒绝开通订单**，不得"先放行再对账"。
    这是订阅自动开通的安全边界。
    """
    signature_valid: bool
    order_id: str | None = None
    provider_ref: str | None = None
    status: PaymentStatus = PaymentStatus.UNKNOWN
    money: Money | None = None
    #: 首次支付若建立了代扣授权，回调可能在此带回
    mandate_ref: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(ABC):
    """支付 Provider。

    实现类必须保证：
      1. create_checkout 对同一 order_id 幂等 —— 重复调用返回同一笔
      2. verify_callback 只做验签与解析，**不得**有任何副作用（不写库、不开通）
      3. 不抛异常 —— 失败以 ok=False / signature_valid=False 返回
      4. 如实申报 supports_recurring 与 supported_currencies
    """

    name: str
    #: 是否支持代扣。False 时业务层走手动续费路径。
    #: 参考：Telegram Stars 为 False；多数柬埔寨本地钱包为 False。
    supports_recurring: bool
    #: 该通道能结算的币种，如 ("USD",) 或 ("USD", "KHR")
    supported_currencies: tuple[str, ...]

    @abstractmethod
    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        raise NotImplementedError

    @abstractmethod
    def verify_callback(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
        content_type: str,
    ) -> CallbackResult:
        """验签并解析回调。同步方法 —— 不应有 IO。"""
        raise NotImplementedError

    @abstractmethod
    async def query_status(self, order_id: str) -> PaymentStatus:
        """主动查单，用于回调丢失时的对账兜底。"""
        raise NotImplementedError

    async def charge_recurring(
        self,
        *,
        mandate_ref: str,
        order_id: str,
        money: Money,
        product_name: str,
    ) -> ChargeResult:
        """按已建立的授权发起周期扣款。

        默认实现返回不支持 —— 只有 supports_recurring=True 的 provider 才重写本方法。
        调用方必须先检查 supports_recurring，不得依赖异常来判断能力。
        """
        return ChargeResult(
            ok=False,
            status=PaymentStatus.FAILED,
            error_code="recurring.unsupported",
            error_message=f"{self.name} does not support recurring charges",
        )

    async def cancel_mandate(self, *, mandate_ref: str) -> bool:
        """撤销代扣授权。默认实现返回 True（无授权可撤）。"""
        return True
