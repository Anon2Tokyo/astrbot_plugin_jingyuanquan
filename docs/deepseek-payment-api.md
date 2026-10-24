> 2026-10-24 实现更新：v0.3.0 已参考本地 dsAutoPay 接入扫码充值、有限轮询和原子加券。本文保留早期接口观察与方案，下面的“待实现”描述属于历史记录；当前行为、配置和验证边界以 README 为准。

# DeepSeek 网页充值接口接入记录

核验日期：2026-10-23。用途：为鲸元券插件后续自动充值提供接口依据。

本文记录用户浏览器提供的请求与响应，以及本次实际只读查单结果。这是 DeepSeek 网页使用的内部接口，未确认有公开稳定性承诺。不是已接入的插件功能，也不代表已经验证完整付款流程。

## 1. 认证与各种标识

这两条已观察请求中都没有显式的 DeepSeek `user_id` 参数。账户身份由请求携带的登录认证信息关联；目前不能从这些样本确定账户内部用户 ID 的字段名或值。

| 字段 | 含义与接入用途 |
| --- | --- |
| `Authorization: Bearer <WEB_SESSION_TOKEN>` | 网页登录认证凭证，用于访问登录账户的接口。是敏感令牌，不是可公开保存的用户编号。不要与调用模型的 API Key 混用；未验证 API Key 能用于网页充值接口。 |
| `x-device-id` | 网页请求携带的设备标识，不是 QQ 号，也不能当作 DeepSeek 用户 ID。是否必填未确认。 |
| `shumei_device_id` | 创建支付订单请求中的设备识别相关值；生成方式、有效期和校验规则未确认。不能当作用户 ID。 |
| `order_info.request_id` | 创建请求的客户端标识，样本形态为 UUID。是否具有服务端幂等语义尚未验证，不能依赖它保证重复 POST 不创建新订单。 |
| `payment_order_id` | DeepSeek 返回的支付订单号，用于把二维码、支付状态和本地充值订单关联起来。不是用户 ID。 |
| 插件保存的 `qq` | 发起充值的 QQ 用户，由 AstrBot 消息事件识别并在本地绑定。DeepSeek 返回中没有这一字段。 |

推荐关联关系：

```text
网页登录凭证 → 你的 DeepSeek 充值账户
QQ 用户 → 本地充值订单 → DeepSeek payment_order_id
```

用户扫描二维码后，是向创建该订单的 DeepSeek 账户充值。插件依据本地绑定关系给发起申请的 QQ 增加鲸元券；不能通过这些响应识别实际扫码付款人的 QQ。二维码即使由别人代付，到账对象也应保持为订单绑定的 QQ。

不要将会话令牌、Cookie 或设备识别值写入代码、本文、Git、日志、群消息或订单流水。服务端认证配置应与普通订单数据分开保存。

## 2. 创建支付订单

### 请求

```http
POST https://platform.deepseek.com/api/v1/payments
Authorization: Bearer <WEB_SESSION_TOKEN>
Content-Type: application/json
```

用户浏览器样本还携带 Cookie、Origin、Referer、User-Agent、`x-device-id` 和以下 `x-client-*` 请求头。未独立重放创建请求，尚未确定哪些是必填项。

```http
Origin: https://platform.deepseek.com
Referer: https://platform.deepseek.com/top_up
x-client-bundle-id: com.deepseek.chat
x-client-locale: zh_CN
x-client-platform: web
x-client-timezone-offset: 28800
x-client-version: 1.0.0
x-device-id: <DEVICE_ID>
```

请求正文（敏感值和请求标识已替换）：

```json
{
  "order_info": {
    "payment_method_type": "CMB_UNIONPAY",
    "fallback_method_type": "ALIPAY",
    "amount": "20",
    "currency": "CNY",
    "request_id": "<CLIENT_REQUEST_UUID>"
  },
  "shumei_device_id": "<DEVICE_VALUE>"
}
```

| 字段 | 观察结果 |
| --- | --- |
| `amount` | 字符串金额，样本为 20 元。允许范围、小数精度和最小金额未验证。 |
| `currency` | 样本为 `CNY`。 |
| `payment_method_type` | 样本主渠道为 `CMB_UNIONPAY`。未验证直接改成 `ALIPAY` 是否支持。 |
| `fallback_method_type` | 样本备用渠道为 `ALIPAY`。 |
| `request_id` | 客户端请求标识；本地应保存以便排查。 |
| `shumei_device_id` | 浏览器提供的设备相关数据；不能随意填一个固定值并假定长期有效。 |

### 成功响应样本

