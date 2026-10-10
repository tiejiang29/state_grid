"""
国家电网数据客户端：取数与解析。

两个供数来源，顺序是"先缓存、后网络"：
  * App 通道（`app_supply` 按刷新间隔把 App 接口面的返回翻译成网页形状灌进
    `push_cache`）——命中即用，一天两轮，正常情况都走这条；
  * 网页通道（`web_api.WebChannel`）——只在某个格子缓存没货时才发真请求，
    所以它补的是 App 给不了的那部分（例如 4006 短信没登进来、某块表缺月行）。
    关掉 `web_channel` 选项或它挂不上时，行为和以前一样：返回 no_cache 按缺数处理。

网页这条能纯 HTTP 走通的前提写在 `web_api.py` 顶部：登录体只发 `params`（旧版多带的
loginKey/code/Channels 会让服务端硬拒），并且首发拿到 RK1003 之后再补一次
`complexSliderRet` —— 2026-10 验证：服务端不回头向腾讯核对，所以不需要浏览器。

取数与解析逻辑仍沿用 bilezhou/state_grid 原版（MIT），下面那套 refresh_data 一行语义没改。
"""

import json
import time
import datetime

from . import web_api
from .const import VERSION
from .utils.logger import LOGGER
from .utils.store import async_save_to_store

# ─── bilezhou 原版混淆变量映射 ───
_Au='daily_ele'
_At='month_meter_num'
_As='constType'
_Ar='queryYear'
_Aq='provinceCode'
_Ap='redirect_url'
_Ao='refresh_interval'
_An='dataVersion'
_Am='doorAccountDict'
_Al='refreshToken'
_Ak='accessToken'
_Aj='brightness'
_Ai='BCP_00026'
_Ah='serviceCode_smt'
_Ag='WEBA10070900'
_Af='serviceType'
_Ae='jM_custType'
_Ad='jM_busiTypeCode'
_Ac='doorNumberManeger'
_Ab='proCode'
_Aa='loginAccount'
_AZ='userAccountId'
_AY='elecTypeCode'
_AX='quInfo'
_AW='blockY'
_AV='blockSrc'
_AU='canvasSrc'
_AT='powerUserList'
_AS='userInfo'
_AR='publicKey'
_AQ='WEBA10070800'
_AP='timeDay'
_AO='WEBA10070700'
_AN='state_grid'
_AM='channelNo'
_AL='month_ele'
_AK='consType'
_AJ='provinceId'
_AI='userName'
_AH='acctId'
_AG='bizrt'
_AF='password'
_AE='0101046'
_AD='month_ele_num'
_AC='consNo'
_AB='list'
_AA='token'
_A9='keyCode'
_A8='querytypeCode'
_A7='01010049'
_A6='month_t_ele_num'
_A5='month_n_ele_num'
_A4='month_v_ele_num'
_A3='month_p_ele_num'
_A2='thisTPq'
_A1='thisNPq'
_A0='thisVPq'
_z='thisPPq'
_y='errmsg'
_x='BCP_000026'
_w='app'
_v='WEBALIPAY_01'
_u='order'
_t='dayElePq'
_s='timestamp'
_r='authFlag'
_q='09'
_p='0101183'
_o='tenant'
_n='devciceId'
_m='devciceIp'
_l='member'
_k='stepelect'
_j='account'
_i='daily_bill_list'
_h='orgNo'
_g='consNo_dst'
_f='srvrt'
_e='0101154'
_d='getday'
_c='clearCache'
_b='promotCode'
_a='01'
_Z='month'
_Y='account_balance'
_X='proNo'
_W='userId'
_V=True
_U='SGAPP'
_T='target'
_S='month_bill_list'
_R='promotType'
_Q='uscInfo'
_P='subBusiTypeCode'
_O='serialNo'
_N=False
_M='0902'
_L='srvCode'
_K='serCat'
_J='busiTypeCode'
_I='code'
_H='channelCode'
_G='errcode'
_F='1'
_E='source'
_D=None
_C='serviceCode'
_B='funcCode'
_A='data'

# ─── 数据接口路径 ───
# 它们同时是推送缓存的键的一半：sidecar 推的形状与 App 通道翻译出的形状都按这些路径对齐。
# 登录/验证码/换 token 那几条路径不再留在这里，appKey/appSecret 也不再需要——本模块不发网络请求。
get_door_number_api='/osg-open-uc0001/member/c9/f02'
get_door_balance_api='/osg-open-bc0001/member/c05/f01'
get_door_bill_api='/osg-open-bc0001/member/c01/f02'
get_door_ladder_api='/osg-open-bc0001/member/c04/f03'
get_door_daily_bill_api='/osg-web0004/member/c24/f01'

# ─── sidecar 推送通道：载荷形状 → 本集成的 API 路径 ───
# 形状名由 sidecar 的 payload_shape() 按"载荷里有什么"判定，不用 URL 做标识：
# 一个网关路径会带多种无关载荷，顺序也不可信。
PUSH_SHAPE_API={
        'daily_ele':get_door_daily_bill_api,
        'monthly_ele':get_door_bill_api,
        'ladder':get_door_ladder_api,
        'balance':get_door_balance_api,
        'meter_list':get_door_number_api,
}
# 账户级载荷没有户号，缓存键的户号位固定为 None
PUSH_ACCOUNT_SHAPES=('meter_list',)
OK_CODES=('1','0000','000000')
CONS_NO_KEYS=('consNo','consNoSrc','elecCustNo','custNo')
# c04/f03 在 2026 升级后给不给抄表读数没人知道（解析出来的 month_meter_num 一直是 0）。
# 只在本进程里说一句它的真实键名，别为一次结构确认刷掉几十行日志。
_ladder_shape_logged = False


