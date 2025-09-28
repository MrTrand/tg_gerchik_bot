# main.ru.py — бот-тестер на aiogram 3, поддерживает polling и webhook (aiohttp)
# Зависиomсти: aiogram>=3.22, aiohttp>=3.12
# Рядом должен лежать questions.json (мы уже сделали)

import asyncio, os, json
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- webhook / aiohttp ---
from aiohttp import web
from aiogram.types import Update

# ====== Конфигурация ======
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Задай его в конфигурации запуска.")

# Если WEBHOOK_URL задан — включаем режим вебхука
WEBHOOK_BASE = os.getenv("WEBHOOK_URL")       # напр. https://your-service.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # любая длинная строка (используется и в URL, и как header secret)
PORT = int(os.getenv("PORT", "8080"))         # Render передаёт свой порт
USE_WEBHOOK = bool(WEBHOOK_BASE)

# ====== Данные теста ======
HERE = Path(__file__).resolve().parent
QUEST_PATH = HERE / "questions.json"
if not QUEST_PATH.exists():
    raise FileNotFoundError(f"Не нашёл файл с вопросами: {QUEST_PATH}")

with QUEST_PATH.open("r", encoding="utf-8") as f:
    DATA = json.load(f)

QUESTIONS: list[dict] = DATA["questions"]
TYPES: dict = DATA["meta"]["types"]
TIE_THRESHOLD: int = DATA["meta"].get("scoring", {}).get("tie_threshold", 2)