```json
{
  "code": 0,
  "msg": "",
  "data": {
    "biz_code": 0,
    "biz_msg": "",
    "biz_data": {
      "code": 0,
      "payment_order_id": "<PAYMENT_ORDER_ID>",
      "fallback": false,
      "amount": "20",
      "url": "https://qr.95516.com/<PAYMENT_PATH>"
    }
  }
}
```

解析路径：

```python
payment = payload["data"]["biz_data"]
order_id = payment["payment_order_id"]
payment_url = payment["url"]
amount = payment["amount"]
```

应同时检查 HTTP 状态、外层 `code`、`data.biz_code`、创建响应内层 `code`，以及订单号、金额和链接是否有效。样本中成功码均为 0，但没有完整错误码表，其他值应停止发码并记录脱敏错误。

`url` 是二维码要编码的支付链接，不是图片地址。可在本地用二维码库直接生成图片，无需打开支付页面抓图。保存图片时使用订单唯一文件名，避免并发覆盖。只编码服务端返回并验证过的 HTTPS 链接；当前仅观察到 `qr.95516.com`。

本样本 `fallback=false`，但随后查单记录的 `payment_channel` 为 `ALIPAY`。因此不要仅凭主渠道、备用渠道或 fallback 字段推断扫码 App 限制；渠道字段之间的完整映射、是否只能支付宝支付仍待验证。

响应未返回可确认的二维码过期时间。不能把自定义本地订单有效期当成上游实际失效时间。

## 3. 查询充值记录

### 请求

```http
GET https://platform.deepseek.com/auth-api/v0/users/get_all_invoice
Authorization: Bearer <WEB_SESSION_TOKEN>
Referer: https://platform.deepseek.com/transactions
x-client-platform: web
x-client-version: 1.0.0
```

本次使用以上请求头、不带 Cookie 的查单成功，HTTP 200，外层 `code=0`、`data.biz_code=0`。这只说明本次查单成功，不证明其他请求头永远不需要，也不证明创建接口可采用同样的最小请求头。

观察到的请求无查询参数、无请求正文。虽然名为 `get_all_invoice`，实际响应包含充值订单 `payment_orders` 和赠送记录 `bonus_orders`，并非仅发票数据。

### 响应结构

下面是经脱敏并简化后的结构示例，省略其他订单。状态、金额、币种、支付时间与渠道保留本次目标订单的观察值；订单号和时间字符串已替换。

```json
{
  "code": 0,
  "msg": "",
  "data": {
    "biz_code": 0,
    "biz_msg": "",
    "biz_data": {
      "invoices": {
        "payment_orders": [
          {
            "payment_order_id": "<PAYMENT_ORDER_ID>",
            "amount": "20",
            "currency": "CNY",
            "payment_order_status": "CREATED",
            "paid_at": null,
            "inserted_at": "<TIMESTAMP>",
            "updated_at": "<TIMESTAMP>",
            "payment_channel": "ALIPAY"
          }
        ],
        "bonus_orders": []
      }
    }
  }
}
```

本次真实响应含 33 条 `payment_orders` 和 1 条 `bonus_orders`，其中找到了创建二维码时返回的同一 `payment_order_id`。目标订单当时为 `CREATED`，`paid_at=null`；没有验证它随后支付成功的全过程。

解析路径：

```python
orders = payload["data"]["biz_data"]["invoices"]["payment_orders"]
target = next(
    (order for order in orders if order["payment_order_id"] == stored_order_id),
    None,
)
```

### 状态处理

本次返回实际观察到了 `CREATED`、`FAILED`、`SUCCESS` 三种状态。没有官方状态枚举或完整状态转移说明，以下为建议的插件处理规则：

| 状态 / 结果 | 本地处理 |
| --- | --- |
| `CREATED` | 未确认支付成功，不入账；用户已观察到页面显示“已取消”而接口仍为此值，不能将它直接解释为二维码仍可支付，也不能据此判断已取消。 |
| `SUCCESS` | 候选成功记录；精确匹配订单、核对人民币金额与币种后，幂等入账。正式接入前验证成功样本的 `paid_at`，字段缺失或矛盾时转人工核对。 |
| `FAILED` | 不入账，保留记录；终态语义未核实，不能据此删除本地订单。 |
| 未知状态、缺少订单、字段异常 | 不入账、不认定未付款，重查或人工核对。 |
| HTTP 错误、业务错误、登录过期 | 查单失败，不能解释成订单付款失败；保留订单并提示管理员处理。 |

不要依据总余额增加、金额相同、付款截图、赠送记录或支付页面跳转来入账。`bonus_orders` 不属于 QQ 用户付款，不发放鲸元券。

