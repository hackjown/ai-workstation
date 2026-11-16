"""v3.9.50 (task-271): 人民币金额大写, and a check of every 大写 amount in a delivered document.

task-271 wrote its own rmb_upper() for a bid response file and delivered 171,866.00 元 as
"壹拾柒万零壹仟捌佰陆拾陆元整" (correct: 壹拾柒万壹仟捌佰陆拾陆元整). Its own text check found the correct form 0 times
and it still sent the file; a bid whose 大写 and 小写 disagree is judged by the 大写 or thrown out.

* upper(amount)            the standard form (the leading 壹拾 kept, 元整 when there are no 角分)
* accepted(amount)         every form the 支付结算办法 accepts (the optional 零 at a 万/元 boundary, 拾 without 壹 …)
* parse(text)              a 大写 amount back to a Decimal (None when it is not one)
* docx_text(data)          the text of a .docx (standard library only)
* check_text(text)         [{found, value, expected, problem}] for every wrong 大写 amount in the text
* check_file(data, name)   the same for a delivered file (.docx; .pdf when PyMuPDF is installed), [] otherwise

Pure, standard library only; tested in tests/test_v3950_doc_tasks.py.
"""
from __future__ import annotations

import io
import re
import zipfile
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

DIGITS = "零壹贰叁肆伍陆柒捌玖"
UNITS = ["", "拾", "佰", "仟"]
BIG = ["", "万", "亿", "万亿"]
_D = {c: i for i, c in enumerate(DIGITS)}
_D.update({"〇": 0, "两": 2})

# a 大写 amount: the digits and units, ending in 元/圆 + 整/正, or in 角/分
AMOUNT_RE = re.compile(r"(?:人民币)?[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]+[元圆](?:整|正|[零壹贰叁肆伍陆柒捌玖]角(?:[零壹贰叁肆伍陆柒捌玖]分)?(?:整|正)?|"
                       r"零?[零壹贰叁肆伍陆柒捌玖]分(?:整|正)?)?|(?:人民币)?零?[壹贰叁肆伍陆柒捌玖]角(?:[零壹贰叁肆伍陆柒捌玖]分)?(?:整|正)?")


def _cents(amount) -> int:
    try:
        d = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as e:
        raise ValueError(f"not an amount: {amount!r}") from e
    if d < 0:
        raise ValueError("negative amount")
    return int(d * 100)