# ====== Бот и маршрутизация ======
bot = Bot(TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

class Quiz(StatesGroup):
    active = State()

def build_kb(q: dict, selected: list[int]) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    qid = q["id"]
    for opt in q["options"]:
        opt_id = opt["id"]
        checked = "✅ " if opt_id in selected else "◻️ "
        kb.button(text=checked + opt["text"], callback_data=f"opt|{qid}|{opt_id}")
    kb.adjust(1)
    min_sel = q.get("min_select", 1)
    max_sel = q.get("max_select", 1)
    if max_sel != 1:
        kb.button(text="➡️ Дальше", callback_data=f"next|{qid}")
        kb.button(text="🔄 Сбросить выбор", callback_data=f"clear|{qid}")
        kb.adjust(1)
    return kb.as_markup()

async def ask_question(message_obj: types.Message, q_index: int, selected: list[int] | None = None):
    q = QUESTIONS[q_index]
    if selected is None:
        selected = []
    await message_obj.answer(q["text"], reply_markup=build_kb(q, selected))

@router.message(CommandStart())
async def on_start(msg: types.Message, state: FSMContext):
    codes = list(TYPES.keys())
    await state.set_state(Quiz.active)
    await state.update_data(idx=0, selected=[], scores={c: 0 for c in codes})
    await msg.answer("Привет! Это тест на тип трудовой мотивации. Жми на варианты ниже.")
    await ask_question(msg, 0, [])

@router.message(Command("reset"))
async def on_reset(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("Сбросил. Набери /start, чтобы пройти тест заново.")

@router.callback_query(Quiz.active, F.data.startswith("opt|"))
async def on_toggle(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = data["idx"]
    cur_q = QUESTIONS[idx]
    cur_qid = cur_q["id"]

    try:
        _, qid_str, opt_id_str = cb.data.split("|", 2)
        qid = int(qid_str)
        opt_id = int(opt_id_str)
    except Exception:
        await cb.answer("Непонятная кнопка.", show_alert=True)
        return

    if qid != cur_qid:
        msg = ("На данный вопрос вы уже ответили, переходите к ответу на следующий вопрос"
               if qid < cur_qid else "До этого вопроса мы ещё не дошли 🙂")
        await cb.answer(msg, show_alert=True)
        return

    max_sel = cur_q.get("max_select", 1)

    if max_sel == 1:
        await apply_awards(cur_q, [opt_id], state)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer("Ответ принят ✅")
        await goto_next(cb, state)
        return

    selected: list[int] = data.get("selected", [])
    if opt_id in selected:
        selected.remove(opt_id)
    else:
        if len(selected) >= max_sel:
            await cb.answer(f"Можно выбрать не больше {max_sel}.", show_alert=True)
            return
        selected.append(opt_id)

    await state.update_data(selected=selected)
    await cb.message.edit_reply_markup(reply_markup=build_kb(cur_q, selected))
    await cb.answer()

@router.callback_query(Quiz.active, F.data.startswith("clear|"))
async def on_clear(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = data["idx"]
    cur_q = QUESTIONS[idx]
    cur_qid = cur_q["id"]

    _, qid_str = cb.data.split("|", 1)
    if int(qid_str) != cur_qid:
        await cb.answer("На данный вопрос вы уже ответили, переходите к ответу на следующий вопрос", show_alert=True)
        return

    await state.update_data(selected=[])
    await cb.message.edit_reply_markup(reply_markup=build_kb(cur_q, []))
    await cb.answer("Выбор сброшен.")

@router.callback_query(Quiz.active, F.data.startswith("next|"))
async def on_next(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = data["idx"]
    q = QUESTIONS[idx]
    cur_qid = q["id"]

    _, qid_str = cb.data.split("|", 1)
    if int(qid_str) != cur_qid:
        await cb.answer("На данный вопрос вы уже ответили, переходите к ответу на следующий вопрос", show_alert=True)
        return

    selected: list[int] = data.get("selected", [])
    min_sel = q.get("min_select", 1)
    max_sel = q.get("max_select", 1)

    if not (min_sel <= len(selected) <= max_sel):
        await cb.answer(f"Нужно выбрать от {min_sel} до {max_sel}.", show_alert=True)
        return

    await apply_awards(q, selected, state)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("Ответы приняты ✅")
    await goto_next(cb, state)

# ====== Служебные ======

async def apply_awards(q: dict, chosen_ids: list[int], state: FSMContext):
    data = await state.get_data()
    scores: dict = data["scores"]
    by_id = {opt["id"]: opt for opt in q["options"]}

    for oid in chosen_ids:
        opt = by_id.get(oid)
        if not opt:
            continue
        for award in opt.get("awards", []):
            tcode = award["type"]
            pts = int(award.get("points", 1))
            scores[tcode] = scores.get(tcode, 0) + pts

    await state.update_data(scores=scores, selected=[])

def _get_advice_or_summary(code: str) -> str | None:
    info = TYPES.get(code, {})
    return info.get("advice") or info.get("summary")

async def goto_next(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = data["idx"] + 1
    await state.update_data(idx=idx, selected=[])

    if idx < len(QUESTIONS):
        await ask_question(cb.message, idx, [])
        return

    scores: dict = data["scores"]
    await state.clear()

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top1_code, top1_pts = ranked[0]
    top2_code, top2_pts = ranked[1] if len(ranked) > 1 else (None, None)

    lines: list[str] = []
    lines.append("🏁 Готово! Итоги:")
    for code, pts in ranked:
        name = TYPES.get(code, {}).get("name", code)
        lines.append(f"• {name} ({code}): {pts}")

    if top2_code and (top1_pts - top2_pts) <= TIE_THRESHOLD:
        name1 = TYPES.get(top1_code, {}).get("name", top1_code)
        name2 = TYPES.get(top2_code, {}).get("name", top2_code)
        lines.append(f"\n🔸 Ведущие типы: {name1} и {name2} (разница {top1_pts - top2_pts}).")
        adv1 = _get_advice_or_summary(top1_code)
        adv2 = _get_advice_or_summary(top2_code)
        if adv1:
            lines.append(f"\n{name1}: {adv1}")
        if adv2:
            lines.append(f"\n{name2}: {adv2}")
    else:
        name1 = TYPES.get(top1_code, {}).get("name", top1_code)
        lines.append(f"\n🔸 Ведущий тип: {name1}.")
        adv1 = _get_advice_or_summary(top1_code)
        if adv1:
            lines.append(f"\n{name1}: {adv1}")

    lines.append("\nКоманды: /start — заново, /reset — сброс.")
    await cb.message.answer("\n".join(lines))

# ====== Режимы запуска ======

async def run_polling():
    print("Polling-режим: бот запущен. Напишите /start в чате.")
    await dp.start_polling(bot)

async def run_webhook():
    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET не задан.")
    webhook_path = f"/webhook/{WEBHOOK_SECRET}"
    webhook_url = f"{WEBHOOK_BASE}{webhook_path}"

    async def handle_webhook(request: web.Request):
        # Проверка секрета и пути
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=401, text="unauthorized")
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
        return web.Response(status=200)

    async def health(_):
        return web.Response(text="ok")

    app = web.Application()
    app.add_routes([
        web.post(webhook_path, handle_webhook),
        web.get("/healthz", health),
    ])

    async def on_startup(_):
        # Ставим вебхук на нужный URL с секретом
        await bot.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
        print(f"Webhook set to {webhook_url}")

    async def on_shutdown(_):
        # Снимаем вебхук при остановке сервиса
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    print(f"Webhook-режим: слушаю порт {PORT}, путь {webhook_path}")
    await site.start()

    # «спим» пока процесс живёт
    while True:
        await asyncio.sleep(3600)

async def main():
    if USE_WEBHOOK:
        await run_webhook()
    else:
        await run_polling()

if __name__ == "__main__":
    asyncio.run(main())
