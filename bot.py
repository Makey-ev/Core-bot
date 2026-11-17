import asyncio
import os
import random
import re
import sqlite3
import string
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, ReplyKeyboardMarkup,
                           KeyboardButton)
from texts import TEXTS

TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН")

MAX_PROFILES = 3
FREE_MEDS = 3
DIGEST_HOUR = 21
SNOOZE_MIN = 15
ESC_REPEAT_MIN = 30
ESC_ALERT_MIN = 30
MISSED_AFTER_H = 12

db = sqlite3.connect("care.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    uid INTEGER PRIMARY KEY, lang TEXT, tz INTEGER DEFAULT 3,
    step TEXT DEFAULT 'menu');
CREATE TABLE IF NOT EXISTS patients(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner INTEGER, name TEXT, user_uid INTEGER,
    code TEXT UNIQUE, last_digest TEXT);
CREATE TABLE IF NOT EXISTS meds(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER, name TEXT, times TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS doses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    med_id INTEGER, patient_id INTEGER, day TEXT, stime TEXT, due TEXT,
    status TEXT DEFAULT 'pending', stage INTEGER DEFAULT 0,
    last_sent TEXT, snooze_until TEXT, snoozes INTEGER DEFAULT 0,
    UNIQUE(med_id, day, stime));