def _find_key(obj, names):
        """在待发请求里递归找第一个命中的键：各业务请求把同类信息放在不同深度
        （data.consNo、queryElecCustList[].consNoSrc、params3.data.consNo…），逐个写死容易漏。"""
        if isinstance(obj,dict):
                for k,v in obj.items():
                        if k in names and isinstance(v,(str,int)) and str(v).strip():
                                return str(v).strip()
                for v in obj.values():
                        r=_find_key(v,names)
                        if r:return r
        elif isinstance(obj,list):
                for v in obj:
                        r=_find_key(v,names)
                        if r:return r
        return None


def _cons_no_in(req):
        return _find_key(req,CONS_NO_KEYS)


def _period_in(req):
        """请求限定的是哪一期：月账单按 queryYear 取，阶梯按 queryDate 取，日电量不带期间。"""
        return _find_key(req,('queryDate','queryYear'))


def _push_hit_key(api, req):
        """推送缓存的键：账户级请求不带户号。期间和日期区间是命中条件，不放键里。"""
        if api == get_door_number_api:
                return (api,None)
        return (api,_cons_no_in(req))


def _d8(s):
        return str(s).replace('-','')


def _month_first(d8):
        return d8[:6]+'01'


def _push_covers(api, req, entry):
        """这条载荷够不够回答这个请求。缓存值 = (响应, 期间, 覆盖起, 覆盖止)。

        期间：月账单请求带 queryYear、阶梯带 queryDate。两边有一边说了期间就必须对上，
        宁可不命中退回真实请求，也不能拿 2026 年的数去答 2025 年。

        区间：页面最宽只给近 30 天（实测 31 行，如 2026-08-30..09-29），而 refresh_data 的主请求
        问的是近 40 天。逐行读过解析器（refresh_data 里那段按月合计的循环）后，它从这份数据
        真正用到的只有两样：最后一天的分时电量、以及"与最后一天同月"的那些行逐行相加（遇到跨月
        就 break），加上 recent_30_daily_ele_list 取前 30 行。所以判定按实际用途来：
        末日必须正好是请求的末日，起点必须不晚于「请求起点」与「末日所在月1号」里较晚的那个。
        按月回补那种请求（末日是别的月份）天然对不上，会退回真实请求。
        """
        P,S,E=entry[1],entry[2],entry[3]
        RP=_period_in(req)
        if (P or RP) and P!=RP:return False
        if api==get_door_daily_bill_api:
                A0=_find_key(req,('startTime',));B0=_find_key(req,('endTime',))
                if A0 and B0:
                        if not (S and E):return False
                        if E!=_d8(B0):return False
                        if S>max(_d8(A0),_month_first(E)):return False
        return True


def _normalize_pushed_daily(resp):
        """页面版 c24/f01 的 day 是 '2026-09-28'，接口版是 '20260928'，而 refresh_data 里
        strptime 按后者写死。在入口统一掉，别让两种格式流进后面的解析。"""
        for row in ((resp.get(_A) or {}).get('sevenEleList')) or []:
                d=row.get('day')
                if isinstance(d,str) and '-' in d:row['day']=d.replace('-','')