def _group(n: int, lead_one: bool) -> list[str]:
    """One 4-digit group (1..9999) -> its forms: inner zeros read 零 once; 拾 alone or 壹拾 when it leads."""
    ds = [n // 1000 % 10, n // 100 % 10, n // 10 % 10, n % 10]
    out, zero, started = "", False, False
    for i, d in enumerate(ds):
        if d == 0:
            if started:
                zero = True
            continue
        if zero:
            out += "零"
            zero = False
        out += DIGITS[d] + UNITS[3 - i]
        started = True
    forms = [out]
    if lead_one and out.startswith("壹拾"):
        forms.append(out[1:])   # 拾万 / 拾元 at the very start is accepted too
    return forms


def _integer_forms(yuan: int) -> list[str]:
    if yuan == 0:
        return [""]
    groups = []
    while yuan:
        groups.append(yuan % 10000)
        yuan //= 10000
    groups.reverse()
    forms = [""]
    prev = None
    for idx, g in enumerate(groups):
        big = BIG[len(groups) - 1 - idx]
        if g == 0:
            prev = g
            continue
        glue = [""]
        if prev is not None:
            ends_zero = prev % 10 == 0          # the higher group ends in 0 (… 0万)
            if g < 1000 or prev == 0:
                glue = ["零"]                    # 壹拾万零伍佰 / 壹亿零伍万: required
            elif ends_zero:
                glue = ["", "零"]               # 壹佰万伍仟 or 壹佰万零伍仟: both accepted
        gf = _group(g, lead_one=not forms[0])
        forms = [f + z + x + big for f in forms for z in glue for x in gf]
        prev = g
    # a group of all zeros between two non-zero groups (1 0000 0005) already got its 零 through prev == 0
    return forms


def accepted(amount) -> set[str]:
    c = _cents(amount)
    yuan, jiao, fen = c // 100, c // 10 % 10, c % 10
    ints = _integer_forms(yuan)
    out = set()
    for i in ints:
        if jiao == 0 and fen == 0:
            for end in ("元整", "元正", "圆整", "圆正"):
                out.add((i or "零") + end)
            continue
        tails = []
        if jiao and fen:
            tails = [DIGITS[jiao] + "角" + DIGITS[fen] + "分"]
            if yuan % 10 == 0 and yuan:
                tails += ["零" + t for t in list(tails)]
        elif jiao:
            tails = [DIGITS[jiao] + "角", DIGITS[jiao] + "角整", DIGITS[jiao] + "角正"]
            if yuan % 10 == 0 and yuan:
                tails += ["零" + t for t in list(tails)]
        else:   # 分 only: 零 is required between 元 and 分
            tails = ["零" + DIGITS[fen] + "分"]
        for t in tails:
            if i:
                out.add(i + "元" + t)
                out.add(i + "圆" + t)
            else:
                out.add(t.lstrip("零"))
    return out


def upper(amount) -> str:
    """The standard form: 壹拾柒万壹仟捌佰陆拾陆元整, 壹佰万零伍仟元整 → 壹佰万伍仟元整, 1.05 → 壹元零伍分."""
    c = _cents(amount)
    yuan, jiao, fen = c // 100, c // 10 % 10, c % 10
    ints = [f for f in _integer_forms(yuan) if not f.startswith("拾")]
    ints.sort(key=len)
    i = ints[0] if ints else ""
    if jiao == 0 and fen == 0:
        return (i or "零") + "元整"
    if not i:
        return (DIGITS[jiao] + "角" if jiao else "") + (DIGITS[fen] + "分" if fen else "")
    if jiao and fen:
        return i + "元" + DIGITS[jiao] + "角" + DIGITS[fen] + "分"
    if jiao:
        return i + "元" + DIGITS[jiao] + "角整"
    return i + "元零" + DIGITS[fen] + "分"


def parse(text: str) -> Optional[Decimal]:
    """A 大写 amount -> Decimal; 零 is ignored, so a misplaced 零 parses to the intended number."""
    t = re.sub(r"^人民币|[整正]$", "", (text or "").strip())
    t = t.replace("圆", "元")
    if not t:
        return None
    yuan_part, _, small = t.partition("元")
    if "元" not in t:
        yuan_part, small = "", t
    total = 0
    section = 0
    num = 0
    for ch in yuan_part:
        if ch in _D:
            num = _D[ch]
        elif ch in "拾佰仟":
            section += (num or 1) * {"拾": 10, "佰": 100, "仟": 1000}[ch]
            num = 0
        elif ch == "万":
            total += (section + num) * 10000
            section = num = 0
        elif ch == "亿":
            total = (total + section + num) * 100000000
            section = num = 0
        else:
            return None
    total += section + num
    jiao = fen = 0
    m = re.fullmatch(r"零?(?:([零壹贰叁肆伍陆柒捌玖])角)?零?(?:([零壹贰叁肆伍陆柒捌玖])分)?", small)
    if small and not m:
        return None
    if m:
        jiao = _D.get(m.group(1) or "零", 0)
        fen = _D.get(m.group(2) or "零", 0)
    return Decimal(total) + Decimal(jiao) / 10 + Decimal(fen) / 100


_SMALL = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?![\d])")


def _small_amounts(text: str, money_only: bool = False) -> set[Decimal]:
    """小写 figures; money_only keeps the ones written as money (1,234 / 12.50 / ¥ before / 元 after)."""
    out = set()
    for m in _SMALL.finditer(text or ""):
        if money_only and not ("," in m.group(1) or m.group(2) or re.search(r"[¥￥]\s*$", text[:m.start()])
                               or re.match(r"\s*(?:元|圆|RMB|CNY)", text[m.end():])):
            continue
        try:
            out.add(Decimal(m.group(1).replace(",", "") + "." + (m.group(2) or "0")).quantize(Decimal("0.01")))
        except InvalidOperation:
            continue
    return out


