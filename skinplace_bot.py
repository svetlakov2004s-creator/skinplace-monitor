import asyncio
import json
import logging
import os
import re
import time
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import Message
from playwright.async_api import async_playwright, Response

# Токен берется автоматически из GitHub Secrets
BOT_TOKEN = os.getenv("BOT_TOKEN")

CHECK_INTERVAL = 25          # Пауза между кругами проверки (в секундах)
MAX_RUN_SECONDS = 5.5 * 3600 # Работаем 5.5 часов до планового перезапуска
STATE_FILE = "state.json"

# Стартовый список по умолчанию
DEFAULT_TARGETS = [
    {"name": "Glock-18 | Vogue", "slug": "glock-18-vogue"},
    {"name": "Glock-18 | Shinobu", "slug": "glock-18-shinobu"},
    {"name": "AWP | Duality", "slug": "awp-duality"},
    {"name": "AWP | Exothermic", "slug": "awp-exothermic"},
    {"name": "AK-47 | Aphrodite", "slug": "ak-47-aphrodite"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

seen_ids = set()
active_chat_id = None
targets = []
start_time = time.time()


def slug_to_pretty_name(slug: str) -> str:
    """Превращает 'glock-18-vogue' в 'Glock-18 | Vogue'."""
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


def build_urls(slug: str) -> list[str]:
    """Генерирует ссылки сразу на оба качества: MW и FT."""
    return [
        f"https://skin.place/ru/buy-cs2-skins/{slug}?exterior=Minimal+Wear",
        f"https://skin.place/ru/buy-cs2-skins/{slug}?exterior=Field-Tested",
    ]


def load_state():
    global active_chat_id, seen_ids, targets
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                active_chat_id = data.get("chat_id")
                seen_ids = set(data.get("seen_ids", []))
                targets = data.get("targets", DEFAULT_TARGETS)
                logging.info(f"База загружена: скинов в списке={len(targets)}, просмотрено лотов={len(seen_ids)}")
                return
        except Exception as e:
            logging.warning(f"Ошибка чтения state.json: {e}")
    targets = list(DEFAULT_TARGETS)


def save_state():
    try:
        recent_ids = list(seen_ids)[-2000:]
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


# ================= КОМАНДЫ TELEGRAM =================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    global active_chat_id
    active_chat_id = message.chat.id
    save_state()

    skins_text = "\n".join([f"{i+1}. {t['name']}" for i, t in enumerate(targets)]) or "Список пуст"
    await message.answer(
        "✅ <b>Бот активен и мониторит скины (включая StatTrak™)!</b>\n\n"
        f"🔍 <b>Текущий список ({len(targets)}):</b>\n{skins_text}\n\n"
        "🎯 <b>Фильтр Float:</b>\n"
        "• Minimal Wear: <code>0.07 – 0.09</code>\n"
        "• Field-Tested: <code>0.15 – 0.19</code>\n\n"
        "⚙️ <b>Управление списком:</b>\n"
        "• <code>/list</code> — показать список скинов\n"
        "• <code>/add ссылка</code> — добавить новый скин\n"
        "• <code>/del номер</code> — удалить скин по номеру",
        parse_mode="HTML",
    )


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if not targets:
        await message.answer("📭 Список отслеживания пуст. Добавьте скин командой:\n<code>/add ссылка_на_скин</code>", parse_mode="HTML")
        return

    lines = [f"<b>{i+1}.</b> {t['name']} (<code>{t['slug']}</code>)" for i, t in enumerate(targets)]
    await message.answer(
        "📋 <b>Отслеживаемые скины:</b>\n\n" + "\n".join(lines) +
        "\n\nЧтобы удалить скин, отправьте <code>/del номер</code> (например: <code>/del 2</code>)",
        parse_mode="HTML"
    )


@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(
            "⚠️ Укажите ссылку после команды!\n"
            "Пример:\n<code>/add https://skin.place/ru/buy-cs2-skins/ak-47-redline?exterior=Field-Tested</code>",
            parse_mode="HTML"
        )
        return

    raw_input = command.args.strip()
    match = re.search(r"buy-cs2-skins/([a-zA-Z0-9\-]+)", raw_input)
    slug = match.group(1).lower() if match else raw_input.lower()

    if not re.match(r"^[a-z0-9\-]+$", slug):
        await message.answer("❌ Не удалось распознать ссылку со SkinPlace. Пришлите обычную ссылку на страницу скина.")
        return

    if any(t["slug"] == slug for t in targets):
        await message.answer(f"ℹ️ Скин <code>{slug}</code> уже есть в списке отслеживания!", parse_mode="HTML")
        return

    pretty_name = slug_to_pretty_name(slug)
    targets.append({"name": pretty_name, "slug": slug})
    save_state()

    await message.answer(
        f"✅ <b>Скин добавлен в мониторинг!</b>\n\n"
        f"🔫 <b>Название:</b> {pretty_name}\n"
        f"🔗 <b>ID в каталоге:</b> <code>{slug}</code>\n"
        f"📊 Проверяются обычные и <b>StatTrak™</b> версии в качествах <b>MW (≤ 0.09)</b> и <b>FT (≤ 0.19)</b>.",
        parse_mode="HTML"
    )


@dp.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        await message.answer(
            "⚠️ Укажите номер скина из списка <code>/list</code>!\n"
            "Пример: <code>/del 1</code>",
            parse_mode="HTML"
        )
        return

    idx = int(command.args.strip()) - 1
    if idx < 0 or idx >= len(targets):
        await message.answer(f"❌ Неверный номер. В списке сейчас {len(targets)} скинов (посмотрите через /list).")
        return

    removed = targets.pop(idx)
    save_state()
    await message.answer(f"🗑 Скин <b>{removed['name']}</b> удален из мониторинга!", parse_mode="HTML")


# ================= ЛОГИКА ПАРСИНГА И АВТОСКРОЛЛА =================

def is_good_float(float_val: float) -> tuple[bool, str]:
    if 0.07 <= float_val <= 0.09:
        return True, "Minimal Wear (MW ≤ 0.09)"
    if 0.15 <= float_val <= 0.19:
        return True, "Field-Tested (FT ≤ 0.19)"
    return False, ""


async def auto_scroll(page):
    """Плавная прокрутка страницы вниз для подгрузки всех лотов (включая StatTrak внизу)."""
    try:
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 900)")
            await asyncio.sleep(0.8)
    except Exception:
        pass


def detect_stattrak_in_dict(d: dict) -> bool:
    """Определяет наличие StatTrak по полям JSON-объекта."""
    if d.get("is_stattrak") or d.get("stattrak") or d.get("isStattrak") or d.get("stat_trak"):
        return True
    name_str = str(d.get("market_hash_name") or d.get("name") or d.get("full_name") or "")
    if "stattrak" in name_str.lower() or re.search(r"\bST\b", name_str):
        return True
    return False


def extract_items_from_json(data, found_list=None) -> list[dict]:
    if found_list is None:
        found_list = []

    if isinstance(data, dict):
        fl = data.get("float") or data.get("paintwear") or data.get("float_value") or data.get("wear")
        pr = data.get("price") or data.get("cost") or data.get("amount")
        item_id = data.get("id") or data.get("item_id") or data.get("asset_id")

        if fl is not None and pr is not None and item_id is not None:
            try:
                float_num = float(fl)
                if 0.0 < float_num < 1.0:
                    found_list.append({
                        "id": str(item_id),
                        "float": float_num,
                        "price": pr,
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


async def parse_dom_fallback(page) -> list[dict]:
    items = []
    links = await page.query_selector_all("a[href*='id=']")
    for link in links:
        href = await link.get_attribute("href")
        text = await link.inner_text()
        if not href or not text:
            continue

        id_match = re.search(r"id=(\d+)", href)
        float_match = re.search(r"\b(0\.\d{3,14})\b", text)
        price_match = re.search(r"([\$€₽]\s?\d+[.,]?\d*|\d+[.,]?\d*\s?[\$€₽])", text)
        pattern_match = re.search(r"pattern=(\d+)", href)

        # Проверяем наличие плашки "ST |" или надписи "StatTrak" прямо в карточке скина
        is_st = bool(re.search(r"\bST\s*\||StatTrak|\bST\s+(MW|FT|FN|WW|BS)\b", text, re.IGNORECASE))

        if id_match and float_match:
            items.append({
                "id": id_match.group(1),
                "float": float(float_match.group(1)),
                "price": price_match.group(0).strip() if price_match else "См. на сайте",
                "pattern": pattern_match.group(1) if pattern_match else "—",
                "is_stattrak": is_st,
            })
    return items


async def send_telegram_alert(skin_name: str, slug: str, item: dict, wear_label: str):
    if not active_chat_id:
        return

    exterior_param = "Minimal+Wear" if "MW" in wear_label else "Field-Tested"
    direct_url = (
        f"https://skin.place/ru/buy-cs2-skins/{slug}"
        f"?id={item['id']}&pattern={item['pattern']}&exterior={exterior_param}"
    )

    st_badge = " 🟠 <b>[StatTrak™]</b>" if item.get("is_stattrak") else ""

    msg = (
        f"🎯 <b>Найден новый скин с нужным Float!</b>\n\n"
        f"🔫 <b>{skin_name}</b>{st_badge}\n"
        f"✨ <b>Качество:</b> {wear_label}\n"
        f"📊 <b>Float:</b> <code>{item['float']}</code>\n"
        f"🎨 <b>Pattern:</b> <code>{item['pattern']}</code>\n"
        f"💰 <b>Цена:</b> {item['price']}\n\n"
        f"🔗 <a href='{direct_url}'>Открыть лот на SkinPlace</a>"
    )
    try:
        await bot.send_message(chat_id=active_chat_id, text=msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram: {e}")


async def monitor_skinplace():
    load_state()
    is_cold_start = len(seen_ids) == 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )
        page = await context.new_page()
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
                    for url in build_urls(target["slug"]):
                        intercepted_items.clear()
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                            await asyncio.sleep(2.5)
                            await page.screenshot(path="debug_page.png")
                            
                            # Прокручиваем список вниз, чтобы подгрузились все StatTrak лоты
                            await auto_scroll(page)
                            await asyncio.sleep(1.5)

                            dom_items = await parse_dom_fallback(page)
                            
                            # Объединяем данные из JSON и HTML (если в одном из них нашелся флаг StatTrak — сохраняем его)
                            merged_items = {}
                            for it in (intercepted_items + dom_items):
                                iid = it["id"]
                                if iid not in merged_items:
                                    merged_items[iid] = it
                                else:
                                    if it.get("is_stattrak"):
                                        merged_items[iid]["is_stattrak"] = True

                            for item in merged_items.values():
                                item_id = item["id"]
                                item_float = item["float"]

                                ok, wear_label = is_good_float(item_float)
                                if not ok:
                                    continue

                                if item_id not in seen_ids:
                                    seen_ids.add(item_id)
                                    if not is_cold_start:
                                        await send_telegram_alert(
                                            target["name"], target["slug"], item, wear_label
                                        )

                        except Exception as e:
                            logging.warning(f"Ошибка при проверке {url}: {e}")

                if is_cold_start:
                    logging.info(f"Первичная база собрана ({len(seen_ids)} скинов). Мониторим новинки!")
                    is_cold_start = False

                save_state()
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