### 2026-10-23 补充观察与通知约定

用户反馈页面展示“已取消”，但对应接口状态仍为 `CREATED`。当前 JSON 没有页面展示状态字段，不能判断这是前端本地状态、更新延迟还是其他映射逻辑。

用户随后提供的响应中，先前生成二维码的 20 元订单已从此前查询的 `CREATED` 变为 `FAILED`，`paid_at` 仍为空。另有一笔 1 元订单为 `SUCCESS`，`paid_at` 为非空带时区时间字符串。这些观察并不证明所有取消订单都会立即或最终变为 `FAILED`。

实现时分开保存上游状态与本地二维码展示有效期；只对精确关联且核验成功的 `SUCCESS` 订单入账。用户说明二维码有效期为 15 分钟，创建响应本身未返回该期限。超过本地有效期不等于确认未支付，保留低频补查或人工核对路径，不把 `CREATED` 自动改写为上游已取消。

群消息约定：创建后只发送一次“@申请人 + 二维码 + 请在 15 分钟内完成支付”；不刷新倒计时、不催付、不发送待支付或过期取消通知。后台静默查单，入账成功后才再次 @申请人通知到账。

## 4. 插件接入方案（待实现）

本地建议新增 `topup_orders` 表，字段至少包括：

| 字段 | 作用 |
| --- | --- |
| `id` | 本地充值单号 |
| `qq` | 从 AstrBot 消息中取得的充值申请人 QQ |
| `source_message_key` | 唯一消息来源键，防止重复消息创建订单 |
| `account_ref` | 本地 DeepSeek 收款账户配置标识，不是令牌或已知上游 user_id |
| `client_request_id` | 发送给创建接口的 request_id |
| `payment_order_id` | 上游唯一订单号，与账户引用组合建立唯一约束 |
| `amount_cny` / `currency` | 预期付款金额与币种，使用 Decimal 或整数分 |
| `exchange_rate` / `credit_amount` | 订单创建时锁定的兑换比例和整数鲸元券金额 |
| `state` | 本地状态：creating / pending / paid_uncredited / credited / review 等，区别于上游状态 |
| `created_at` / `last_checked_at` / `credited_at` | 本地审计与补单时间 |
| `upstream_status` / `paid_at` | 最近查单结果 |

```text
/充值 20
→ 持久化本地申请，绑定 QQ，锁定 1 元 = 1000 鲸元券
→ POST 创建订单
→ 保存 payment_order_id，再发送二维码
→ 后台 GET 查询充值记录，按上游订单号匹配
→ 验证 SUCCESS、20 元、CNY 和本地账户关联
→ 单一数据库事务：余额 +20000、写流水、订单标记 credited
→ 回复用户充值成功
```

注意：

- 轮询由本地服务主动发出，不依赖公网回调地址。查单周期与退避策略需在确认接口限流后确定，不应按每个订单高频重复拉取全账户记录。
- 重启后继续查询未完成订单。用户通知发送失败不能回滚已完成账务，重新通知也不能重新加余额。
- 创建接口若超时，可能已在上游生成订单。未验证 request_id 的幂等性与检索能力前，不自动重发 POST；进入待核对，避免产生多张可支付二维码。
- 本地超时只影响展示和轮询频率，不证明支付失败；需保留迟到支付补单路径。
- 不确定该查询是否存在隐式分页、条数或时间限制。“列表里没找到”不能等同未支付；正式接入需验证长期补单能力。
- 不要自动把不属于本地已绑定申请的历史成功订单发给某个 QQ。
- 用户的钱进入 DeepSeek 账户，应单列为“供应商代充值”，不能统计为管理员支付宝现金收款。当前人工充值账本不能不加区分地复用其收入分类。
- 如更换 DeepSeek 登录账户，必须避免旧订单在新账户下被误处理；没有已验证的账户身份查询接口之前，账户配置变更应暂停相关待处理订单并核对。

## 5. 仍需验证

1. 创建接口最小认证条件、设备字段要求，以及支持的支付渠道。
2. 一笔受控测试订单从 CREATED 到 SUCCESS 的完整流程、成功时 paid_at 的格式与一致性。
3. 订单超时、失效、失败、退款的完整语义与晚到支付行为。
4. 创建 request_id 是否具备幂等性，创建响应丢失后能否恢复订单关联。
5. 查单分页、记录保留窗口、限流和登录凭证续期机制。
6. 返回金额究竟始终为实际支付金额还是存在手续费等差异；不可把未知差额默认为到账本金。

未完成这些验证前，已知样本只能证明“可以创建二维码订单”和“可以查询到同一订单”，不能宣称自动充值已经可靠上线。
