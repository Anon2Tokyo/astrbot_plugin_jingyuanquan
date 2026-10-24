"""Money and usage calculations, independent of AstrBot."""

import json
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation

TOKENS_PER_MILLION = 1_000_000
EXCHANGE_RATE = 1000  # RMB -> whole 鲸元券; snapshot on every transaction


def decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("金额格式错误") from None
    if not result.is_finite() or result < 0 or result > Decimal(100000000):
        raise ValueError("金额必须是有限的非负数，且不超过一亿")
    return result


def units(value):
    amount = decimal(value)
    if amount != amount.to_integral_value():
        raise ValueError("鲸元券必须是整数，最小单位为 1 鲸元券")
    return int(amount)


def display(value):
    if Decimal(value) != Decimal(value).to_integral_value():
        raise ValueError("鲸元券必须是整数")
    return str(int(value))


@dataclass(frozen=True)
class Usage:
    hit: int
    miss: int
    output: int

    @classmethod
    def parse(cls, data):
        names = (
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
            "completion_tokens",
            "prompt_tokens",
            "total_tokens",
        )
        if not isinstance(data, dict) or any(
            type(data.get(k)) is not int or data[k] < 0 for k in names
        ):
            raise ValueError("缺少可信用量")
        hit, miss, output, prompt, total = (data[k] for k in names)
        if hit + miss != prompt or prompt + output != total:
            raise ValueError("用量字段不一致")
        return cls(hit, miss, output)


@dataclass(frozen=True)
class Price:
    model: str
    hit: Decimal
    miss: Decimal
    output: Decimal
    multiplier: Decimal
    exchange_rate: int = EXCHANGE_RATE

    @classmethod
    def from_config(cls, config):
        price = cls(
            str(config["model"]),
            *(
                decimal(config[k])
                for k in (
                    "cache_hit_price",
                    "cache_miss_price",
                    "output_price",
                    "multiplier",
                )
            ),
        )
        if (
            not price.model
            or min(price.hit, price.miss, price.output, price.multiplier) <= 0
        ):
            raise ValueError("请先配置模型、正数采购单价及收费倍率")
        return price

    def cost(self, usage):
        return (
            usage.hit * self.hit + usage.miss * self.miss + usage.output * self.output
        ) / TOKENS_PER_MILLION

    def charge(self, cost):
        return int(
            (decimal(cost) * self.exchange_rate * self.multiplier).to_integral_value(
                rounding=ROUND_CEILING
            )
        )

    def snapshot(self):
        return json.dumps({k: str(v) for k, v in asdict(self).items()})

    @classmethod
    def restore(cls, snapshot):
        data = json.loads(snapshot)
        return cls(
            data["model"],
            *(Decimal(data[k]) for k in ("hit", "miss", "output", "multiplier")),
            exchange_rate=int(data["exchange_rate"]),
        )
