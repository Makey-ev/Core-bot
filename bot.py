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
                           InlineKeyboardMarkup, Message)
from texts import TEXTS

TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН")

MAX_PROFILES = 3     # профилей на опекуна (пилот)
FREE_MEDS = 3        # лекарств на профиль (пилот; потом 1 + Premium)
DIGEST_HOUR = 21     # час вечернего дайджеста (время опекуна)
SNOOZE_MIN = 15
ESC_REPEAT_MIN = 30  # через сколько повторить пациенту
ESC_ALERT_MIN = 30   # ещё через сколько — сигнал опекуну
MISSED_AFTER_H = 12  # когда считать приём пропущенным

db = sqlite3.connect("care.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    uid INTEGER PRIMARY KEY, lang TEXT, tz INTEGER DEFAULT 3);
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
    await m.answer(t(m.from_user.id, "welcome"))

@dp.message(Command("help"))
async def cmd_help(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    await m.answer(t(m.from_user.id, "welcome"))

@dp.message(Command("language"))
async def cmd_lang(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"lang:{code}")]
        for code, name in LANG_NAMES.items()])
    await m.answer(t(m.from_user.id, "lang_choose"), reply_markup=kb)

@dp.callback_query(F.data.startswith("lang:"))
async def cb_lang(cb: CallbackQuery):
    ensure_user(cb.from_user.id, cb.from_user.language_code)
    code = cb.data.split(":")[1]
    if code in TEXTS:
        db.execute("UPDATE users SET lang=? WHERE uid=?", (code, cb.from_user.id))
        db.commit()
    await cb.answer("OK")
    await cb.message.edit_text(t(cb.from_user.id, "lang_set"))