# ─── bilezhou 原版业务配置（不变） ───
configuration={_Q:{_l:_M,_m:'',_n:'',_o:_AN},_E:_U,_T:'32101',_H:_M,_AM:_M,'toPublish':_a,'siteId':'2012000000033700',_L:'',_O:'',_B:'',_C:{_u:_e,'uploadPic':'0101296','pauseSCode':'0101250','pauseTCode':'0101251','listconsumers':'0101093','messageList':'0101343','submit':'0101003','sbcMsg':'0101210','powercut':'0104514','BkAuth01':'f15','BkAuth02':'f18','BkAuth03':'f02','BkAuth04':'f17','BkAuth05':'f05','BkAuth06':'f16','BkAuth07':'f01','BkAuth08':'f03'},'electricityArchives':{'servicecode':'0104505',_E:_M},'subscriptionList':{_L:'APP_SGPMS_05_030',_O:'22',_H:_M,_B:'22',_T:'-1'},'userInformation':{_C:'01008183',_E:_U},'userInform':{_C:_p,_E:_U},'elesum':{_H:_M,_B:_v,_b:_F,_R:_F,_C:'0101143',_E:_w},_j:{_H:_M,_B:'WEBA1007200'},_Ac:{_E:_M,_T:'-1',_H:_q,_AM:_q,_C:_A7,_B:'WEBA40050000',_Q:{_l:_M,_m:'',_n:'',_o:_AN}},'doorAuth':{_E:_U,_C:'f04'},'xinZ':{_K:'101',_Ad:'101','fJ_busiTypeCode':'102',_Ae:'03','fJ_custType':'02',_Af:_a,_P:'',_B:_AO,_u:_e,_E:_U,_A8:_F},'onedo':{_C:_AE,_E:_U,_B:_AO,'queryType':'03'},'xinHuTongDian':{_K:'110',_J:'211',_P:'21102',_B:'WEBA10071200',_H:_M,_E:_q,_C:_p},'company':{_K:'104',_B:_AO,_Af:'02',_A8:_F,_r:_F,_E:_U,_u:_e},'charge':{_H:_q,_B:'WEBA10071300',_AM:'0901',_K:'102',_Ae:_a,_Ad:'102'},'other':{_H:_q,_B:'WEBA10079700',_K:'129',_J:'999',_P:'21501',_C:_x,_L:'',_O:''},'vatchange':{'submit':'0101003',_J:'320',_P:'',_K:'115',_B:'WEBA10074000',_r:_F},'bill':{_c:_F,_B:_v,_R:_F,_C:_x},_k:{_H:_M,_B:_v,_R:_F,_c:_q,_C:_x,_E:_w},_d:{_H:_M,_c:'11',_B:_v,_b:_F,_R:_F,_C:_x,_E:_w},'mouthOut':{_H:_M,_c:'11',_B:_v,_b:_F,_R:_F,_C:_x,_E:_w},'meter':{_K:'114',_J:'304',_B:'WEBA10071000',_P:'',_C:_AE,_O:''},'complaint':{_J:'005','srvMode':_M,'anonymousFlag':'0','replyMode':_a,'retvisitFlag':_a},'report':{_J:'006'},'tradewinds':{_J:'019'},'somesay':{_J:'091'},'faultrepair':{_B:_Ag,_C:_p,_K:'111',_J:'001',_P:'21505'},'electronicInvoice':{_K:'105',_J:'0'},'rename':{_C:_AE,_B:'WEBA10076100',_J:'210',_K:'109',_r:_F,'gh_busiTypeCode':'211','gh_subusi':'21101',_O:'',_L:''},'pause':{_P:'',_C:_A7,_B:'WEBA10073600',_K:'107',_J:'203','jr_busi':'201',_O:'',_L:''},'capacityRecovery':{_C:_A7,_E:_U,_L:'',_O:'',_B:'WEBA10073700','busiTypeCode_stop':'204','busiTypeCode_less':'202',_J:'202',_P:'',_K:'108',_AP:'5',_r:_F},'electricityPriceChange':{_C:_p,_J:'215',_P:'21502',_K:'113',_r:_F,_AP:'15',_B:'WEBA10073900WEB',_L:'',_O:''},'electricityPriceStrategyChange':{_C:'01008183',_J:'215',_P:'21506',_K:'160',_B:'WEBV00000517WEB',_L:'',_O:''},'eemandValueAdjustment':{_C:_p,_L:'',_O:'',_K:'112',_B:'WEBA10073800',_J:'215',_P:'21504',_r:_F,_AP:'5','getMonthServiceCode':_AE},'businessProgress':{_C:_p,_L:_a,_B:'WEB01'},'increase':{_E:_U,_O:'',_L:'',_Ah:_A7,_C:_e,_u:_e,_B:_AQ,_A8:_F,_K:'106',_J:'111',_P:''},'fjincrea':{_K:'105',_J:'110',_P:'',_E:_U,_B:_AQ,_O:'',_L:'',_Ah:_A7,_C:_e,_u:_e,_A8:_F},'persIncrea':{_K:'105',_J:'109',_u:_e,_P:'',_E:_U,_B:_AQ,_A8:_F},'fgdChange':{_C:_p,_L:_a,_H:_q,_B:_Ag,_J:'215',_P:'21505',_K:'111',_r:_F},'createOrder':{_H:_M,_B:_v,_L:'BCP_000001','chargeMode':'02','conType':_a,'bizTypeId':'BT_ELEC'},'largePopulation':{_J:'383',_B:'WEBA10076800',_P:'',_L:'',_R:'',_b:'',_H:'0901',_K:'383',_C:'',_O:''},'biaoJiCode':{_C:'0104507',_E:'1704',_H:'1704'},'twoGuar':{_J:'402',_P:'40201',_B:'web_twoGuar'},'electTrend':{_C:_Ai,_H:_M},'emergency':{_C:_Ai,_B:'A10000000',_H:_M},'infoPublic':{_C:'2545454',_E:_w}}

# ─── bilezhou 原版工具函数（不变） ───
def json_dumps(data):return json.dumps(data,separators=(',',':'),ensure_ascii=_N)
def normal_round(num,ndigits=0):
        # int() 是向零截断的：负数走 int(x+0.5) 会把 -14.630000000000003 变成 -14.62。
        # 欠费户的余额就是负数，实测 sumMoney -14.63 显示成 -14.62，所以负半边单独处理，
        # 正数的行为一个字节都没改。
        A=ndigits;B=10**A;R=(num*B if A else num)
        return (int(R+.5) if R>=0 else -int(-R+.5))/(B if A else 1)
def catchFloat(data,key):
        if key in data:
                try:return normal_round(float(data[key]),2)
                except:return 0
        else:return 0
def catchInt(data,key):
        if key in data:
                try:return normal_round(float(data[key]),0)
                except:return 0
        else:return 0
def get_month_date_range(date_str):
        C=date_str;A=int(C[:4]);B=int(C[4:]);F=datetime.date(A,B,1)
        if B==12:D=1;E=A+1
        else:D=B+1;E=A
        G=datetime.date(E,D,1)-datetime.timedelta(days=1);return A,F,G