def check_text(text: str) -> list[dict]:
    """Every 大写 amount in the text that is not written the way its own value is written, or whose value appears
    nowhere in the text as a 小写 figure (a 大写 that does not match the price it spells out)."""
    issues, seen = [], set()
    smalls = _small_amounts(text)
    for m in AMOUNT_RE.finditer(text or ""):
        raw = m.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        core = re.sub(r"^人民币", "", raw)
        if not re.search(r"[元圆]", core) and len(core) < 2:
            continue
        v = parse(core)
        if v is None:
            issues.append({"found": raw, "value": None, "expected": None, "problem": "写法无法识别"})
            continue
        ok_forms = accepted(v)
        if core not in ok_forms and core.rstrip("整正") not in {f.rstrip("整正") for f in ok_forms}:
            issues.append({"found": raw, "value": str(v), "expected": upper(v), "problem": "大写写法错误"})
            continue
        # 大写 next to a 小写 price (the same line / table row) must spell that price: 171,866.00（大写：壹拾柒万壹仟玖佰元整）
        near = _small_amounts(text[max(0, m.start() - 80):m.start()] + " " + text[m.end():m.end() + 40], money_only=True)
        near = {x for x in near if x >= 100}
        if v >= 100 and near and v.quantize(Decimal("0.01")) not in near and v.quantize(Decimal("0.01")) not in smalls:
            issues.append({"found": raw, "value": str(v), "expected": None, "problem": "文档里没有和它一致的小写金额"})
    return issues


def docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = [n for n in z.namelist() if re.match(r"word/(document|header\d*|footer\d*)\.xml$", n)]
            xml = " ".join(z.read(n).decode("utf-8", "ignore") for n in parts)
    except Exception:  # noqa: BLE001
        return ""
    xml = re.sub(r"</w:p>|<w:tab/>|<w:br/>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def pdf_text(data: bytes) -> str:
    try:
        try:
            import pymupdf as _fz   # type: ignore
        except ImportError:
            import fitz as _fz       # type: ignore
        doc = _fz.open(stream=data, filetype="pdf")
        return "\n".join(p.get_text() for p in doc)
    except Exception:  # noqa: BLE001
        return ""


def file_text(data: bytes, name: str) -> str:
    n = (name or "").lower()
    if n.endswith(".docx"):
        return docx_text(data)
    if n.endswith(".pdf"):
        return pdf_text(data)
    if n.endswith((".txt", ".md", ".csv", ".html", ".htm")):
        return data.decode("utf-8", "ignore")
    return ""


def check_file(data: bytes, name: str) -> list[dict]:
    t = file_text(data, name)
    return check_text(t) if t else []


def refusal(name: str, issues: list[dict]) -> str:
    """The note for the model when send_file is refused for wrong 大写 amounts."""
    lines = []
    for i in issues[:6]:
        if i["problem"] == "大写写法错误":
            lines.append(f"「{i['found']}」({i['value']} 元) 应写作「{i['expected']}」")
        elif i["problem"] == "文档里没有和它一致的小写金额":
            lines.append(f"「{i['found']}」= {i['value']} 元，但文档里没有这个小写金额（大小写不一致）")
        else:
            lines.append(f"「{i['found']}」写法无法识别")
    return (f"REFUSED: {name} 的金额大写有错，不能交付（大小写不一致的报价文件会被判无效）：\n- " + "\n- ".join(lines) +
            "\nFix it at the source (the script / data that writes the document), regenerate, then send_file again. "
            "In python use exactly this converter instead of writing your own:\n" + SNIPPET)


SNIPPET = '''def rmb_upper(v):
    from decimal import Decimal, ROUND_HALF_UP
    c = int(Decimal(str(v)).quantize(Decimal("0.01"), ROUND_HALF_UP) * 100)
    D, U, B = "零壹贰叁肆伍陆柒捌玖", ["", "拾", "佰", "仟"], ["", "万", "亿"]
    y, j, f = c // 100, c // 10 % 10, c % 10
    gs = []
    while y: gs.append(y % 10000); y //= 10000
    s, prev = "", None
    for k in range(len(gs) - 1, -1, -1):
        g = gs[k]
        if g == 0: prev = 0; continue
        if prev is not None and (g < 1000 or prev == 0): s += "零"
        ds, z, st = [g // 1000, g // 100 % 10, g // 10 % 10, g % 10], False, False
        for i, d in enumerate(ds):
            if d == 0: z = z or st; continue
            if z: s += "零"; z = False
            s += D[d] + U[3 - i]; st = True
        s += B[k]; prev = g
    if not j and not f: return (s or "零") + "元整"
    if not s: return (D[j] + "角" if j else "") + (D[f] + "分" if f else "")
    return s + "元" + (D[j] + "角" if j else "零") + (D[f] + "分" if f else ("" if not j else "整"))'''
