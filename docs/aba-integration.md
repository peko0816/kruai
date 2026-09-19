# D9：ABA PayWay 接入指引

**这份文件的用途**：把 D9 从「一个待办」变成「一张填空题」。抽象层已经把改动面
压到最小，缺的全部是 M0-3 才能拿到的**外部事实**。

---

## 1. 为什么现在写不了

四样东西只存在于 ABA 商户后台与他们登录后的文档里，**一样都猜不出来**：

| 缺什么 | 没有它会怎样 |
|---|---|
| 商户凭证（`PAYWAY_MERCHANT_ID` / `PAYWAY_API_KEY`，沙箱即可） | 连不上，收银台创建不了 |
| **`HASH_FIELD_ORDER`** —— 签名字段的拼接顺序 | **每一个回调都验签失败**。顺序错一位就全错，而且症状是「所有支付都开通不了」 |
| 回调的字段名与状态取值 | 解析出来全是空，或者把失败读成成功 |
| 该商户实际开通了哪些支付方式（KHQR / 卡 / 钱包） | 用户在收银台看到一个自己用不了的选项 |

按想象的字段名写出来的 adapter，会通过作者自己写的测试，然后在第一次接触真实回调
时失败。**那是最糟的一种绿**：测试全过，钱收不到，而且要等到真实用户投诉才发现。

对应 M0-3 的通过标准（`docs/DEFINITION_OF_DONE.md`）：凭证有效 + 收银台可创建 +
回调验签通过（SIGNATURE OK）+ 确认已开通的支付方式。**那四条就是上面这张表。**

另：PRD 15.3 第 2 条把「ABA 商户资质与结算主体」列为需要人工确认的事项。

---

## 2. 落地时要改什么（完整清单）

```
backend/app/services/payments/aba.py        新增，实现 3 个抽象方法
backend/app/services/payments/registry.py   _FACTORIES 加一行
.env / docs/CONFIG_REFERENCE.md             PAYWAY_* 填值，PAYMENT_PROVIDERS 加 aba
```

**其余一律不动**：checkout 端点、webhook 端点、对账任务、续费任务、订阅开通、
entitlements、Bot、前端。这是 ARCHITECTURE 3.3 那张「换收单机构的改动面」表的
实测结果，不是愿望——D8a/D8b 全程只与 `PaymentProvider` 打交道。

### 2.1 要实现的三个方法

```python
class AbaPayWay(PaymentProvider):
    name = "aba"
    supports_recurring = ...   # 见 2.2
    supported_currencies = ("USD", "KHR")   # 以商户实际开通为准

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult: ...
    def verify_callback(self, *, headers, body, content_type) -> CallbackResult: ...
    async def query_status(self, order_id: str) -> PaymentStatus: ...
```

`charge_recurring` 只在 `supports_recurring=True` 时重写；基类默认返回
`recurring.unsupported`，业务层据此走 manual 路径，不会报错。

### 2.2 `supports_recurring` 怎么填

**按 ABA 实际开通的能力如实填**，不要乐观。填 `True` 却不能代扣，学习者会以为
自己开了自动续费而实际在等一封永远不会来的提醒；填 `False` 而其实能代扣，只是
少了一点便利。**两种错误的代价不对称**，所以拿不准就填 `False`。

---

## 3. 落地后必须过的检查

### 3.1 契约测试（已经写好了，自动生效）

`backend/tests/unit/test_payment_provider_contract.py` 对 **registry 里的每一个
provider** 跑一遍，所以 `aba` 一注册就被它管住：

- checkout 幂等（同一 `order_id` 调两次结果相同）
- checkout 失败返回 `ok=False` 而不是抛异常
- **验签失败时不得回吐 `order_id` / `money` / `status`**
- `verify_callback` 面对空 body、乱码、非法 JSON 都不抛
- 不能代扣的渠道以值的形式说出来，而不是靠调用失败
- 声明的币种必须是 `core/money.py` 认识的

### 3.2 `HASH_FIELD_ORDER` 单独核对

这是最容易错且最难发现的一项。建议做法：拿 ABA 文档里的示例请求与示例签名，
写一条单测，**用我们的实现复现出他们文档里那个签名字符串**。对不上就是顺序错了。
不要用「沙箱能跑通」代替这条——沙箱通了只说明那一种字段组合是对的。

### 3.3 金额

`amount_minor` 是最小货币单位的整数。ABA 的 API 若以「元」为单位传（如 `1.99`），
**换算必须在 adapter 内部完成且不得引入浮点**：`Money(amount_minor=199, ...)`
进来，拼报文时按 `currency_minor_units` 做整数换算。
KHR 没有小数位——`amount_minor=8000` 就是 ៛8000，不是 80 元。

### 3.4 回调金额

`CallbackResult.money` 请如实填写。上层会拿它与订单金额**三项全等**核对
（`amount_minor` / `currency` / `currency_minor_units`，见 D-067），
对不上就拒绝开通。填不出来就留 `None`——上层会照常放行，那是为
「回调本来就不带金额」的渠道准备的。

---

## 4. 上线前

- `PAYMENT_PROVIDERS` 里保留 `fake` 只在 dev/CI；`ENV=prod` 时 registry 会
  拒绝构建任何 fake（D-010）
- 跑一笔真实小额支付，确认回调验签通过并自动开通订阅（M2 验收清单）
- 确认 `docs/CONFIG_REFERENCE.md` 第 9 节的 `PAYWAY_BASE_URL` 指向生产而非沙箱