class StateGridDataClient:
        hass=_D;coordinator=_D;dataVersion=_D;powerUserList=_D;doorAccountDict={}
        timestamp=int(time.time()*1000);refresh_interval=12;is_debug=_N;account=_D;password=_D
        # userInfo / token 已经没有生产者了（网页登录删掉的），但下面几个请求载荷的构造里
        # 还直接下标读它们。载荷现在只当缓存键用（匹配看户号/期间/日期区间），所以留占位：
        # 不给就会在第一次取数时 AttributeError 把整轮炸掉——23:25 在 NAS 上实测到
        userInfo=_D;token=_D
        # 网页通道三态：None=还没试过；False=试过但不可用（别在每个空格子上都去登录一次）；
        # 其余是可用实例
        # 本轮真的供上了几格：refresh_data 结尾靠它决定要不要把 timestamp 前移
        # （网页优先之后，一轮可以完全不碰推送缓存，而推 timestamp 的只有 ingest_push）
        fetch_ok=0
        web=_D
        web_channel=_N
        # 网页优先（选项里可关）：真=每格先问网页、App 灌进来的载荷兜底；
        # 假=老顺序（缓存命中优先，网页只补没货的格子）。
        # 两侧字段与数值 10-05 逐项对过，换顺序不改口径，只改"今天这格是谁答的"。
        web_priority=_V
        # 网页登录的备用标识（登记邮箱）：主标识吃到 RK001 时由 web_api 顶上。
        # 住在条目选项里，不落 state_grid.config（那份只放取数要用的凭证）
        email_account=""

        # ── 推送缓存：App 通道与 sidecar 都往这里灌 {(api, 户号): [(响应, 期间, 覆盖起, 覆盖止), …]} ──
        push_cache = {}
        push_meta = _D
        push_pending = _N

        # ────────────────────────────────────────────
        # __init__: 从 store 恢复账号与已解析出来的户号数据
        # ────────────────────────────────────────────
        def __init__(A,hass,config=_D):
                B=config;A.hass=hass
                # 请求载荷里那几个 userId / loginAccount 位：网页版是从登录响应里拿的，
                # 现在不发网络请求了，就留一份空壳。它们不参与缓存匹配（键只看户号、
                # 期间和日期区间），别让"没有网页会话"把整轮取数炸掉
                A.userInfo={'userId':'','loginAccount':''}
                if B is not _D:
                        try:
                                A.powerUserList=B.get(_AT,_D);A.doorAccountDict=B.get(_Am,{});A.is_debug=B.get('is_debug',_N)
                                A.dataVersion=B.get(_An,_D);A.account=B.get(_j,_D);A.password=B.get(_AF,_D);A.refresh_interval=B.get(_Ao,12)
                                if A.refresh_interval<12:A.refresh_interval=12
                                # 加载 timestamp，使重启后 12 小时间隔判断仍然正确
                                # 若旧版存储中没有该字段，则保留类默认值（当前时间）
                                _saved_ts=B.get(_s)
                                if _saved_ts and isinstance(_saved_ts,(int,float)) and _saved_ts>0:A.timestamp=int(_saved_ts)
                        except Exception as C:LOGGER.error(C)

                # 类属性 push_cache 是全实例共用的一份 dict，这里换新，避免多个实例互相消费对方的数据
                A.push_cache={};A.push_meta=_D

        # ────────────────────────────────────────────
        # save_data: bilezhou 原版 + 增强字段保存
        # ────────────────────────────────────────────
        async def save_data(B):
                # 只存"取数与解析"要的东西：账号、密码摘要、户号数据、新鲜时间。
                # 网页会话那套（keyCode/publicKey/accessToken/refreshToken/token）和 LLM 三项、
                # 备用邮箱、冷却时间戳都不再写——登录链已经整段删掉了，
                # 容器要的配置住在 state_grid_docker 的 store 里
                A={};A[_AT]=B.powerUserList;A[_Am]=B.doorAccountDict;A['is_debug']=B.is_debug
                A[_An]=VERSION;A[_j]=B.account;A[_AF]=B.password;A[_Ao]=B.refresh_interval
                # 保存 timestamp，使重启后 12 小时间隔判断仍然正确
                A[_s]=B.timestamp
                await async_save_to_store(B.hass,'state_grid.config',A)

        # ────────────────────────────────────────────
        # ingest_push: 供数总入口（sidecar 推送和 App 通道都走这里）
        # ────────────────────────────────────────────
        async def ingest_push(A,bundle):
                """把 {items:[{shape,consNo,response}]} 装进一次性缓存。

                每次推送整包替换：跨代残留会被下一轮误当成新数据消费。
                同一个 (接口, 户号) 允许挂多份载荷：网页在同一个接口上是**按年/按月问很多次**的
                （c24/f01 先问近 40 天再逐月回补、c01/f02 先问去年再问今年），一键只存一份的话
                主请求吃完就没有下一份了；命中时由 _push_covers 按期间和覆盖区间挑。
                """
                items=(bundle or {}).get('items') or []
                cache={};skipped=[]
                for it in items:
                        shape=(it or {}).get('shape');api=PUSH_SHAPE_API.get(shape);resp=(it or {}).get('response')
                        if api is _D or not isinstance(resp,dict):
                                skipped.append(str(shape));continue
                        if str(resp.get('code'))not in OK_CODES:
                                skipped.append(f"{shape}:code={resp.get('code')}");continue
                        cons=None if shape in PUSH_ACCOUNT_SHAPES else str(it.get('consNo') or '').strip()
                        if shape not in PUSH_ACCOUNT_SHAPES and not cons:
                                skipped.append(f"{shape}:无户号");continue
                        if shape=='daily_ele':_normalize_pushed_daily(resp)
                        year=((resp.get(_A) or {}).get('dataInfo') or {}).get('year')
                        days=[_d8(r['day']) for r in ((resp.get(_A) or {}).get('sevenEleList') or [])
                              if r.get('day')]
                        years={d[:4] for d in days}
                        # 跨年的 7 天窗口说不清自己算哪一年，就把期间留空，别猜
                        period=str(it.get('period') or year or
                                   (list(years)[0] if len(years)==1 else '')).strip() or None
                        span=(min(days),max(days)) if days else (None,None)
                        cache.setdefault((api,cons),[]).append((resp,period,span[0],span[1]))
                A.push_cache=cache
                A.push_pending=_V
                n=sum(len(v) for v in cache.values())
                A.push_meta={'received_at':int(time.time()*1000),'items':n,
                             'pushed_at':(bundle or {}).get('pushed_at'),'skipped':skipped[:8]}
                # 数据的"新鲜时间"就是推送时间：timestamp 不跟着走的话 12 小时闸门一直是开的，
                # 每 5 分钟的轮询都会重新走一遍解析（虽然不再发网络请求，也是白跑）
                A.timestamp=int(time.time()*1000)
                # 供数来源必须写进行情：App 通道和 sidecar 走的是同一个入口，
                # 上一版把这句固定写成 "sidecar 推送入仓"，结果 App 每轮灌数都在日志里
                # 冒充 sidecar，看不出来到底是谁在供数
                LOGGER.warning('%s 入仓 %d 份（%d 个键，跳过 %d）',
                               (bundle or {}).get('source') or '未标注来源的推送',n,len(cache),len(skipped))
                return n
        def handle_request_result_message(E,api,result,printResult=_V):
                D='message';C='resultMessage';A=result
                if E.is_debug and printResult:LOGGER.warning(api+'-'+json_dumps(A))
                B=_D
                if _A in A and A[_A]and _f in A[_A]and C in A[_A][_f]:B=A[_A][_f][C]
                elif _f in A and C in A[_f]:B=A[_f][C]
                elif D in A:B=A[D]
                else:B=json_dumps(A)
                return B
        # ────────────────────────────────────────────
        # 取数：只从推送缓存要载荷，不再发任何网络请求
        # ────────────────────────────────────────────
        async def __fetch_safe(A,api,data):
                """__fetch 的薄壳。留着这一层是为了 refresh_data 那整段一行都不用改
                —— 它调的一直是 __fetch_safe；网络与登录都去掉后，这里只剩统一异常出口。
                """
                try:
                        return await A.__fetch(api,data)
                except Exception as exc:
                        LOGGER.warning('取数异常(%s)：%s %s',api,type(exc).__name__,str(exc)[:120])
                        return {'code':'fetch_error','message':str(exc)[:120]}

        async def __fetch_web(A,api,data):
                """问一次网页。挂不上/供不上都回 _D（调用方按缺数处理），别在这里编造成功。"""
                # _D=还没试过；_V=试过但没挂上——两种都可以再试，但 async_attach 自己带闸门
                # （连致失败越多隔得越久），所以每格都走到这里也不会变成逐格敲登录。
                if A.web is _D or A.web is _V:A.web=await web_api.async_attach(A) or _V
                if A.web is _D or A.web is _V:return _D
                if A.web.unavailable():return _D   # 本轮已锁/当天已冷却：零请求直接回缓存
                A.userInfo=A.web.user_info or A.userInfo;A.token=A.web.token
                W0=await A.web.async_call(api,data)
                if W0:
                        LOGGER.warning('网页通道取到 %s',api.split("/member/")[-1])
                        A.fetch_ok+=1
                        return W0
                return _D

        async def __fetch(A,api,data,header=_D):
                # 供数顺序由 `web_priority` 定：真=每格先问网页、App 灌进来的载荷兜底；
                # 假=原来的顺序（缓存命中优先，网页只补没货的格子）。
                # 两条路的载荷与响应形状同一套（解析层不分叉），换顺序不改数值口径。
                if A.web_channel and A.web_priority:
                        W0=await A.__fetch_web(api,data)
                        if W0 is not _D:return W0
                # 推送缓存的载荷命中即用、一次一份：同一 (接口, 户号) 可以挂多份，
                # 由 _push_covers 按期间和覆盖区间挑。
                # 网页优先时没被消费的 App 载荷不会堆着：ingest_push 是整包替换，下一轮就换新
                if A.push_cache:
                        K0=_push_hit_key(api,data)
                        Q0=A.push_cache.get(K0) or []
                        for i in range(len(Q0)):
                                if _push_covers(api,data,Q0[i]):
                                        B0=Q0.pop(i)
                                        if not Q0:A.push_cache.pop(K0,_D)
                                        LOGGER.warning('命中推送缓存: %s 户号=%s',api,K0[1])
                                        A.fetch_ok+=1
                                        return B0[0]
                        LOGGER.warning('推送缓存未命中: %s 户号=%s 原因=%s 现有=%s',api,K0[1],
                                       '覆盖不足' if Q0 else '无此键',
                                       sorted('%s|%s' % ('/'.join(k[0].split('/')[-2:]), k[1])
                                              for k in A.push_cache))
                # App 优先那一路：缓存没货时才可以走网络（载荷是上面那些方法原样建的，
                # 响应也是原样的网页形状，所以解析层不用第二套语义）
                if A.web_channel and not A.web_priority:
                        W0=await A.__fetch_web(api,data)
                        if W0 is not _D:return W0
                # 这里不能碰 A.timestamp —— 它代表"数据新鲜度"，miss 也算新鲜的话 12 小时闸门就废了
                return {'code':'no_cache','message':'这一格网页没供上、缓存里也没有载荷'}

        async def __get_door_number(A):
                B=configuration[_Ac];G={_C:B[_C],_E:B[_E],_T:B[_T],_Q:{_l:B[_Q][_l],_m:B[_Q][_m],_n:B[_Q][_n],_o:B[_Q][_o]},_AX:{_W:A.userInfo[_W]},_AA:A.token};C=await A.__fetch_safe(get_door_number_api,G);H=A.handle_request_result_message('get_door_number_api',C)
                if _I in C and str(C[_I]) in ('1', '0000', '000000') and _A in C and _AG in C[_A]:
                        E={}
                        if A.powerUserList is not _D:E={A[_g]:A for A in A.powerUserList}
                        F=[]
                        for D in C[_A][_AG][_AT]:
                                if D[_g]in E:F.append(E[D[_g]])
                                elif _AY in D and D[_AY]!='05':F.append(D)
                        A.powerUserList=F;return{_G:0}
                return{_G:1,_y:H}

        async def __get_door_balance(C,door_account):
                A=door_account;E={_A:{_L:'',_O:'',_H:configuration[_j][_H],_B:configuration[_j][_B],_AH:C.userInfo[_W],_AI:C.userInfo.get(_Aa,C.userInfo.get('nickname',_D)),_R:_F,_b:_F,_AZ:C.userInfo[_W],_AB:[{'consNoSrc':A[_g],_Ab:A.get(_X,A.get(_AJ,_D)),'sceneType':A.get('consSortCode',A.get(_AY,_D)),_AC:A[_AC],_h:A[_h]}]},_C:'0101143',_E:configuration[_E],_T:A.get(_X,A.get(_AJ,_D))};B=await C.__fetch_safe(get_door_balance_api,E);C.handle_request_result_message('get_door_balance_api',B)
                if _I in B and str(B[_I]) in ('1', '000000') and _A in B and B[_A]and _AB in B[_A]:
                        D=B[_A][_AB]
                        if len(D)!=0:A[_Y]=D[0]

        async def __get_door_bill(C,door_account,year):
                F='dataInfo';D='mothEleList';A=door_account;G={_A:{_AH:C.userInfo[_W],_H:configuration[_H],_c:'11',_AK:A[_As],_B:'ALIPAY_01',_h:A[_h],_Ab:A[_X],_b:_F,_R:_F,_O:'',_L:'',_AI:'',_Aq:A[_X],_AZ:C.userInfo[_W],_AC:A[_g],_Ar:year},_C:_x,_E:_w,_T:A[_X]};B=await C.__fetch_safe(get_door_bill_api,G);C.handle_request_result_message('get_door_bill_api',B)
                if _I in B and str(B[_I]) in ('1', '000000') and _A in B and B[_A]:
                        if D in B[_A]:
                                if _S not in A:A[_S]=B[_A][D]
                                else:
                                        H={A[_Z]:A for A in A[_S]};I=B[_A][D]
                                        for E in I:
                                                if E[_Z]not in H:A[_S].append(E)
                        if F in B[_A]:return B[_A][F]

        async def __get_door_mouth_bill(F,door_account,monthBill):
                global _ladder_shape_logged
                M='billRead';J=monthBill;G='pointList';E='readList';C=door_account;K=datetime.datetime.strptime(J[_Z],'%Y%m');N=f"{K.year}-{K.month:02d}";O={_A:{_H:configuration[_k][_H],_B:configuration[_k][_B],_R:configuration[_k][_R],_c:configuration[_k][_c],_AC:C[_g],_b:C[_X],_h:C[_h],'queryDate':N,_Aq:C[_X],_AK:C[_As],_AZ:F.userInfo[_W],_O:'',_L:'',_AI:F.userInfo[_Aa],_AH:F.userInfo[_W]},_C:configuration[_k][_C],_E:configuration[_k][_E],_T:C[_X]};B=await F.__fetch(get_door_ladder_api,O);Q=F.handle_request_result_message('get_door_ladder_api',B)
                if _I in B and str(B[_I]) in ('1', '000000') and _A in B and B[_A]and _AB in B[_A]:
                        A=B[_A][_AB][0];H=0;L=0;D=[]
                        if E in A and len(A[E])>0:D=A[E]
                        elif G in A and len(A[G])>0 and E in A[G][0]and len(A[G][0][E])>0:D=A[G][0][E]
                        if len(D)==0 and not _ladder_shape_logged:
                                _ladder_shape_logged=_V
                                LOGGER.warning('c04/f03 的 list[0] 里没有 readList/pointList，抄表读数取不到；实际键=%s',sorted(A)[:16])
                        if len(D)>0:
                                L=catchFloat(D[0],'activeCount')
                                if M in D[0]:
                                        for P in D[0][M]:H=max(H,catchInt(P,'currentNumber'))
                        I={};I[_At]=H;I[_AD]=normal_round(L,2);J[_AL]=I

        async def __get_door_daily_bill(E,door_account,year,start_date,end_date,monthBill=_D):
                F='sevenEleList';D=monthBill;C=door_account;L={'params1':{_C:configuration[_C],_E:configuration[_E],_T:configuration[_T],_Q:{_l:configuration[_Q][_l],_m:configuration[_Q][_m],_n:configuration[_Q][_n],_o:configuration[_Q][_o]},_AX:{_W:E.userInfo[_W]},_AA:E.token},'params3':{_A:{_AH:E.userInfo[_W],_AC:C[_g],_AK:_a,'endTime':end_date,_h:C[_h],_Ar:year,_Ab:C.get(_X,C.get(_AJ,_D)),_O:'',_L:'','startTime':start_date,_AI:E.userInfo[_Aa],_B:configuration[_d][_B],_H:configuration[_d][_H],_c:configuration[_d][_c],_b:configuration[_d][_b],_R:configuration[_d][_R]},_C:configuration[_d][_C],_E:configuration[_d][_E],_T:C.get(_X,C.get(_AJ,_D))},'params4':'010103'};B=await E.__fetch_safe(get_door_daily_bill_api,L);E.handle_request_result_message('get_door_daily_bill_api',B)
                if _I in B and str(B[_I]) in ('1', '000000') and _A in B and B[_A]and F in B[_A]:
                        if D is _D:C[_i]=B[_A][F]
                        else:
                                G=0;H=0;I=0;J=0;K=0
                                for A in B[_A][F]:A[_t]=catchFloat(A,_t);A[_z]=catchFloat(A,_z);A[_A0]=catchFloat(A,_A0);A[_A1]=catchFloat(A,_A1);A[_A2]=catchFloat(A,_A2);G+=A[_t];H+=A[_z];I+=A[_A0];J+=A[_A1];K+=A[_A2]
                                D[_AD]=normal_round(G,2);D[_A3]=normal_round(H,2);D[_A4]=normal_round(I,2);D[_A5]=normal_round(J,2);D[_A6]=normal_round(K,2);D[_Au]=B[_A][F]

        # ────────────────────────────────────────────
        # refresh_data: bilezhou 原版（不变）
        # ────────────────────────────────────────────
        async def refresh_data(C,force_refresh=_N):
                A5='recent_12_monthly_ele_list';A4='recent_30_daily_ele_list';A3='monthEleCost';A2='last_month_ele_cost';A1='year_ele_cost';A0='%Y%m%d';z='daily_lasted_date';y='isMent';f=force_refresh;e='monthEleNum';d='last_month_ele_num';T='year_ele_num';S='yearTotalCost';R='day';J='year_bill_list';I='balance'
                # 保存原始 timestamp：__fetch 内部会更新它用于签名，
                # 如果本次刷新中途失败（登录失败/异常），还原 timestamp 避免下次 12 小时判断错误
                _orig_ts=C.timestamp
                # 推送是一次性的触发器：这一轮用掉就熄灭。push_cache 里可能有本轮用不到的形状
                # （比如暂时还抓不到的阶梯），拿它非空当强制刷新条件的话，每 5 分钟的轮询
                # 都会被迫重跑一遍整段解析
                C.push_pending=_N
                C.fetch_ok=0
                try:
                        if f:await C.__get_door_number()
                        A6=f or int(time.time()*1000)-C.timestamp>C.refresh_interval*3600*1000
                        if A6 is _N:return
                        H=datetime.datetime.now();D=H-datetime.timedelta(days=1);U=f"{D.year}-{D.month:02d}-{D.day:02d}";F=D-datetime.timedelta(days=40);V=f"{F.year}-{F.month:02d}-{F.day:02d}"
                        if not C.powerUserList:LOGGER.warning('本轮电表列表为空，没有任何户号可取数')
                        for A in C.powerUserList:
                                A7=A[_g];C.doorAccountDict[A7]=A;await C.__get_door_balance(A)
                                if _Y in A:
                                        g=catchFloat(A[_Y],'accountBalance');AB=catchFloat(A[_Y],'estiAmt');AC=catchFloat(A[_Y],'prepayBal');W=catchFloat(A[_Y],'sumMoney');AD=catchFloat(A[_Y],'historyOwe');h=A[_Y][_AK];i=''
                                        if y in A[_Y]:i=A[_Y][y]
                                        A8=h==_F;X=h=='0';j=not(not X or i!=_F)
                                        if A8:A[I]=W
                                        if X and not j:A[I]=-abs(W)
                                        if X and j:A[I]=W
                                        # accountBalance 字段存在就用它的真实值（即使为 0）
                                        # 修复：余额为 0 时不能被前面 sumMoney 兜底覆盖
                                        if 'accountBalance' in A[_Y]:A[I]=g
                                else:LOGGER.error('国家电网账户余额获取失败！')
                                if I not in A:A[I]=0
                                await C.__get_door_daily_bill(A,H.year,V,U)
                                if _i not in A:LOGGER.error('国家电网无法获取日用电数据！');continue
                                Y=0;K=_N
                                for k in range(10):
                                        E=A[_i][k]
                                        try:float(E[_t]);K=_V;break
                                        except:Y=Y+1
                                l=0;m=0;n=0;o=0;p=0;A[z]=f"{H.year}-{H.month:02d}-{H.day:02d}"
                                if K:
                                        for k in range(Y):A[_i].pop(0)
                                        E=A[_i][0];G=datetime.datetime.strptime(E[R],A0);A[z]=f"{G.year}-{G.month:02d}-{G.day:02d}";l=catchFloat(E,_t);m=catchFloat(E,_z);n=catchFloat(E,_A0);o=catchFloat(E,_A1);p=catchFloat(E,_A2)
                                A['daily_ele_num']=normal_round(l,2);A['daily_p_ele_num']=normal_round(m,2);A['daily_v_ele_num']=normal_round(n,2);A['daily_n_ele_num']=normal_round(o,2);A['daily_t_ele_num']=normal_round(p,2);q=0;r=0;s=0;t=0;u=0
                                if K:
                                        for B in A[_i]:
                                                A9=datetime.datetime.strptime(B[R],A0)
                                                if A9.month!=G.month:break
                                                q+=catchFloat(B,_t);r+=catchFloat(B,_z);s+=catchFloat(B,_A0);t+=catchFloat(B,_A1);u+=catchFloat(B,_A2)
                                A[_AD]=normal_round(q,2);A[_A3]=normal_round(r,2);A[_A4]=normal_round(s,2);A[_A5]=normal_round(t,2);A[_A6]=normal_round(u,2)
                                if K:
                                        Z=G-datetime.timedelta(days=G.day)
                                        if _S not in A or len(A[_S])<12:await C.__get_door_bill(A,Z.year-1)
                                        v=await C.__get_door_bill(A,Z.year)
                                        if v is not _D:A[S]=v
                                        w=[]
                                        if _S in A:
                                                for B in A[_S]:
                                                        if _AL not in B:await C.__get_door_mouth_bill(A,B)
                                                        if _Au not in B:
                                                                AA,F,D=get_month_date_range(B[_Z])
                                                                # 用独立局部变量，避免污染循环外 V/U
                                                                # （否则下一个户号 __get_door_daily_bill 会拿到错误的日期范围）
                                                                _ms=f"{F.year}-{F.month:02d}-{F.day:02d}"
                                                                _me=f"{D.year}-{D.month:02d}-{D.day:02d}"
                                                                await C.__get_door_daily_bill(A,int(AA),_ms,_me,B)
                                                        if B[_Z].startswith(str(Z.year)):w.append(B)
                                        A[J]=sorted(w,key=lambda x:x[_Z],reverse=_V)
                                if S in A:A[T]=catchFloat(A[S],'totalEleNum');A[A1]=catchFloat(A[S],'totalEleCost')
                                if T not in A:A[T]=0;A[A1]=0
                                x=0;a=D
                                if J in A and len(A[J])>0:
                                        L=A[J][0];A[d]=catchFloat(L,e);A[A2]=catchFloat(L,A3)
                                        if _AL in L:x=L[_AL][_At]
                                        a=datetime.datetime.strptime(L[_Z],'%Y%m')
                                if d not in A:A[d]=0;A[A2]=0
                                A['last_month_meter_num']=int(x);M=0;N=0;O=0;P=0;Q=0
                                if a.month==12:M=A[_AD];N=A[_A3];O=A[_A4];P=A[_A5];Q=A[_A6]
                                else:
                                        if J in A:
                                                # 月条目里的 month_*_ele_num 是按日回补成功时才写的
                                                # （__get_door_daily_bill 带 monthBill 那条路），没补上就缺键。
                                                # M 本来就是 catchFloat 读法，这里统一，免得某个月回补失败炸掉整轮。
                                                for B in A[J]:M+=catchFloat(B,e);N+=catchFloat(B,_A3);O+=catchFloat(B,_A4);P+=catchFloat(B,_A5);Q+=catchFloat(B,_A6)
                                        if K and G.month!=a.month:M+=A[_AD];N+=A[_A3];O+=A[_A4];P+=A[_A5];Q+=A[_A6]
                                A[T]=normal_round(M,2);A['year_p_ele_num']=normal_round(N,2);A['year_v_ele_num']=normal_round(O,2);A['year_n_ele_num']=normal_round(P,2);A['year_t_ele_num']=normal_round(Q,2)
                                if _i in A:
                                        b=[]
                                        for B in A[_i][:30]:b.append({R:B[R],'ele':normal_round(catchFloat(B,_t),2),'v_ele':normal_round(catchFloat(B,_A0),2),'p_ele':normal_round(catchFloat(B,_z),2),'n_ele':normal_round(catchFloat(B,_A1),2),'t_ele':normal_round(catchFloat(B,_A2),2)})
                                        b.reverse();A[A4]=b
                                else:A[A4]=[]
                                if _S in A:
                                        A[_S]=sorted(A[_S],key=lambda x:x[_Z],reverse=_V);c=[]
                                        for B in A[_S][:12]:c.append({_Z:B[_Z],'cost':normal_round(catchFloat(B,A3),2),'ele':normal_round(catchFloat(B,e),2),'v_ele':catchFloat(B,_A4),'p_ele':catchFloat(B,_A3),'n_ele':catchFloat(B,_A5),'t_ele':catchFloat(B,_A6)})
                                        c.reverse();A[A5]=c
                                else:A[A5]=[]
                                A['refresh_time']=datetime.datetime.strftime(H,'%Y-%m-%d %H:%M:%S')
                        # 本轮供上过格子就把"数据新鲜度"推到当下：这句必须在这儿，
                        # 因为网页优先的一轮可以一次都不碰推送缓存，而原先只有 ingest_push 会推
                        # timestamp——缺了它，12 小时闸门从 00:36 起就一直是开的，
                        # 10-10 实测每 5 分钟重跑整轮、一天 953 发网页请求。
                        # 一格都没供上时仍然不推（让下一轮接着试），异常那条路照旧还原 _orig_ts。
                        if C.fetch_ok:C.timestamp=int(time.time()*1000)
                        await C.save_data()
                except Exception:
                        # 裸 except 会把中断现场一起吞掉，排查时什么线索都没有。回溯无条件打：
                        # 一轮中断本来就少见，憋到 is_debug 才打，出问题时日志里就是空白。
                        # 行为（还原 timestamp、返回 0）保持不变。
                        LOGGER.exception('refresh_data 中断')
                        # 异常时还原 timestamp，避免下次 12 小时判断错误
                        C.timestamp=_orig_ts
                        return 0

        def get_door_account_list(A):return list(A.doorAccountDict.values())
        def get_door_account(A):return A.doorAccountDict
