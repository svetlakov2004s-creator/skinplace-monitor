import asyncio
import json
import logging
import os
import random
import re
import time
from urllib.parse import urlparse, parse_qs, urlunparse
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import Message, FSInputFile
from playwright.async_api import async_playwright, Response

BOT_TOKEN = os.getenv("BOT_TOKEN")

# === НАСТРОЙКИ ТАЙМИНГОВ И АНТИ-БАНА ===
DELAY_BETWEEN_PAGES = (30, 45)  # Случайная пауза между открытием страниц (30-45 сек)
CHECK_INTERVAL = 120            # Пауза после прохода всего списка (2 минуты)
RATE_LIMIT_COOLDOWN = 180       # Пауза при плашке "Слишком много попыток" (3 минуты)

MAX_RUN_SECONDS = 5.5 * 3600
STATE_FILE = "state.json"

DEFAULT_TARGETS = [
    {"name": "Glock-18 | Vogue", "slug": "glock-18-vogue", "url": "https://skin.place/ru/buy-cs2-skins/glock-18-vogue"},
    {"name": "Glock-18 | Shinobu", "slug": "glock-18-shinobu", "url": "https://skin.place/ru/buy-cs2-skins/glock-18-shinobu"},
    {"name": "AWP | Duality", "slug": "awp-duality", "url": "https://skin.place/ru/buy-cs2-skins/awp-duality"},
    {"name": "AWP | Exothermic", "slug": "awp-exothermic", "url": "https://skin.place/ru/buy-cs2-skins/awp-exothermic"},
    {"name": "AK-47 | Aphrodite", "slug": "ak-47-aphrodite", "url": "https://skin.place/ru/buy-cs2-skins/ak-47-aphrodite"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

seen_ids = set()
active_chat_id = None
targets = []
start_time = time.time()
current_page = None


def slug_to_pretty_name(slug: str) -> str:
    weapons = {
        "ak-47": "AK-47", "glock-18": "Glock-18", "awp": "AWP",
        "m4a1-s": "M4A1-S", "m4a4": "M4A4", "usp-s": "USP-S",
        "desert-eagle": "Desert Eagle", "mp9": "MP9", "mac-10": "MAC-10",
        "famas": "FAMAS", "galil-ar": "Galil AR", "p250": "P250",
        "five-seven": "Five-SeveN", "tec-9": "Tec-9", "ssg-08": "SSG 08"
    }
    for prefix, weapon_title in weapons.items():
        if slug.startswith(prefix + "-"):
            skin_part = slug[len(prefix) + 1:].replace("-", " ").title()
            return f"{weapon_title} | {skin_part}"
    return slug.replace("-", " ").title()


def replace_exterior_in_url(raw_url: str, exterior: str) -> str:
    parsed = urlparse(raw_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["exterior"] = [exterior]
    pairs = []
    for k, vals in qs.items():
        val_str = str(vals[0]).replace(" ", "+")
        pairs.append(f"{k}={val_str}")
    new_query = "&".join(pairs)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def build_urls_for_target(target: dict) -> list[str]:
    slug = target["slug"]
    base_url = target.get("url") or f"https://skin.place/ru/buy-cs2-skins/{slug}"

    if "id=" in base_url:
        return [
            replace_exterior_in_url(base_url, "Minimal Wear"),
            replace_exterior_in_url(base_url, "Field-Tested"),
        ]
    return [f"https://skin.place/ru/buy-cs2-skins/{slug}"]


def load_state():
    global active_chat_id, seen_ids, targets
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                active_chat_id = data.get("chat_id")
                seen_ids = set(data.get("seen_ids", []))
                targets = data.get("targets", DEFAULT_TARGETS)
                logging.info(f"База загружена: Chat ID={active_chat_id}, скинов={len(targets)}")
                return
        except Exception as e:
            logging.warning(f"Ошибка чтения state.json: {e}")
    targets = list(DEFAULT_TARGETS)


def save_state():
    try:
        recent_ids = list(seen_ids)[-3000:]
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "chat_id": active_chat_id,
                    "seen_ids": recent_ids,
                    "targets": targets,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        logging.error(f"Ошибка сохранения state.json: {e}")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    global active_chat_id
    active_chat_id = message.chat.id
    save_state()

    skins_text = "\n".join([f"{i+1}. {t['name']}" for i, t in enumerate(targets)]) or "Список пуст"
    await message.answer(
        "✅ <b>Бот активен (Глубокий скролл + точные цены)!</b>\n\n"
        f"⏱ <b>Пауза между страницами:</b> {DELAY_BETWEEN_PAGES[0]}–{DELAY_BETWEEN_PAGES[1]} сек.\n"
        f"🔍 <b>Скинов в отслеживании ({len(targets)}):</b>\n{skins_text}\n\n"
        "🎯 <b>Фильтры Float:</b> MW (0.07–0.09) и FT (0.15–0.19)\n\n"
        "⚙️ <b>Команды:</b>\n"
        "• <code>/screenshot</code> — фото экрана браузера\n"
        "• <code>/list</code> — список скинов\n"
        "• <code>/add полная_ссылка</code> — добавить или обновить скин\n"
        "• <code>/del номер</code> — удалить скин",
        parse_mode="HTML",
    )


@dp.message(Command("screenshot"))
async def cmd_screenshot(message: Message):
    global current_page
    if not current_page:
        await message.answer("⏳ Браузер еще запускается. Подождите 10 секунд.")
        return

    msg = await message.answer("📸 Делаю скриншот...")
    try:
        img_path = "live_view.png"
        await current_page.screenshot(path=img_path)
        current_url = current_page.url
        await message.answer_photo(
            photo=FSInputFile(img_path),
            caption=f"🌐 <b>Текущий экран:</b>\n<code>{current_url}</code>",
            parse_mode="HTML"
        )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if not targets:
        await message.answer("📭 Список пуст.", parse_mode="HTML")
        return

    lines = []
    for i, t in enumerate(targets):
        has_id = "✅ полная ссылка" if "id=" in t.get("url", "") else "⚠️ без ID"
        lines.append(f"<b>{i+1}.</b> {t['name']} ({has_id})")
    await message.answer("📋 <b>Отслеживаемые скины:</b>\n\n" + "\n".join(lines), parse_mode="HTML")


@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("⚠️ Пришлите полную ссылку со страницы скина:\n<code>/add https://skin.place/ru/buy-cs2-skins/...</code>", parse_mode="HTML")
        return

    raw_input = command.args.strip()
    match = re.search(r"buy-cs2-skins/([a-zA-Z0-9\-]+)", raw_input)
    slug = match.group(1).lower() if match else raw_input.lower()

    if not re.match(r"^[a-z0-9\-]+$", slug):
        await message.answer("❌ Неверный формат ссылки.")
        return

    full_url = raw_input if raw_input.startswith("http") else f"https://skin.place/ru/buy-cs2-skins/{slug}"
    pretty_name = slug_to_pretty_name(slug)

    for t in targets:
        if t["slug"] == slug:
            t["url"] = full_url
            save_state()
            await message.answer(f"🔄 Ссылка для <b>{pretty_name}</b> обновлена!\n<code>{full_url}</code>", parse_mode="HTML")
            return

    targets.append({"name": pretty_name, "slug": slug, "url": full_url})
    save_state()
    await message.answer(f"✅ Добавлен: <b>{pretty_name}</b>\n🔗 <code>{full_url}</code>", parse_mode="HTML")


@dp.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        await message.answer("⚠️ Укажите номер: <code>/del 1</code>", parse_mode="HTML")
        return

    idx = int(command.args.strip()) - 1
    if idx < 0 or idx >= len(targets):
        await message.answer(f"❌ Неверный номер. Всего скинов: {len(targets)}")
        return

    removed = targets.pop(idx)
    save_state()
    await message.answer(f"🗑 Удален: <b>{removed['name']}</b>", parse_mode="HTML")


def is_good_float(float_val: float) -> tuple[bool, str]:
    if 0.07 <= float_val <= 0.09:
        return True, "Minimal Wear (MW ≤ 0.09)"
    if 0.15 <= float_val <= 0.19:
        return True, "Field-Tested (FT ≤ 0.19)"
    return False, ""


async def accept_cookies_if_present(page):
    try:
        btn = await page.query_selector("#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll, button:has-text('Allow all')")
        if btn:
            await btn.click()
            await asyncio.sleep(1.0)
    except Exception:
        pass


async def is_rate_limited(page) -> bool:
    try:
        body_text = await page.inner_text("body")
        if "Слишком много попыток" in body_text or "Too Many Requests" in body_text or "Too many attempts" in body_text:
            return True
    except Exception:
        pass
    return False


async def auto_scroll(page, max_scrolls=10):
    """
    Адаптивный глубокий скролл: прокручивает каталог вниз шагами по 1200 пикселей
    для подгрузки 100-200 скинов. Если список закончился раньше — останавливается сам.
    """
    try:
        previous_height = await page.evaluate("document.body.scrollHeight")
        for _ in range(max_scrolls):
            await page.evaluate("window.scrollBy(0, 1200)")
            await asyncio.sleep(1.2)

            current_height = await page.evaluate("document.body.scrollHeight")
            if current_height == previous_height:
                # Достигнут конец списка карточек
                break
            previous_height = current_height

        # Возвращаем экран к началу каталога
        await page.evaluate("window.scrollTo(0, 150)")
    except Exception:
        pass


def detect_stattrak_in_dict(d: dict) -> bool:
    if d.get("is_stattrak") or d.get("stattrak") or d.get("isStattrak") or d.get("stat_trak"):
        return True
    name_str = str(d.get("market_hash_name") or d.get("name") or d.get("full_name") or "")
    return bool("stattrak" in name_str.lower() or re.search(r"\bST\b", name_str))


def format_json_price(pr) -> str:
    """Форматирует цену из JSON, добавляя знак $, если пришло просто число."""
    if pr is None:
        return "См. на сайте"
    pr_str = str(pr).strip()
    if not pr_str:
        return "См. на сайте"
    if any(sym in pr_str for sym in ("$", "€", "₽", "USD", "EUR", "RUB")):
        return pr_str
    return f"$ {pr_str}"


def extract_items_from_json(data, found_list=None) -> list[dict]:
    if found_list is None:
        found_list = []

    if isinstance(data, dict):
        fl = data.get("float") or data.get("paintwear") or data.get("float_value") or data.get("wear")
        pr = data.get("price") or data.get("cost") or data.get("amount") or data.get("price_usd")
        item_id = data.get("id") or data.get("item_id") or data.get("asset_id") or data.get("skin_id")

        if fl is not None and item_id is not None:
            try:
                float_num = float(fl)
                if 0.0 < float_num < 1.0:
                    found_list.append({
                        "id": str(item_id),
                        "float": float_num,
                        "price": format_json_price(pr),
                        "pattern": data.get("pattern") or data.get("paintseed") or "—",
                        "is_stattrak": detect_stattrak_in_dict(data),
                    })
            except (ValueError, TypeError):
                pass

        for value in data.values():
            extract_items_from_json(value, found_list)

    elif isinstance(data, list):
        for elem in data:
            extract_items_from_json(elem, found_list)

    return found_list


PRICE_REGEX = re.compile(
    r"([\$€₽][ \t]*\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?[ \t]*(?:[\$€₽]|USD|EUR|RUB))",
    re.IGNORECASE
)


async def parse_dom_fallback(page) -> list[dict]:
    items = []
    links = await page.query_selector_all("a[href*='id=']")
    for link in links:
        href = await link.get_attribute("href") or ""
        text = await link.inner_text() or ""
        id_match = re.search(r"id=(\d+)", href)
        float_match = re.search(r"\b(0\.\d{3,14})\b", text)
        pattern_match = re.search(r"pattern=(\d+)", href)
        is_st = bool(re.search(r"\bST\s*\||StatTrak|\bST\s+(MW|FT|FN|WW|BS)\b", text, re.IGNORECASE))

        if id_match and float_match:
            fl_str = float_match.group(1)
            # Удаляем float из текста, чтобы его цифры не путались с ценой
            clean_text = text.replace(fl_str, "")
            price_match = PRICE_REGEX.search(clean_text)

            # Если внутри <a> цены нет, проверяем родительский контейнер карточки
            if not price_match:
                try:
                    parent_text = await link.evaluate(
                        "el => (el.closest('div, li, tr') || el.parentElement)?.innerText || ''"
                    )
                    parent_clean = parent_text.replace(fl_str, "")
                    price_match = PRICE_REGEX.search(parent_clean)
                except Exception:
                    pass

            items.append({
                "id": id_match.group(1),
                "float": float(fl_str),
                "price": price_match.group(0).strip() if price_match else "См. на сайте",
                "pattern": pattern_match.group(1) if pattern_match else "—",
                "is_stattrak": is_st,
            })

    if not items:
        raw_cards = await page.evaluate("""() => {
            const results = [];
            const elements = document.querySelectorAll('div, tr, li');
            for (const el of elements) {
                const txt = el.innerText || '';
                if (txt.length > 10 && txt.length < 300 && /\\b(FN|MW|FT|WW|BS)\\b/.test(txt) && /\\b0\\.\\d{4,10}\\b/.test(txt)) {
                    results.push(txt);
                }
            }
            return results;
        }""")
        seen_floats = set()
        for txt in raw_cards:
            fl_m = re.search(r"\b(0\.\d{4,10})\b", txt)
            if not fl_m:
                continue
            fl_str = fl_m.group(1)
            if fl_str in seen_floats:
                continue
            seen_floats.add(fl_str)

            clean_txt = txt.replace(fl_str, "")
            pr_m = PRICE_REGEX.search(clean_txt)
            is_st = bool(re.search(r"\bST\s*\||StatTrak", txt, re.IGNORECASE))

            items.append({
                "id": f"float_{fl_str}",
                "float": float(fl_str),
                "price": pr_m.group(0).strip() if pr_m else "См. на сайте",
                "pattern": "—",
                "is_stattrak": is_st,
            })

    return items


async def send_telegram_alert(skin_name: str, target_url: str, item: dict, wear_label: str):
    if not active_chat_id:
        return

    st_badge = " 🟠 <b>[StatTrak™]</b>" if item.get("is_stattrak") else ""

    msg = (
        f"🎯 <b>Найден новый скин с нужным Float!</b>\n\n"
        f"🔫 <b>{skin_name}</b>{st_badge}\n"
        f"✨ <b>Качество:</b> {wear_label}\n"
        f"📊 <b>Float:</b> <code>{item['float']}</code>\n"
        f"🎨 <b>Pattern:</b> <code>{item['pattern']}</code>\n"
        f"💰 <b>Цена:</b> {item['price']}\n\n"
        f"🔗 <a href='{target_url}'>Открыть на SkinPlace</a>"
    )
    try:
        await bot.send_message(chat_id=active_chat_id, text=msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram: {e}")


async def monitor_skinplace():
    global current_page
    load_state()
    is_cold_start = len(seen_ids) == 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )
        page = await context.new_page()
        current_page = page
        intercepted_items = []

        async def handle_response(response: Response):
            if "json" in response.headers.get("content-type", "").lower():
                try:
                    data = await response.json()
                    extracted = extract_items_from_json(data)
                    if extracted:
                        intercepted_items.extend(extracted)
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            while time.time() - start_time < MAX_RUN_SECONDS:
                current_targets = list(targets)

                for target in current_targets:
                    for url in build_urls_for_target(target):
                        intercepted_items.clear()
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                            await asyncio.sleep(5.0)
                            await accept_cookies_if_present(page)

                            if await is_rate_limited(page):
                                logging.warning(f"Лимит запросов! Ждем {RATE_LIMIT_COOLDOWN} сек...")
                                await asyncio.sleep(RATE_LIMIT_COOLDOWN)
                                await page.reload(wait_until="domcontentloaded", timeout=35000)
                                await asyncio.sleep(5.0)

                            if "id=" in page.url and "id=" not in target.get("url", ""):
                                target["url"] = page.url

                            await auto_scroll(page, max_scrolls=10)
                            await asyncio.sleep(2.0)

                            dom_items = await parse_dom_fallback(page)

                            merged_items = {}
                            for it in (intercepted_items + dom_items):
                                iid = it["id"]
                                if iid not in merged_items:
                                    merged_items[iid] = it
                                else:
                                    if it.get("is_stattrak"):
                                        merged_items[iid]["is_stattrak"] = True
                                    # Если в одной из версий (JSON или DOM) нашлась точная цена, сохраняем её
                                    if merged_items[iid].get("price") == "См. на сайте" and it.get("price") != "См. на сайте":
                                        merged_items[iid]["price"] = it["price"]

                            logging.info(f"Проверен {target['name']} -> найдено лотов: {len(merged_items)}")

                            for item in merged_items.values():
                                item_id = f"{target['slug']}_{item['id']}"
                                item_float = item["float"]

                                ok, wear_label = is_good_float(item_float)
                                if not ok:
                                    continue

                                if item_id not in seen_ids:
                                    seen_ids.add(item_id)
                                    if not is_cold_start:
                                        await send_telegram_alert(
                                            target["name"], url, item, wear_label
                                        )

                            pause = random.uniform(*DELAY_BETWEEN_PAGES)
                            logging.info(f"Пауза {pause:.1f} сек. до следующей страницы...")
                            await asyncio.sleep(pause)

                        except Exception as e:
                            logging.warning(f"Ошибка при проверке {url}: {e}")
                            await asyncio.sleep(20)

                if is_cold_start:
                    logging.info(f"Первичная база собрана (подходящих лотов: {len(seen_ids)}). Мониторим новинки!")
                    is_cold_start = False

                save_state()
                logging.info(f"Круг завершен. Отдых {CHECK_INTERVAL} сек...")
                await asyncio.sleep(CHECK_INTERVAL)

        finally:
            save_state()
            await browser.close()
            await dp.stop_polling()


async def main():
    await asyncio.gather(
        dp.start_polling(bot, handle_signals=False),
        monitor_skinplace(),
        return_exceptions=True
    )


if __name__ == "__main__":
    asyncio.run(main())