""")
db.commit()

LANG_NAMES = {"ru": "Русский", "en": "English", "uk": "Українська",
              "it": "Italiano", "es": "Español", "de": "Deutsch"}

def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def ensure_user(uid, lang_code=None):
    if db.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone() is None:
        lang = (lang_code or "en")[:2].lower()
        db.execute("INSERT INTO users(uid, lang) VALUES(?,?)",
                   (uid, lang if lang in TEXTS else "en"))
        db.commit()

def t(uid, key, **kw):
    row = db.execute("SELECT lang FROM users WHERE uid=?", (uid,)).fetchone()
    lang = row["lang"] if row else "en"
    s = TEXTS.get(lang, TEXTS["en"]).get(key) or TEXTS["en"][key]
    return s.format(**kw) if kw else s

def tz_of(uid):
    row = db.execute("SELECT tz FROM users WHERE uid=?", (uid,)).fetchone()
    return row["tz"] if row else 3

def gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if db.execute("SELECT 1 FROM patients WHERE code=?", (code,)).fetchone() is None:
            return code

def main_menu(uid):
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=t(uid, "menu_add_med")),
         KeyboardButton(text=t(uid, "menu_list"))],
        [KeyboardButton(text=t(uid, "menu_stats")),
         KeyboardButton(text=t(uid, "menu_family"))],
        [KeyboardButton(text=t(uid, "menu_lang"))],
    ], resize_keyboard=True)
    return kb

def dose_kb(uid, did):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(uid, "btn_took"), callback_data=f"take:{did}"),
        InlineKeyboardButton(text=t(uid, "btn_snooze"), callback_data=f"snooze:{did}")]])

async def send_reminder(bot, uid, did, medname, stime, repeat=False):
    try:
        if repeat:
            await bot.send_message(uid, t(uid, "esc_patient", med=medname),
                                   reply_markup=dose_kb(uid, did))
        else:
            await bot.send_message(uid, t(uid, "reminder", med=medname, time=stime),
                                   reply_markup=dose_kb(uid, did))
    except Exception as e:
        print(f"send to {uid}: {e}")

dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    await m.answer(t(m.from_user.id, "welcome"), reply_markup=main_menu(m.from_user.id))

@dp.message(F.text == "🏠 Меню")
async def menu_back(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    await m.answer(t(m.from_user.id, "menu_text"), reply_markup=main_menu(m.from_user.id))

@dp.message(F.text.in_(["➕ Лекарство", "➕ Medication", "➕ Ліки", "➕ Farmaco", "➕ Medicamento", "➕ Medikament"]))
async def add_med_start(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    db.execute("UPDATE users SET step='med_name' WHERE uid=?", (uid,))
    db.commit()
    await m.answer(t(uid, "med_ask_name"), reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🏠 Меню")]], resize_keyboard=True))

@dp.message(F.text.in_(["📋 Список", "📋 List", "📋 Список", "📋 Elenco", "📋 Lista", "📋 Liste"]))
async def show_list(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    ps = db.execute("SELECT * FROM patients WHERE owner=? OR user_uid=?", (uid, uid)).fetchall()
    if not ps:
        await m.answer(t(uid, "list_empty"), reply_markup=main_menu(uid))
        return
    lines = [t(uid, "list_head")]
    for p in ps:
        ptz = tz_of(p["user_uid"] or p["owner"])
        today = (now_utc() + timedelta(hours=ptz)).date().isoformat()
        linked = "✅" if p["user_uid"] else f"⏳ {p['code']}"
        lines.append(f"\n👤 {p['name']} {linked}")
        for med in db.execute("SELECT * FROM meds WHERE patient_id=?", (p["id"],)).fetchall():
            c = db.execute("SELECT COUNT(*) c, SUM(status='taken') k FROM doses "
                           "WHERE med_id=? AND day=?", (med["id"], today)).fetchone()
            lines.append(f"  💊 {med['name']} — {med['times']} ({c['k'] or 0}/{c['c']})")
    await m.answer("\n".join(lines), reply_markup=main_menu(uid))

@dp.message(F.text.in_(["📊 Статистика", "📊 Stats", "📊 Статистика", "📊 Statistiche", "📊 Estadísticas", "📊 Statistik"]))
async def show_stats(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    ps = db.execute("SELECT * FROM patients WHERE owner=? OR user_uid=?", (uid, uid)).fetchall()
    if not ps:
        await m.answer(t(uid, "list_empty"), reply_markup=main_menu(uid))
        return
    lines = [t(uid, "stats_head")]
    for p in ps:
        ptz = tz_of(p["user_uid"] or p["owner"])
        d0 = (now_utc() + timedelta(hours=ptz)).date()
        week = (d0 - timedelta(days=6)).isoformat()
        r = db.execute("SELECT COUNT(*) c, SUM(status='taken') k FROM doses "
                       "WHERE patient_id=? AND day>=?", (p["id"], week)).fetchone()
        pct = round(100 * (r["k"] or 0) / r["c"]) if r["c"] else 100
        lines.append(t(uid, "stats_line", name=p["name"], pct=pct,
                       taken=r["k"] or 0, total=r["c"]))
    await m.answer("\n".join(lines), reply_markup=main_menu(uid))

@dp.message(F.text.in_(["👨‍👩‍👧 Семья", "👨‍👩‍👧 Family", "👨‍👩‍👧 Сім'я", "👨‍👩‍👧 Famiglia", "👨‍👩‍👧 Familia", "👨‍👩‍👧 Familie"]))
async def family_menu(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    ps = db.execute("SELECT * FROM patients WHERE owner=? AND user_uid IS NULL", (uid,)).fetchall()
    if not ps:
        await m.answer(t(uid, "family_none"), reply_markup=main_menu(uid))
        return
    lines = [t(uid, "family_codes")]
    for p in ps:
        lines.append(f"👤 {p['name']}: {p['code']}")
    lines.append("\n" + t(uid, "family_hint"))
    await m.answer("\n".join(lines), reply_markup=main_menu(uid))

@dp.message(F.text.in_(["🌐 Язык", "🌐 Language", "🌐 Мова", "🌐 Lingua", "🌐 Idioma", "🌐 Sprache"]))
async def lang_menu(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"lang:{code}")]
        for code, name in LANG_NAMES.items()])
    await m.answer(t(uid, "lang_choose"), reply_markup=kb)

@dp.callback_query(F.data.startswith("lang:"))
async def cb_lang(cb: CallbackQuery):
    ensure_user(cb.from_user.id, cb.from_user.language_code)
    code = cb.data.split(":")[1]
    if code in TEXTS:
        db.execute("UPDATE users SET lang=? WHERE uid=?", (code, cb.from_user.id))
        db.commit()
    await cb.answer("OK")
    await cb.message.edit_text(t(cb.from_user.id, "lang_set"))
    await cb.message.answer(t(cb.from_user.id, "menu_text"), reply_markup=main_menu(cb.from_user.id))

# Пошаговое добавление лекарства
@dp.message(F.text)
async def step_handler(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    row = db.execute("SELECT step FROM users WHERE uid=?", (uid,)).fetchone()
    if not row or row["step"] != "med_name":
        return
    
    name = m.text.strip()
    if not name or len(name) > 50:
        await m.answer(t(uid, "med_bad_name"))
        return
    
    db.execute("UPDATE users SET step='med_time' WHERE uid=?", (uid,))
    db.execute("INSERT INTO patients(owner, name, code) VALUES(?,?,?)",
               (uid, name, gen_code()))
    db.commit()
    await m.answer(t(uid, "med_ask_time"), reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🏠 Меню")]], resize_keyboard=True))

@dp.message(F.text)
async def time_handler(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    row = db.execute("SELECT step FROM users WHERE uid=?", (uid,)).fetchone()
    if not row or row["step"] != "med_time":
        return
    
    times_text = m.text.strip()
    times = []
    for part in times_text.split(","):
        part = part.strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", part):
            await m.answer(t(uid, "med_bad_time"))
            return
        h, mi = map(int, part.split(":"))
        if h > 23 or mi > 59:
            await m.answer(t(uid, "med_bad_time"))
            return
        times.append(f"{h:02d}:{mi:02d}")
    
    times = sorted(set(times))[:6]
    if not times:
        await m.answer(t(uid, "med_bad_time"))
        return
    
    db.execute("UPDATE users SET step='menu' WHERE uid=?", (uid,))
    db.commit()
    await m.answer(t(uid, "med_done", times=", ".join(times)), reply_markup=main_menu(uid))

@dp.callback_query(F.data.startswith(("take:", "takefor:")))
async def cb_take(cb: CallbackQuery, bot: Bot):
    ensure_user(cb.from_user.id, cb.from_user.language_code)
    uid = cb.from_user.id
    mode, did = cb.data.split(":")
    d = db.execute("SELECT * FROM doses WHERE id=?", (int(did),)).fetchone()
    if d is None:
        await cb.answer(t(uid, "not_found"))
        return
    p = db.execute("SELECT * FROM patients WHERE id=?", (d["patient_id"],)).fetchone()
    allowed = p["owner"] == uid if mode == "takefor" else uid in (p["user_uid"], p["owner"])
    if not allowed:
        await cb.answer(t(uid, "not_found"))
        return
    if d["status"] != "pending":
        await cb.answer(t(uid, "already"))
        return
    db.execute("UPDATE doses SET status='taken' WHERE id=?", (d["id"],))
    db.commit()
    med = db.execute("SELECT name FROM meds WHERE id=?", (d["med_id"],)).fetchone()
    medname = med["name"] if med else "💊"
    await cb.answer(t(uid, "taken_ok"))
    try:
        if mode == "takefor":
            await cb.message.edit_text(t(uid, "resolved", name=p["name"], med=medname))
        else:
            hm = (now_utc() + timedelta(hours=tz_of(uid))).strftime("%H:%M")
            await cb.message.edit_text(t(uid, "taken_edit", med=medname, time=hm))
    except Exception:
        pass
    if d["stage"] >= 3 and p["owner"] != p["user_uid"]:
        try:
            await bot.send_message(p["owner"],
                                   t(p["owner"], "resolved", name=p["name"], med=medname))
        except Exception:
            pass

@dp.callback_query(F.data.startswith("snooze:"))
async def cb_snooze(cb: CallbackQuery):
    ensure_user(cb.from_user.id, cb.from_user.language_code)
    uid = cb.from_user.id
    d = db.execute("SELECT * FROM doses WHERE id=?",
                   (int(cb.data.split(":")[1]),)).fetchone()
    if d is None:
        await cb.answer(t(uid, "not_found"))
        return
    p = db.execute("SELECT * FROM patients WHERE id=?", (d["patient_id"],)).fetchone()
    if uid not in (p["user_uid"], p["owner"]) or d["status"] != "pending":
        await cb.answer(t(uid, "already"))
        return
    if d["snoozes"] >= 3:
        await cb.answer(t(uid, "snooze_limit"))
        return
    su = (now_utc() + timedelta(minutes=SNOOZE_MIN)).isoformat()
    db.execute("UPDATE doses SET snoozes=snoozes+1, snooze_until=? WHERE id=?", (su, d["id"]))
    db.commit()
    await cb.answer(t(uid, "snoozed"))

# Планировщик (тот же, что в v2)
async def tick(bot: Bot):
    now = now_utc()
    for p in db.execute("SELECT * FROM patients").fetchall():
        ptz = tz_of(p["user_uid"] or p["owner"])
        plocal = now + timedelta(hours=ptz)
        today = plocal.date().isoformat()
        target = p["user_uid"] or p["owner"]
        for med in db.execute("SELECT * FROM meds WHERE patient_id=?", (p["id"],)).fetchall():
            created = datetime.fromisoformat(med["created"])
            for stime in med["times"].split(","):
                h, mi = map(int, stime.split(":"))
                due = plocal.replace(hour=h, minute=mi, second=0, microsecond=0) \
                      - timedelta(hours=ptz)
                if due <= now and now - due <= timedelta(hours=2) and due >= created:
                    db.execute("INSERT OR IGNORE INTO doses(med_id, patient_id, day, stime, due) "
                               "VALUES(?,?,?,?,?)",
                               (med["id"], p["id"], today, stime, due.isoformat()))
        db.commit()
        rows = db.execute("SELECT d.*, m.name medname FROM doses d "
                          "JOIN meds m ON m.id=d.med_id "
                          "WHERE d.patient_id=? AND d.status='pending'", (p["id"],)).fetchall()
        for d in rows:
            last = datetime.fromisoformat(d["last_sent"]) if d["last_sent"] else None
            if d["snooze_until"]:
                su = datetime.fromisoformat(d["snooze_until"])
                if now < su:
                    continue
                await send_reminder(bot, target, d["id"], d["medname"], d["stime"])
                db.execute("UPDATE doses SET snooze_until=NULL, last_sent=?, stage=1 WHERE id=?",
                           (now.isoformat(), d["id"]))
                continue
            if d["stage"] == 0:
                await send_reminder(bot, target, d["id"], d["medname"], d["stime"])
                db.execute("UPDATE doses SET stage=1, last_sent=? WHERE id=?",
                           (now.isoformat(), d["id"]))
            elif d["stage"] == 1 and last and now >= last + timedelta(minutes=ESC_REPEAT_MIN):
                await send_reminder(bot, target, d["id"], d["medname"], d["stime"], repeat=True)
                db.execute("UPDATE doses SET stage=2, last_sent=? WHERE id=?",
                           (now.isoformat(), d["id"]))
            elif d["stage"] == 2 and last and now >= last + timedelta(minutes=ESC_ALERT_MIN):
                if p["owner"] != target:
                    try:
                        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                            text=t(p["owner"], "btn_takefor"),
                            callback_data=f"takefor:{d['id']}")]])
                        await bot.send_message(
                            p["owner"],
                            t(p["owner"], "esc_owner", name=p["name"],
                              med=d["medname"], time=d["stime"]),
                            reply_markup=kb)
                    except Exception as e:
                        print(f"alert: {e}")
                db.execute("UPDATE doses SET stage=3, last_sent=? WHERE id=?",
                           (now.isoformat(), d["id"]))
            elif d["stage"] == 3 and last and now >= last + timedelta(hours=MISSED_AFTER_H):
                db.execute("UPDATE doses SET status='missed' WHERE id=?", (d["id"],))
        olocal = now + timedelta(hours=tz_of(p["owner"]))
        if olocal.hour >= DIGEST_HOUR and (p["last_digest"] or "") != olocal.date().isoformat():
            r = db.execute("SELECT COUNT(*) c, SUM(status='taken') k FROM doses "
                           "WHERE patient_id=? AND day=?", (p["id"], today)).fetchone()
            if r["c"]:
                try:
                    await bot.send_message(p["owner"], t(p["owner"], "digest",
                                           name=p["name"], done=r["k"] or 0, total=r["c"]))
                except Exception as e:
                    print(f"digest: {e}")
            db.execute("UPDATE patients SET last_digest=? WHERE id=?",
                       (olocal.date().isoformat(), p["id"]))
    db.commit()

async def scheduler(bot: Bot):
    while True:
        try:
            await tick(bot)
        except Exception as e:
            print("scheduler:", e)
        await asyncio.sleep(20)

async def main():
    if "ВСТАВЬТЕ" in TOKEN:
        raise SystemExit("❌ Укажите токен")
    bot = Bot(token=TOKEN)
    asyncio.create_task(scheduler(bot))
    print("✅ Care bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