@dp.message(Command("tz"))
async def cmd_tz(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    parts = m.text.split(maxsplit=1)
    try:
        tz = int(parts[1].replace("+", ""))
        assert -12 <= tz <= 14
    except Exception:
        await m.answer(t(uid, "tz_usage"))
        return
    db.execute("UPDATE users SET tz=? WHERE uid=?", (tz, uid))
    db.commit()
    await m.answer(t(uid, "tz_set", tz=f"{tz:+d}"))

@dp.message(Command("care"))
async def cmd_care(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or " " in parts[1].strip():
        await m.answer(t(uid, "care_usage"))
        return
    name = parts[1].strip()[:30]
    if db.execute("SELECT 1 FROM patients WHERE owner=? AND name=?", (uid, name)).fetchone():
        await m.answer(t(uid, "care_usage"))
        return
    if db.execute("SELECT COUNT(*) c FROM patients WHERE owner=?", (uid,)).fetchone()["c"] >= MAX_PROFILES:
        await m.answer(t(uid, "care_limit"))
        return
    code = gen_code()
    db.execute("INSERT INTO patients(owner, name, code) VALUES(?,?,?)", (uid, name, code))
    db.commit()
    await m.answer(t(uid, "care_created", name=name, code=code))

@dp.message(Command("self"))
async def cmd_self(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    if db.execute("SELECT 1 FROM patients WHERE owner=? AND user_uid=?", (uid, uid)).fetchone():
        await m.answer(t(uid, "self_exists"))
        return
    name = t(uid, "self_name")
    db.execute("INSERT INTO patients(owner, name, user_uid, code) VALUES(?,?,?,?)",
               (uid, name, uid, gen_code()))
    db.commit()
    await m.answer(t(uid, "self_created", name=name))

@dp.message(Command("med"))
async def cmd_med(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    parts = m.text.split(maxsplit=3)
    if len(parts) < 4:
        await m.answer(t(uid, "med_usage"))
        return
    p = db.execute("SELECT * FROM patients WHERE owner=? AND name=?",
                   (uid, parts[1])).fetchone()
    if p is None:
        await m.answer(t(uid, "no_profile", name=parts[1]))
        return
    times = []
    for part in parts[2].split(","):
        part = part.strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", part):
            await m.answer(t(uid, "med_usage"))
            return
        h, mi = map(int, part.split(":"))
        if h > 23 or mi > 59:
            await m.answer(t(uid, "med_usage"))
            return
        times.append(f"{h:02d}:{mi:02d}")
    times = sorted(set(times))[:6]
    if db.execute("SELECT COUNT(*) c FROM meds WHERE patient_id=?",
                  (p["id"],)).fetchone()["c"] >= FREE_MEDS:
        await m.answer(t(uid, "med_limit"))
        return
    db.execute("INSERT INTO meds(patient_id, name, times, created) VALUES(?,?,?,?)",
               (p["id"], parts[3].strip()[:50], ",".join(times), now_utc().isoformat()))
    db.commit()
    await m.answer(t(uid, "med_added", name=p["name"], med=parts[3].strip()[:50],
                     times=", ".join(times)))

@dp.message(Command("family"))
async def cmd_family(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer(t(uid, "family_bad"))
        return
    p = db.execute("SELECT * FROM patients WHERE code=?",
                   (parts[1].strip().upper(),)).fetchone()
    if p is None:
        await m.answer(t(uid, "family_bad"))
        return
    if p["user_uid"] and p["user_uid"] != uid:
        await m.answer(t(uid, "family_already"))
        return
    db.execute("UPDATE patients SET user_uid=? WHERE id=?", (uid, p["id"]))
    db.commit()
    await m.answer(t(uid, "family_ok", name=p["name"]))
    if p["owner"] != uid:
        try:
            await m.bot.send_message(p["owner"], t(p["owner"], "family_notify", name=p["name"]))
        except Exception:
            pass

@dp.message(Command("list"))
async def cmd_list(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    ps = db.execute("SELECT * FROM patients WHERE owner=? OR user_uid=?", (uid, uid)).fetchall()
    if not ps:
        await m.answer(t(uid, "list_empty"))
        return
    lines, buttons = [], []
    for p in ps:
        ptz = tz_of(p["user_uid"] or p["owner"])
        today = (now_utc() + timedelta(hours=ptz)).date().isoformat()
        linked = "✅" if p["user_uid"] else f"⏳ {p['code']}"
        lines.append(f"\n👤 {p['name']} {linked}")
        for med in db.execute("SELECT * FROM meds WHERE patient_id=?", (p["id"],)).fetchall():
            c = db.execute("SELECT COUNT(*) c, SUM(status='taken') k FROM doses "
                           "WHERE med_id=? AND day=?", (med["id"], today)).fetchone()
            lines.append(f"  💊 {med['name']} — {med['times']} ({c['k'] or 0}/{c['c']})")
            if p["owner"] == uid:
                buttons.append([InlineKeyboardButton(
                    text=f"🗑 {p['name']}: {med['name']}"[:40],
                    callback_data=f"delmed:{med['id']}")])
    await m.answer(t(uid, "list_head") + "\n" + "\n".join(lines),
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None)

@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    uid = m.from_user.id
    ps = db.execute("SELECT * FROM patients WHERE owner=? OR user_uid=?", (uid, uid)).fetchall()
    if not ps:
        await m.answer(t(uid, "list_empty"))
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
    await m.answer("\n".join(lines))

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

@dp.callback_query(F.data.startswith("delmed:"))
async def cb_delmed(cb: CallbackQuery):
    ensure_user(cb.from_user.id, cb.from_user.language_code)
    uid = cb.from_user.id
    med = db.execute("SELECT * FROM meds WHERE id=?",
                     (int(cb.data.split(":")[1]),)).fetchone()
    if med is None:
        await cb.answer(t(uid, "not_found"))
        return
    p = db.execute("SELECT * FROM patients WHERE id=?", (med["patient_id"],)).fetchone()
    if p["owner"] != uid:
        await cb.answer(t(uid, "not_found"))
        return
    db.execute("DELETE FROM doses WHERE med_id=?", (med["id"],))
    db.execute("DELETE FROM meds WHERE id=?", (med["id"],))
    db.commit()
    await cb.answer(t(uid, "med_deleted"))
    try:
        await cb.message.edit_text(t(uid, "med_deleted") + "\n/list")
    except Exception:
        pass

@dp.message(F.text)
async def on_text(m: Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    await m.answer(t(m.from_user.id, "unknown"))

# ---------- Ядро: планировщик (не зависит от платформы) ----------
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
        raise SystemExit("❌ Укажите токен в строке TOKEN или в переменной BOT_TOKEN")
    bot = Bot(token=TOKEN)
    asyncio.create_task(scheduler(bot))
    print("✅ Care bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
