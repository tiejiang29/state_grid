"""App 通道取数 → 翻译成网页响应的形状 → 灌进 `data_client` 的推送缓存。

为什么不直接改 `refresh_data`：那 800 行是 bilezhou 原版、也是整条认证链上的承重结构
（余额正负号、按月回补、12 小时闸门都在里面）。它本来就有"命中缓存即返回"的入口
（`__fetch` 开头的 `push_cache` 判定），所以只要把 App 的返回包装成它认识的响应，
一行解析逻辑都不用动，也不引入第二套取数语义。

映射都注明了来源键，改服务端字段时按这里对。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

from .app_api import AppAccount, AppChannel, CHINA
from .utils.logger import LOGGER

DAILY_LAG_DAYS = 1          # 网页主请求问的是"昨天"为止的近 40 天，不是今天
DAILY_DAYS = 40             # 与 __get_door_daily_bill 的 timedelta(days=40) 对齐
MONTH_BACKFILL_PER_FILL = 12  # 按月回补一次最多补几个月，别在一轮里把 APP 打爆
_last_fill = 0.0
_DAILY_VALUE_KEYS = ("dayElePq", "thisPPq", "thisVPq", "thisNPq", "thisTPq")


def _daily_response(rows: list[dict[str, Any]], end_date: date) -> dict[str, Any]:
    """`c11/f01`(App) → `c24/f01`(网页) 的 `data.sevenEleList`，行按日期倒序。

    不能把没有出数的行丢掉：`_push_covers` 要求载荷的覆盖止**正好等于**请求止，
    而解析器本来就会跳过开头那几行没有数值的行（`refresh_data` 里数 `Y` 的那个循环），
    所以留着才对得上、也算得对。App 有时只返回到最后一个出数的日子，那就把它和请求止
    之间的空缺按同样格式补成 "-" 行——补的是"这几天还没公布"，不是编造电量。
    超出请求止的行要丢掉：多一天就把覆盖止顶到请求止之外，反而一份都吃不到。
    """
    keep = []
    for r in rows:
        day = str(r.get("day") or "")
        if not day:
            continue
        d = datetime.strptime(day.replace("-", ""), "%Y%m%d").date()
        if d <= end_date:
            keep.append((d, r))
    keep.sort(key=lambda x: x[0], reverse=True)
    if not keep:
        return {"code": "1", "data": {"sevenEleList": []}}
    fmt = "%Y-%m-%d" if "-" in str(keep[0][1]["day"]) else "%Y%m%d"
    pad = []
    for i in range((end_date - keep[0][0]).days, 0, -1):
        d = end_date - timedelta(days=i - 1)
        pad.append({"day": d.strftime(fmt), **{k: "-" for k in _DAILY_VALUE_KEYS}})
    return {"code": "1", "data": {"sevenEleList": pad + [r for _, r in keep]}}


def _month_window(ym: str) -> tuple[int, date, date]:
    """'202609' → (2026, 2026-09-01, 2026-09-30)，与 get_month_date_range 同口径。"""
    y, m = int(ym[:4]), int(ym[4:6])
    start = date(y, m, 1)
    end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
    return y, start, end


def _account_rows(client, cons: str) -> list[dict[str, Any]]:
    """网页那个月行集合：键是 `month_bill_list`（跨年合并的那一份），不是 `year_bill_list`
    ——后者是 `refresh_data` 结尾按当年筛出来的子集，拿它当依据会漏掉去年的月份。"""
    acct = (getattr(client, "doorAccountDict", None) or {}).get(cons) or {}
    return [r for r in (acct.get("month_bill_list") or []) if isinstance(r, dict)]


def _monthly_response(app_data: dict[str, Any], year: int) -> dict[str, Any]:
    """`c51/f04`(App) → `c01/f02`(网页) 的 `dataInfo` + `mothEleList`。

    口径差异要记着：App 的 `yearPq` 只算**已出账月份**（实测 569+840+212=1621），
    网页的 `totalEleNum` 含当月未出账（1679.76=1621+58.76）。这里照实搬，不做换算——
    当月累计本来就由日电量那条路径算，不该由年度合计顶替。
    App 的月度行没有分时字段，所以 `mothEleList` 只给总量与电费；年度峰谷平尖由
    `refresh_data` 从日电量累计（同网页路径）。
    """
    rows = []
    for it in app_data.get("list") or []:
        if not isinstance(it, dict) or not it.get("ym"):
            continue                                  # month 键缺失会让解析器 KeyError
        rows.append({"month": str(it["ym"]),
                     "monthEleNum": it.get("monthPq"),
                     "monthEleCost": it.get("monthAmt")})
    rows.sort(key=lambda r: r["month"], reverse=True)
    return {"code": "1", "data": {
        "dataInfo": {"totalEleNum": app_data.get("yearPq"),
                     "totalEleCost": app_data.get("yearAmt"), "year": year},
        "mothEleList": rows}}


def _balance_response(app_data: dict[str, Any]) -> dict[str, Any]:
    """`c16/f01`(App) → `c05/f01`(网页) 的 `data.list[0]`，同名字段原样透传。

    余额就是 `sumMoney`：实测网页行 `sumMoney=61.0 → balance=61.0`、第二块表
    `sumMoney=-14.63 → balance=-14.63`（它的 `prepayBal` 反而是 0）。
    曾经按"prepayBal 才是余额"写过一次 `accountBalance` 覆盖，那是拿昨天的数值做的
    巧合比对，会把余额算成 0，已撤。
    唯一不能省的是 `consType`：解析器直接下标读它，缺了会抛异常把整轮刷新掐掉。
    """
    rows = [r for r in (app_data.get("list") or []) if isinstance(r, dict)]
    for r in rows:
        r.setdefault("consType", r.get("sceneType") or "01")
    return {"code": "1", "data": {"list": rows}}


def _meter_list_response(raw_accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """登录响应里的 `userInfo.powerUserList` → 网页 `c9/f02` 的 `data.bizrt.powerUserList`。

    解析器要求每行有 `consNo_dst`（缺了 KeyError 并会连锁炸掉后面几步），实测 App 的行
    带 `consNo_dst / consNo / orgNo / proNo / constType / elecTypeCode`，正好是它要的。
    """
    return {"code": "1", "data": {"bizrt": {"powerUserList": raw_accounts}}}


async def async_fill_cache(hass, client) -> int:
    """登录 App、取日电量/月度/余额（含按年、按月的第二份窗口），灌缓存。

    返回入库份数；0 表示这轮不供应（保持原路径）。
    """
    global _last_fill
    # 供数节奏跟着 refresh_interval（选项里的刷新间隔，下限 12 小时）走：那正是
    # refresh_data 消费这批缓存的闸门。灌得比它勤只是把同一份日/月度重复取一遍——
    # 国网一天最多给出新的一天，所以一天两次就够。
    interval_s = max(int(getattr(client, "refresh_interval", 12) or 12), 12) * 3600
    if time.time() - _last_fill < interval_s:
        return 0
    _last_fill = time.time()
    account = str(getattr(client, "account", "") or "")
    digest = str(getattr(client, "password", "") or "")
    if not account or len(digest) != 32:
        return 0
    ch = AppChannel(hass, account, digest.lower())
    if not await ch.async_login():
        return 0
    today = datetime.now(CHINA).date()
    # 窗口要和 __get_door_daily_bill 那条主请求一模一样（D=今天-1，F=D-40），
    # 差一天 `_push_covers` 的覆盖止就对不上请求止，整批缓存就白灌。
    end = today - timedelta(days=DAILY_LAG_DAYS)
    start = end - timedelta(days=DAILY_DAYS)
    items: list[dict[str, Any]] = [{
        "shape": "meter_list", "consNo": None,
        "response": _meter_list_response(ch.raw_accounts)}]
    main_ok = 0
    for a in ch.accounts:
        daily = await ch.async_daily(a, start, end)
        ddaily = (daily or {}).get("data")
        resp = _daily_response((ddaily or {}).get("sevenEleList") or [], end)
        rows = resp["data"]["sevenEleList"]
        if rows:
            main_ok += 1
            LOGGER.debug("App 日电量 %s：%d 行，末行 %s=%s",
                           a.cons_no_src, len(rows), rows[0]["day"], rows[0].get("dayElePq"))
            items.append({"shape": "daily_ele", "consNo": a.cons_no_src,
                          "period": str(end.year), "response": resp})
        else:
            # 少一块表就是一整块表没数据，必须留得下原因：上游返回的码/消息是唯一的线索
            LOGGER.warning("App 通道没拿到 %s 的日电量：code=%s data=%s",
                           a.cons_no_src, (daily or {}).get("code"),
                           str(daily)[:160])
        monthly = await ch.async_monthly(a, today.year)
        dmonthly = (monthly or {}).get("data")
        if isinstance(dmonthly, dict) and dmonthly.get("list"):
            items.append({"shape": "monthly_ele", "consNo": a.cons_no_src,
                          "period": str(today.year),
                          "response": _monthly_response(dmonthly, today.year)})
        # 网页问去年那份年度账单只在合并出来的月行不足 12 个时发生（refresh_data 里那句
        # len(A['month_bill_list'])<12），凑满了就不再问，这里也跟着停，免得白打一次
        rows_all = _account_rows(client, a.cons_no_src)
        if len(rows_all) < 12:
            last = await ch.async_monthly(a, today.year - 1)
            dlast = (last or {}).get("data")
            if isinstance(dlast, dict) and dlast.get("list"):
                items.append({"shape": "monthly_ele", "consNo": a.cons_no_src,
                              "period": str(today.year - 1),
                              "response": _monthly_response(dlast, today.year - 1)})
        # 按月回补：网页对每个还没有 daily_ele 的月行都会用同一个 c24/f01 再问一次整月窗口。
        # 补成功的月行会写上 daily_ele，之后不再问，所以这笔开销是有界的、只在刚装好的头一两天。
        for ym in sorted({r["month"] for r in rows_all
                          if r.get("month") and not r.get("daily_ele")},
                         reverse=True)[:MONTH_BACKFILL_PER_FILL]:
            y, ms, me = _month_window(ym)
            mdaily = await ch.async_daily(a, ms, me)
            mresp = _daily_response(((mdaily or {}).get("data") or {}).get("sevenEleList") or [], me)
            if mresp["data"]["sevenEleList"]:
                items.append({"shape": "daily_ele", "consNo": a.cons_no_src,
                              "period": str(y), "response": mresp})
        balance = await ch.async_balance(a)
        dbalance = (balance or {}).get("data")
        if isinstance(dbalance, dict) and dbalance.get("list"):
            items.append({"shape": "balance", "consNo": a.cons_no_src,
                          "response": _balance_response(dbalance)})
    daily_items = [i for i in items if i["shape"] == "daily_ele"]
    if not main_ok:
        # 只剩 meter_list 也没法算电量，且 ingest_push 是整包替换，
        # 这时候灌进去反而会把网页兜底那一份好数据顶掉，所以直接不供应。
        LOGGER.warning("App 通道没取到日电量，本轮不灌缓存（保留网页兜底推送那一份）")
        return 0
    count = await client.ingest_push({
        "source": "App 通道推送",
        "items": items,
        "pushed_at": datetime.now(CHINA).isoformat(timespec="seconds")})
    LOGGER.debug("App 通道本轮取到 %d 份载荷（日电量 %d 份，主窗口 %d 块表）",
                   len(items), len(daily_items), main_ok)
    return count
