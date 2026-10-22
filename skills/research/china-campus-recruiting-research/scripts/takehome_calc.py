# -*- coding: utf-8 -*-
"""税前 -> 税后 计算器 (中国大陆, 个税政策口径至 2027-12-31).

用法:
    python takehome_calc.py                      # 打印默认示例表
    python takehome_calc.py 15000 15 杭州 0.12 1500
    python takehome_calc.py --md 15000 15 杭州   # markdown 表格行

参数: 月薪 薪数 [城市] [公积金比例] [专项附加扣除/月] [--md] [--no-rent=...]

口径:
- 个人社保 = 养老8% + 医疗2% + 失业%(城市各异), 按实际月薪计缴(受当地基数上下限约束)
- 公积金: 个人与单位同比例, 默认 12%; 单位那份也进个人账户
- 个税: 综合所得年度累计 (减除费用60000 + 三险一金个人部分 + 专项附加扣除)
- 年终奖: 单独计税 (÷12 查月度税率表), 政策延续至 2027-12-31

注意: 只有 base cap 是逐城可核实的; 其余 cap 仅是量级占位, 用前按当地人社厅/公积金中心
公布的最新年度通知复核。
"""
import sys

# 养老%, 医疗%, 失业%, 社保基数上限(元/月)
CITIES = {
    "杭州": dict(pension=0.08, medical=0.02, unemp=0.005, si_cap=25299),
    "上海": dict(pension=0.08, medical=0.02, unemp=0.005, si_cap=36921),
    "广州": dict(pension=0.08, medical=0.02, unemp=0.002, si_cap=27501),
    "深圳": dict(pension=0.08, medical=0.02, unemp=0.003, si_cap=27501),
    "北京": dict(pension=0.08, medical=0.02, unemp=0.005, si_cap=35283),
    "成都": dict(pension=0.08, medical=0.02, unemp=0.004, si_cap=21228),
    # 南京: 个人养老8% + 医疗2% + 失业0.5% (与江苏全省口径一致)。
    # si_cap 取江苏省人社厅公布的社保缴费基数上限粗值; 月薪低于该值时 cap 不生效,
    # 故 9K~20K 档位的到手计算不受此参数影响。
    "南京": dict(pension=0.08, medical=0.02, unemp=0.005, si_cap=24396),
    # 扬州: 江苏省口径与南京一致 (养老8% + 医疗2% + 失业0.5%).
    "扬州": dict(pension=0.08, medical=0.02, unemp=0.005, si_cap=24396),
    # 苏州: 同属江苏省口径, 个人 养老8% + 医疗2% + 失业0.5%; 基数上限随省通知走。
    # (实测缺省会报 KeyError: '苏州' —— 苏州是本人四个目标城市之一, 必须常备。)
    "苏州": dict(pension=0.08, medical=0.02, unemp=0.005, si_cap=24396),
}
YEAR_BRACKETS = [(36000, .03, 0), (144000, .10, 2520), (300000, .20, 16920),
                 (420000, .25, 31920), (660000, .30, 52920), (960000, .35, 85920),
                 (float("inf"), .45, 181920)]
MONTH_BRACKETS = [(3000, .03, 0), (12000, .10, 210), (25000, .20, 1410),
                  (35000, .25, 2660), (55000, .30, 4410), (80000, .35, 7160),
                  (float("inf"), .45, 15160)]


def _bracket(taxable, table):
    for cap, rate, quick in table:
        if taxable <= cap:
            return max(0.0, taxable * rate - quick)
    return 0.0


def bonus_tax(bonus):
    """年终奖单独计税: 用 bonus/12 找税率档位, 但税额 = 全额奖金 x 税率 - 速算扣除数。
    切勿把 bonus/12 直接丢进 _bracket —— 那会把税算成月均的那部分, 少交 12 倍。"""
    if bonus <= 0:
        return 0.0
    m = bonus / 12
    for cap, rate, quick in MONTH_BRACKETS:
        if m <= cap:
            return max(0.0, bonus * rate - quick)
    return 0.0


def calc(monthly, months=15, city="杭州", hf_rate=0.12, special_month=0.0):
    c = CITIES[city]
    si_base = min(monthly, c["si_cap"])
    si_p = si_base * (c["pension"] + c["medical"] + c["unemp"])   # 个人社保/月
    hf_p = monthly * hf_rate                                        # 个人公积金/月
    wage, bonus = monthly * 12, monthly * max(0, months - 12)
    it_wage = _bracket(wage - 60000 - (si_p + hf_p) * 12 - special_month * 12, YEAR_BRACKETS)
    it_bonus = bonus_tax(bonus)
    gross = wage + bonus
    net = gross - (si_p + hf_p) * 12 - it_wage - it_bonus
    return dict(
        税前年薪=round(gross), 税前月薪=round(monthly), 薪数=months,
        个人社保年=round(si_p * 12), 个人公积金年=round(hf_p * 12),
        全年个税=round(it_wage + it_bonus), 税后到手年=round(net),
        税后到手月均=round(net / 12),
        普通月到手=round(monthly - si_p - hf_p - it_wage / 12),
        公积金账户年入=round(monthly * hf_rate * 2 * 12),
        含公积金年总收益=round(net + monthly * hf_rate * 2 * 12),
        实际税负率=f"{100 * (it_wage + it_bonus) / gross:.1f}%",
    )


W = "万"


def row(r):
    return (f"| {r['税前月薪']:,} | {r['税前年薪']/10000:.1f}{W} | "
            f"{(r['个人社保年']+r['个人公积金年'])/10000:.2f}{W} | {r['全年个税']/10000:.2f}{W} | "
            f"**{r['税后到手年']/10000:.2f}{W}** | **{r['税后到手月均']:,}元** | "
            f"{r['公积金账户年入']/10000:.2f}{W} | {r['含公积金年总收益']/10000:.2f}{W} |")


def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    md = "--md" in sys.argv
    if not a:
        print("| 税前月薪 | 税前年薪 | 个人社保+公积金(年) | 全年个税 | 税后到手(年) | 税后到手(月均) | 公积金账户年入 | 含公积金总收益(年) |")
        print("|---|---|---|---|---|---|---|---|")
        for m in (12000, 15000, 18000, 20000, 25000):
            print(row(calc(m, 15, "杭州", 0.12, 1500)))
        return
    m = float(a[0]); months = int(a[1]) if len(a) > 1 else 15
    city = a[2] if len(a) > 2 else "杭州"
    hf = float(a[3]) if len(a) > 3 else 0.12
    sp = float(a[4]) if len(a) > 4 else 1500
    r = calc(m, months, city, hf, sp)
    print(row(r) if md else "\n".join(f"{k}: {v}" for k, v in r.items()))


if __name__ == "__main__":
    main()
