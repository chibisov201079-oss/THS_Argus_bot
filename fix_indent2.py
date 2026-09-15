# -*- coding: utf-8 -*-
"""
v2: правильно обрабатывает re.sub (передаёт NEW_FUNC через функцию-callback,
а не как строку-шаблон, чтобы re не трогал \n внутри f-строк).
"""
import re, sys, io

FNAME = "radar_bot.py"

NEW_FUNC = '''def full_analysis(sym):
    m = find_coin(sym)
    if not m: return "Не нашёл монету в топ-100."
    card, _ = coin_card(sym)
    tvl = tvl_info(sym)
    ctx = (card or "") + ("\\nTVL: НЕ ПРЕДОСТАВЛЕНО" if not tvl else f"\\nTVL: ${tvl['tvl']/1e6:,.0f}M, 7д {tvl['chg7d']:+.1f}%")
    bars = klines(sym); risk_block = "РИСК: НЕ ПРЕДОСТАВЛЕНО"
    if bars:  # РИСК-блок считает КОД, а не LLM (правило методологии)
        c = [b[3] for b in bars]; last = c[-1]
        sup = min(b[2] for b in bars[-30:]); res = max(b[1] for b in bars[-30:])
        stop = sup * 0.99
        if last > stop:
            bal, rp = 10000, 1.0
            per = last - stop; qty = (bal * rp / 100) / per; rr = (res - last) / per
            risk_block = (f"РИСК (посчитано кодом): вход ~${last:,.6g}, стоп-кандидат ${stop:,.6g} "
                          f"(под 30д-минимумом), цель-кандидат ${res:,.6g} (30д-максимум). "
                          f"Объём при счёте ${bal}×{rp}%: {qty:,.4g}. R:R {rr:.1f}:1 "
                          + ("— ОК." if rr >= 2 else "— НИЖЕ 2:1, план говорит «нет».")
                          + " Три условия отмены сформулируй; самое слабое допущение укажи.")
    prompt = (f"Ты мой ИССЛЕДОВАТЕЛЬСКИЙ ОТДЕЛ ПО КРИПТОРЫНКУ. По [{sym}] пройди шаги по порядку: "
              f"СКАН → РАЗБОР → ИЗУЧЕНИЕ → РИСК → ПЛАН.\\nМОИ ДАННЫЕ (с датами и источниками):\\n{ctx}\\n"
              f"{risk_block}\\nНОВОСТИ: НЕ ПРЕДОСТАВЛЕНО — проверь вручную.\\n"
              f"СКАН: подтверди одним абзацем, стоит ли исследовать сейчас. РАЗБОР: тренд, импульс, "
              f"уровни, поведение цены — только из данных. МНЕНИЕ отдельно. ИЗУЧЕНИЕ: таблица ФАКТОВ "
              f"(показатель|значение|дата|источник) из моих данных; драйверы пометь как требующие "
              f"ручной проверки. ПЛАН: заполни шаблон (идея/направление и срок/вход/стоп/цели/объём/"
              f"R:R/отмена 3 пункта/уверенность/числа на проверку). В конце: СТАТУС: ЖДЁТ ПРОВЕРКИ ЧЕЛОВЕКОМ.\\n"
              f"ПРАВИЛА: не выдумывай числа — если данных нет, пиши «НЕ ПРЕДОСТАВЛЕНО — проверь вручную». "
              f"ФАКТЫ отдельно от МНЕНИЯ. Заканчивай строкой УВЕРЕННОСТЬ: низкая/средняя/высокая. "
              f"Никаких обещаний прибыли и указаний покупать/продавать.")
    try:
        from openai import OpenAI
        cl = OpenAI(api_key=OPENAI_API_KEY, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
        ans = cl.chat.completions.create(model="gemini-2.0-flash", messages=[{"role": "user", "content": prompt}], max_tokens=1200, timeout=90)
        return (ans.choices[0].message.content.strip()[:3800]
                + "\\n\\n🧮 Риск-блок посчитан кодом, текст — LLM." + DISCLAIMER)
    except Exception as e:
        return f"LLM-разбор не удался ({e}). Проверь OPENAI_API_KEY в config.py или попробуй позже."
'''

def main():
    with io.open(FNAME, "r", encoding="utf-8") as f:
        src = f.read()

    pattern = re.compile(
        r"def full_analysis\(sym\):.*?(?=\n(?:def |class |# =+))",
        re.DOTALL
    )

    if not pattern.search(src):
        print("Не нашёл функцию full_analysis — ничего не менял.")
        sys.exit(1)

    replacement_text = NEW_FUNC.rstrip("\n")
    # КЛЮЧЕВОЕ ОТЛИЧИЕ от v1: передаём repl как функцию (lambda), а не строку.
    # Так re.sub НЕ обрабатывает \ и \n внутри replacement_text как свои escape-коды.
    new_src = pattern.sub(lambda m: replacement_text, src, count=1)

    if "\t" in new_src:
        print("⚠ В файле были табы — заменяю их на 4 пробела везде.")
        new_src = new_src.replace("\t", "    ")

    backup = FNAME + ".bak2"
    with io.open(backup, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"Резервная копия сохранена как {backup}")

    with io.open(FNAME, "w", encoding="utf-8") as f:
        f.write(new_src)
    print(f"Готово: {FNAME} обновлён (v2, безопасный re.sub).")

if __name__ == "__main__":
    main()
