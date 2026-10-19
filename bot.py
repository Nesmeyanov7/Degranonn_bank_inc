import os
import time
import logging
import discord
from discord import app_commands, ButtonStyle
from discord.ui import View, Button, Modal, TextInput, button, Select
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiosqlite
import datetime
import asyncio
import io
import csv

# Настройка логирования предупреждений Discord (например, если сработал лимит запросов 429)
logging.basicConfig(level=logging.WARNING, format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DB_NAME = 'bank.db'
ADMIN_ROLE_NAME = "Техник"
LEADER_ROLE_NAME = "Лидер Фракции"
MAX_AMOUNT = 1_000_000_000_000  # Защита от переполнения SQLite INTEGER (макс. 1 триллион за операцию)

BANK_ACCOUNT_ID = "0000000000"
BANK_DISCORD_ID = 0

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Глобальное хранилище уведомлений (в памяти)
# Структура: {user_id: [{"msg": str, "read": bool, "time": str}]}
user_notifications = {}

RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MSK_TZ = datetime.timezone(datetime.timedelta(hours=3), name="MSK")

# Постоянное подключение к БД и блокировка записи
_db_conn: aiosqlite.Connection | None = None
_db_init_lock = asyncio.Lock()
db_write_lock = asyncio.Lock()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_msk_now() -> datetime.datetime:
    """Возвращает точное московское время (UTC+3) независимо от часового пояса хостинга"""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(MSK_TZ)


def get_treasury_account_id(prefix: str) -> str:
    """Возвращает 10-значный номер счета казны для кода фракции (например, '01' -> '0100000000')"""
    clean = (prefix or "01")[:2].zfill(2)
    return f"{clean}00000000"


def get_treasury_discord_id(prefix: str) -> int:
    """Возвращает отрицательный системный ID для записи казны фракции в таблице users"""
    try:
        val = int(prefix)
        return -val if val > 0 else -100
    except ValueError:
        return -999


def is_treasury_account(account_id: str) -> bool:
    return (
        len(account_id) == 10
        and account_id.isdigit()
        and account_id != BANK_ACCOUNT_ID
        and account_id.endswith("00000000")
    )


def format_money(val: int) -> str:
    return f"{val:,}".replace(",", " ")


def calc_delta_seconds(val: int, unit: str) -> int:
    mapping = {
        'seconds': 1,
        'minutes': 60,
        'hours': 3600,
        'days': 86400,
        'weeks': 604800,
        'months': 2592000
    }
    return val * mapping.get(unit, 0)


def compute_next_run_after(prev_run_str: str, val: int, unit: str) -> str:
    """
    Рассчитывает следующую дату списания строго по расписанию (без сдвига времени),
    но если бот долго был выключен — гарантирует, что следующая дата будет в будущем.
    """
    now = get_msk_now().replace(microsecond=0)
    delta_sec = max(1, calc_delta_seconds(val, unit))
    delta = datetime.timedelta(seconds=delta_sec)

    try:
        prev_dt = datetime.datetime.strptime(prev_run_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK_TZ)
        next_dt = prev_dt + delta
        if next_dt <= now:
            missed_steps = int((now - prev_dt).total_seconds() // delta_sec) + 1
            next_dt = prev_dt + datetime.timedelta(seconds=missed_steps * delta_sec)
    except ValueError:
        next_dt = now + delta

    return next_dt.strftime("%Y-%m-%d %H:%M:%S")


def sanitize_csv_cell(val) -> str:
    """Защита от CSV-инъекций (Formula Injection в Excel)"""
    s = "" if val is None else str(val)
    if s and s[0] in ('=', '+', '-', '@'):
        return f"'{s}"
    return s


UNIT_LABELS = {
    'seconds': 'сек.',
    'minutes': 'мин.',
    'hours': 'ч.',
    'days': 'дн.',
    'weeks': 'нед.',
    'months': 'мес.'
}

TX_TYPE_LABELS = {
    'user_transfer': ('💸 Перевод', 'user_transfer'),
    'auto_transfer': ('🔄 Автоплатеж', 'auto_transfer'),
    'bank_auto_deposit': ('🏦 Автоначисление (Банк)', 'bank_auto_deposit'),
    'bank_auto_withdraw': ('🏦 Автосписание (Банк)', 'bank_auto_withdraw'),
    'faction_deposit': ('📥 Взнос в казну', 'faction_deposit'),
    'faction_withdraw': ('📤 Снятие из казны', 'faction_withdraw'),
    'admin_deposit': ('💰 Зачисление (Банк)', 'admin_deposit'),
    'admin_withdraw': ('📉 Списание (Банк)', 'admin_withdraw'),
    'admin_set': ('⚙️ Установка баланса', 'admin_set'),
}


def parse_first_run_datetime(
    date_str: str,
    time_str: str
) -> tuple[datetime.datetime | None, str | None, bool]:
    """
    Рассчитывает дату и время первого списания по МСК.
    Возвращает: (target_datetime, error_message, shifted_to_tomorrow)
    """
    now = get_msk_now().replace(microsecond=0)
    date_str = date_str.strip()
    time_str = time_str.strip()
    shifted_tomorrow = False

    if not date_str and not time_str:
        return now, None, False

    parsed_date: datetime.date | None = None
    if date_str:
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                parsed_date = datetime.datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue
        if parsed_date is None:
            return (
                None,
                "❌ Неверный формат даты! Используйте `ДД.ММ.ГГГГ` (например, `25.10.2026`) или `ГГГГ-ММ-ДД`.",
                False
            )

        if parsed_date < now.date():
            return None, "❌ Нельзя указать дату, которая уже прошла!", False
        if parsed_date > now.date() + datetime.timedelta(days=3650):
            return None, "❌ Дата слишком далеко в будущем (максимум 10 лет)!", False

    parsed_time: datetime.time | None = None
    if time_str:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.datetime.strptime(time_str, fmt).time()
                break
            except ValueError:
                continue
        if parsed_time is None:
            return (
                None,
                "❌ Неверный формат времени! Используйте `ЧЧ:ММ` (например, `18:30`) или `ЧЧ:ММ:СС`.",
                False
            )

    if parsed_date is None and parsed_time is not None:
        candidate = datetime.datetime.combine(
            now.date(),
            parsed_time,
            tzinfo=MSK_TZ
        )
        if candidate <= now:
            candidate += datetime.timedelta(days=1)
            shifted_tomorrow = True
        return candidate, None, shifted_tomorrow

    if parsed_date is not None and parsed_time is None:
        candidate = datetime.datetime.combine(
            parsed_date,
            now.time(),
            tzinfo=MSK_TZ
        )
        return candidate, None, False

    candidate = datetime.datetime.combine(
        parsed_date,
        parsed_time,
        tzinfo=MSK_TZ
    )
    if candidate < now:
        return (
            None,
            "❌ Указанное время сегодня уже прошло! Оставьте поле даты пустым (для автопереноса на завтра) или укажите будущее время.",
            False
        )

    return candidate, None, False


def check_is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
    return any(role.name == ADMIN_ROLE_NAME for role in interaction.user.roles)


def check_is_leader(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
    return any(role.name == LEADER_ROLE_NAME for role in interaction.user.roles)


def build_profile_embed(user_data) -> discord.Embed:
    balance = user_data[4]
    is_blocked = balance < 0

    if is_blocked:
        embed = discord.Embed(
            title="🏦 Личный кабинет — ⛔ СЧЕТ ЗАБЛОКИРОВАН",
            description=(
                f"⚠️ **Ваш банковский счет заблокирован из-за задолженности перед Банком!**\n"
                f"Все исходящие переводы и операции приостановлены.\n"
                f"Пополните счет минимум на **{format_money(abs(balance))}**, чтобы погасить долг и вернуть доступ к функциям."
            ),
            color=discord.Color.dark_red()
        )
    else:
        embed = discord.Embed(title="🏦 Личный кабинет", color=discord.Color.green())

    embed.add_field(name="👤 Пользователь", value=user_data[1], inline=True)
    embed.add_field(name="🎮 Ник в игре", value=user_data[2], inline=True)
    embed.add_field(name="🛡️ Код фракции", value=f"`{user_data[5]}`", inline=True)
    embed.add_field(name="💳 Номер счета", value=f"`{user_data[3]}`", inline=True)
    status_str = f"⛔ **{format_money(balance)}** (Долг)" if is_blocked else f"**{format_money(balance)}**"
    embed.add_field(name="💰 Баланс", value=status_str, inline=True)
    return embed


# --- МГНОВЕННЫЕ ОТВЕТЫ НА ИНТЕРАКЦИИ (1 HTTP-ЗАПРОС БЕЗ RATE-LIMIT И БЕЗ ЛИШНЕГО DEFER) ---

async def safe_send_modal(interaction: discord.Interaction, modal: Modal) -> bool:
    """Открывает модальное окно за 1 прямой HTTP-запрос с замером времени отклика Discord API."""
    t0 = time.perf_counter()
    try:
        await interaction.response.send_modal(modal)
        dt = time.perf_counter() - t0
        if dt > 1.0:
            print(f"[DIAG] send_modal занял {dt:.2f} сек. (задержка сети до серверов Discord).")
        return True
    except discord.NotFound:
        print(f"[WARN] Не удалось открыть модальное окно: интеракция {interaction.id} истекла (> 3 сек в пути).")
        return False
    except discord.HTTPException as e:
        print(f"[WARN] Ошибка HTTP при открытии модального окна: {e}")
        return False


async def safe_send_ephemeral(
    interaction: discord.Interaction,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: View | None = None,
    file: discord.File | None = None
):
    """
    Отправляет новое эфемерное сообщение за 1 прямой HTTP-запрос (interaction.response.send_message)
    без предварительного defer(), что исключает двойной пинг и ожидание.
    """
    kwargs = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if file is not None:
        kwargs["file"] = file

    t0 = time.perf_counter()
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(**kwargs)
        else:
            await interaction.followup.send(**kwargs)
        dt = time.perf_counter() - t0
        if dt > 1.0:
            print(f"[DIAG] send_message занял {dt:.2f} сек. (задержка сети/API Discord).")
    except discord.NotFound:
        print(f"[WARN] Интеракция {interaction.id} истекла до отправки ответа (> 3 сек в пути).")
    except discord.HTTPException as e:
        print(f"[WARN] Ошибка HTTP при отправке сообщения {interaction.id}: {e}")


async def safe_edit_or_send(
    interaction: discord.Interaction,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: View | None = None
):
    """
    Мгновенно обновляет сообщение за 1 прямой HTTP-запрос (interaction.response.edit_message — тип 7 UPDATE_MESSAGE).
    В отличие от связки defer() + edit_original_response(), прямой edit_message:
    1) Выполняется в 2 раза быстрее (1 сетевой запрос вместо 2);
    2) НЕ попадает под жесткий лимит Discord (5 запросов за 5 сек на вебхук сообщения) при быстрых кликах по меню!
    """
    kwargs = {"content": content}
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view

    t0 = time.perf_counter()
    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(**kwargs)
        else:
            await interaction.edit_original_response(**kwargs)
        dt = time.perf_counter() - t0
        if dt > 1.0:
            print(f"[DIAG] edit_message занял {dt:.2f} сек. (задержка сети/API Discord).")
    except discord.NotFound:
        try:
            send_kwargs = {k: v for k, v in kwargs.items() if v is not None}
            if not interaction.response.is_done():
                await interaction.response.send_message(**send_kwargs, ephemeral=True)
            else:
                await interaction.followup.send(**send_kwargs, ephemeral=True)
        except discord.HTTPException as e:
            print(f"[WARN] Не удалось обновить сообщение для интеракции {interaction.id}: {e}")
    except discord.HTTPException as e:
        print(f"[WARN] Ошибка HTTP при обновлении сообщения {interaction.id}: {e}")


# --- БАЗА ДАННЫХ (ЕДИНОЕ ПОСТОЯННОЕ ПОДКЛЮЧЕНИЕ В ПАМЯТИ) ---

async def ensure_bank_account(conn: aiosqlite.Connection):
    """Гарантирует наличие главного счета Центрального Банка (0000000000)"""
    await conn.execute(
        '''
        INSERT OR IGNORE INTO users (discord_id, discord_tag, game_nick, account_id, balance, faction_prefix)
        VALUES (?, ?, ?, ?, 1000000000, '00')
        ''',
        (BANK_DISCORD_ID, "SYSTEM_BANK", "🏦 Центральный Банк", BANK_ACCOUNT_ID)
    )


async def ensure_faction_treasury(conn: aiosqlite.Connection, prefix: str) -> str:
    """Гарантирует наличие 10-значного счета казны фракции (XX00000000) в таблице users"""
    clean_prefix = (prefix or "01")[:2].zfill(2)
    if clean_prefix == "00":
        return BANK_ACCOUNT_ID
    treasury_acc = get_treasury_account_id(clean_prefix)
    treasury_did = get_treasury_discord_id(clean_prefix)
    await conn.execute(
        '''
        INSERT OR IGNORE INTO users (discord_id, discord_tag, game_nick, account_id, balance, faction_prefix)
        VALUES (?, ?, ?, ?, 0, ?)
        ''',
        (treasury_did, f"КАЗНА_{clean_prefix}", f"🛡️ Казна фракции #{clean_prefix}", treasury_acc, clean_prefix)
    )
    return treasury_acc


async def get_db() -> aiosqlite.Connection:
    """
    Возвращает единое постоянное соединение с SQLite с кэшированием в оперативной памяти (RAM).
    """
    global _db_conn
    if _db_conn is not None:
        return _db_conn

    async with _db_init_lock:
        if _db_conn is None:
            conn = await aiosqlite.connect(DB_NAME)
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA temp_store=MEMORY")
            await conn.execute("PRAGMA mmap_size=67108864")  # 64 МБ отображаются напрямую в RAM
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA cache_size=-16000")

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    discord_id INTEGER PRIMARY KEY,
                    discord_tag TEXT,
                    game_nick TEXT,
                    account_id TEXT UNIQUE,
                    balance INTEGER DEFAULT 0,
                    faction_prefix TEXT
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    type TEXT NOT NULL,
                    sender_acc TEXT,
                    receiver_acc TEXT,
                    amount INTEGER NOT NULL
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS auto_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_did INTEGER,
                    sender_acc TEXT,
                    receiver_acc TEXT,
                    amount INTEGER,
                    interval_val INTEGER,
                    interval_unit TEXT,
                    next_run TEXT,
                    is_active INTEGER DEFAULT 1,
                    is_bank INTEGER DEFAULT 0
                )
            ''')

            # Безопасная миграция для существующих БД (добавляем колонку is_bank, если её не было)
            async with conn.execute("PRAGMA table_info(auto_payments)") as cur:
                cols = [row[1] for row in await cur.fetchall()]
            if "is_bank" not in cols:
                await conn.execute("ALTER TABLE auto_payments ADD COLUMN is_bank INTEGER DEFAULT 0")

            # Индексы для мгновенного поиска
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_account ON users(account_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_faction ON users(faction_prefix)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_sender ON transactions(sender_acc)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_receiver ON transactions(receiver_acc)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions(created_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ap_active_next ON auto_payments(is_active, next_run)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ap_owner ON auto_payments(owner_did)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ap_sender ON auto_payments(sender_acc)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ap_receiver ON auto_payments(receiver_acc)")

            # Создаем счет Центрального Банка и счета казны для всех существующих фракций
            await ensure_bank_account(conn)
            async with conn.execute("SELECT DISTINCT faction_prefix FROM users WHERE discord_id > 0") as cur:
                prefixes = [r[0] for r in await cur.fetchall() if r[0] and r[0] != "00"]
            for pfx in prefixes:
                await ensure_faction_treasury(conn, pfx)

            await conn.commit()
            _db_conn = conn
    return _db_conn


async def init_db():
    await get_db()


async def get_user_data(discord_id: int):
    db = await get_db()
    async with db.execute('SELECT * FROM users WHERE discord_id = ?', (discord_id,)) as cursor:
        return await cursor.fetchone()


async def get_user_by_account(account_id: str):
    db = await get_db()
    async with db.execute('SELECT * FROM users WHERE account_id = ?', (account_id,)) as cursor:
        return await cursor.fetchone()


async def get_bank_balance() -> int:
    db = await get_db()
    async with db.execute('SELECT balance FROM users WHERE account_id = ?', (BANK_ACCOUNT_ID,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_faction_treasury_data(prefix: str):
    clean_prefix = (prefix or "01")[:2].zfill(2)
    db = await get_db()
    treasury_acc = get_treasury_account_id(clean_prefix)
    async with db.execute('SELECT * FROM users WHERE account_id = ?', (treasury_acc,)) as cursor:
        row = await cursor.fetchone()
    if not row:
        async with db_write_lock:
            await ensure_faction_treasury(db, clean_prefix)
            await db.commit()
        async with db.execute('SELECT * FROM users WHERE account_id = ?', (treasury_acc,)) as cursor:
            row = await cursor.fetchone()
    return row


async def get_faction_members(prefix: str):
    """Возвращает только реальных игроков фракции (discord_id > 0), без технического счета казны"""
    db = await get_db()
    async with db.execute(
        'SELECT * FROM users WHERE (faction_prefix = ? OR account_id LIKE ?) AND discord_id > 0 ORDER BY game_nick ASC',
        (prefix, f"{prefix}%")
    ) as cursor:
        return await cursor.fetchall()


async def get_user_autopayments(discord_id: int, account_id: str):
    """
    Возвращает все автоплатежи, видимые пользователю в профиле:
    1) Обязательные автоплатежи от Банка (is_bank = 1) по этому счету
    2) Личные автоплатежи пользователя (owner_did = discord_id и is_bank = 0)
    """
    db = await get_db()
    async with db.execute(
        '''
        SELECT id, owner_did, sender_acc, receiver_acc, amount, interval_val, interval_unit, next_run, is_active, is_bank
        FROM auto_payments
        WHERE is_active = 1
          AND (
              (owner_did = ? AND COALESCE(is_bank, 0) = 0)
              OR (COALESCE(is_bank, 0) = 1 AND (sender_acc = ? OR receiver_acc = ?))
          )
        ORDER BY COALESCE(is_bank, 0) DESC, next_run ASC
        ''',
        (discord_id, account_id, account_id)
    ) as cur:
        return await cur.fetchall()


async def log_transaction(db: aiosqlite.Connection, t_type: str, sender: str | None, receiver: str | None, amount: int):
    timestamp = get_msk_now().strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO transactions (created_at, type, sender_acc, receiver_acc, amount) VALUES (?, ?, ?, ?, ?)",
        (timestamp, t_type, sender, receiver, amount)
    )


async def add_notification(user_id: int, message: str):
    if not user_id or user_id <= 0:
        return
    if user_id not in user_notifications:
        user_notifications[user_id] = []
    user_notifications[user_id].insert(0, {
        "msg": message,
        "read": False,
        "time": get_msk_now().strftime("%H:%M")
    })
    if len(user_notifications[user_id]) > 15:
        user_notifications[user_id] = user_notifications[user_id][:15]


# --- ФОНОВАЯ ЗАДАЧА АВТОПЛАТЕЖЕЙ ---

@tasks.loop(seconds=10)
async def auto_payment_task():
    try:
        now_str = get_msk_now().strftime("%Y-%m-%d %H:%M:%S")
        db = await get_db()

        # Быстрая проверка без блокировки записи: есть ли вообще платежи к исполнению
        async with db.execute(
            '''
            SELECT id, owner_did, sender_acc, receiver_acc, amount, interval_val, interval_unit, next_run, is_active, COALESCE(is_bank, 0)
            FROM auto_payments
            WHERE is_active = 1 AND next_run <= ?
            ''',
            (now_str,)
        ) as cursor:
            payments = await cursor.fetchall()

        if not payments:
            return

        async with db_write_lock:
            changed = False
            for p in payments:
                pay_id, owner_did, s_acc, r_acc, amount, i_val, i_unit, next_run_str, is_active, is_bank = p

                if amount <= 0 or s_acc == r_acc:
                    await db.execute('UPDATE auto_payments SET is_active = 0 WHERE id = ?', (pay_id,))
                    changed = True
                    continue

                # Проверяем существование обоих счетов
                async with db.execute('SELECT balance, discord_id, game_nick FROM users WHERE account_id = ?', (s_acc,)) as cur:
                    s_row = await cur.fetchone()
                async with db.execute('SELECT balance, discord_id, game_nick FROM users WHERE account_id = ?', (r_acc,)) as cur:
                    r_row = await cur.fetchone()

                if not s_row or not r_row:
                    await db.execute('UPDATE auto_payments SET is_active = 0 WHERE id = ?', (pay_id,))
                    changed = True
                    continue

                s_bal, s_did, s_nick = s_row
                r_bal, r_did, r_nick = r_row

                # --- ЛОГИКА 1: АВТОПЛАТЕЖ ОТ БАНКА (is_bank == 1) ---
                if is_bank == 1:
                    if s_acc == BANK_ACCOUNT_ID:
                        # Начисление от Банка игроку/фракции (со счета Банка)
                        cur_bank = await db.execute(
                            'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                            (amount, BANK_ACCOUNT_ID, amount)
                        )
                        if cur_bank.rowcount == 0:
                            continue

                        await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, r_acc))
                        await log_transaction(db, 'bank_auto_deposit', BANK_ACCOUNT_ID, r_acc, amount)

                        if r_did and r_did > 0:
                            new_r_bal = r_bal + amount
                            if r_bal < 0 and new_r_bal >= 0:
                                await add_notification(r_did, f"🏦 Автоначисление от Банка: **+{format_money(amount)}**. 🔓 Ваш счет разблокирован!")
                            else:
                                await add_notification(r_did, f"🏦 Автоначисление от Банка: **+{format_money(amount)}**.")

                        new_next_run = compute_next_run_after(next_run_str, i_val, i_unit)
                        await db.execute('UPDATE auto_payments SET next_run = ? WHERE id = ?', (new_next_run, pay_id))
                        changed = True

                    elif r_acc == BANK_ACCOUNT_ID:
                        # Списание Банком со счета игрока/фракции
                        # Если счет уже в минусе (заблокирован) — повторно в минус не уводим! Ждем погашения долга.
                        if s_bal < 0:
                            continue

                        new_s_bal = s_bal - amount
                        await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (new_s_bal, s_acc))
                        await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, BANK_ACCOUNT_ID))
                        await log_transaction(db, 'bank_auto_withdraw', s_acc, BANK_ACCOUNT_ID, amount)

                        if s_did and s_did > 0:
                            if new_s_bal < 0:
                                await add_notification(
                                    s_did,
                                    f"⛔ Обязательное списание Банка (**-{format_money(amount)}**): средств не хватило, счет ушел в минус (**{format_money(new_s_bal)}**) и заблокирован!"
                                )
                            else:
                                await add_notification(
                                    s_did,
                                    f"🏦 Обязательное автосписание в Банк: **-{format_money(amount)}** (остаток: **{format_money(new_s_bal)}**)."
                                )

                        new_next_run = compute_next_run_after(next_run_str, i_val, i_unit)
                        await db.execute('UPDATE auto_payments SET next_run = ? WHERE id = ?', (new_next_run, pay_id))
                        changed = True

                # --- ЛОГИКА 2: ОБЫЧНЫЙ АВТОПЛАТЕЖ ИГРОКА (is_bank == 0) ---
                else:
                    if s_bal < amount:
                        continue

                    cur_deduct = await db.execute(
                        'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                        (amount, s_acc, amount)
                    )
                    if cur_deduct.rowcount == 0:
                        continue

                    await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, r_acc))
                    await log_transaction(db, 'auto_transfer', s_acc, r_acc, amount)

                    await add_notification(owner_did, f"✅ Автоплатеж **{format_money(amount)}** на `{r_acc}` ({r_nick}) выполнен.")
                    if r_did and r_did > 0:
                        await add_notification(r_did, f"💰 Получен автоплатеж **{format_money(amount)}** от `{s_acc}` ({s_nick}).")

                    new_next_run = compute_next_run_after(next_run_str, i_val, i_unit)
                    await db.execute(
                        'UPDATE auto_payments SET next_run = ? WHERE id = ?',
                        (new_next_run, pay_id)
                    )
                    changed = True

            if changed:
                await db.commit()
    except Exception as e:
        print(f"[Ошибка в auto_payment_task]: {e}")


@auto_payment_task.before_loop
async def before_auto_payment():
    await bot.wait_until_ready()


# --- МОДАЛЬНЫЕ ОКНА ---

class CreateAccountModal(Modal, title="Создание банковского счета"):
    def __init__(self, target_user: discord.Member):
        super().__init__()
        self.target_user = target_user
        self.game_nick = TextInput(label="Ник в игре", placeholder="Введите игровой ник", required=True, max_length=32)
        self.account_id = TextInput(
            label="ID Счета (ровно 10 цифр)",
            placeholder="Например: 0100000001 (первые 2 — код фракции)",
            required=True,
            max_length=10,
            min_length=10
        )
        self.add_item(self.game_nick)
        self.add_item(self.account_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ У вас нет прав администратора!")
            return

        existing = await get_user_data(self.target_user.id)
        if existing:
            await safe_send_ephemeral(interaction, f"⚠️ У пользователя {self.target_user.mention} уже есть счет: `{existing[3]}`")
            return

        acc_id = self.account_id.value.strip()
        if not acc_id.isdigit() or len(acc_id) != 10:
            await safe_send_ephemeral(interaction, "❌ ID счета должен состоять ровно из 10 цифр!")
            return

        prefix = acc_id[:2]
        if prefix == "00":
            await safe_send_ephemeral(interaction, "❌ Префикс `00` зарезервирован за Центральным Банком! Используйте коды фракций `01`–`99`.")
            return

        if acc_id.endswith("00000000"):
            await safe_send_ephemeral(interaction, f"❌ Счет `{acc_id}` зарезервирован под общую казну фракции `{prefix}`! Укажите другой номер (например, `{prefix}00000001`).")
            return

        db = await get_db()
        async with db_write_lock:
            try:
                await ensure_faction_treasury(db, prefix)
                await db.execute(
                    'INSERT INTO users (discord_id, discord_tag, game_nick, account_id, balance, faction_prefix) VALUES (?, ?, ?, ?, 0, ?)',
                    (self.target_user.id, self.target_user.name, self.game_nick.value.strip(), acc_id, prefix)
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                await safe_send_ephemeral(
                    interaction,
                    "❌ Такой ID счета или пользователь уже зарегистрирован в системе!"
                )
                return

        await add_notification(self.target_user.id, f"🎉 Вам открыт счет `{acc_id}`!")
        await safe_send_ephemeral(
            interaction,
            f"✅ Счет `{acc_id}` успешно создан для {self.target_user.mention} (Ник: **{self.game_nick.value.strip()}**, Фракция: `{prefix}`, Казна фракции: `{get_treasury_account_id(prefix)}`)."
        )


class ManageBalanceModal(Modal, title="Операция с балансом"):
    def __init__(self, action_type: str, prefill_account: str | None = None):
        super().__init__()
        self.action_type = action_type
        self.prefill_account = prefill_account
        labels = {
            "add": "Сумма зачисления",
            "remove": "Сумма списания",
            "set": "Новый баланс"
        }
        self.amount_input = TextInput(
            label=labels.get(action_type, "Сумма"),
            placeholder="Введите целое число",
            required=True,
            max_length=13
        )
        self.add_item(self.amount_input)

        if not self.prefill_account:
            self.account_input = TextInput(
                label="ID Счета (10 цифр)",
                placeholder="Счет игрока, казны (XX00000000) или Банка (0000000000)",
                required=True,
                max_length=10,
                min_length=10
            )
            self.add_item(self.account_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ У вас нет прав администратора!")
            return

        try:
            amount = int(self.amount_input.value.strip())
        except ValueError:
            await safe_send_ephemeral(interaction, "❌ Сумма должна быть целым числом!")
            return

        if self.action_type in ("add", "remove") and amount <= 0:
            await safe_send_ephemeral(interaction, "❌ Сумма зачисления/списания должна быть больше 0!")
            return

        if abs(amount) > MAX_AMOUNT:
            await safe_send_ephemeral(interaction, f"❌ Сумма превышает максимально допустимый лимит ({format_money(MAX_AMOUNT)})!")
            return

        acc_id = self.prefill_account or self.account_input.value.strip()
        if not acc_id.isdigit() or len(acc_id) != 10:
            await safe_send_ephemeral(interaction, "❌ ID счета должен состоять из 10 цифр!")
            return

        db = await get_db()
        async with db_write_lock:
            async with db.execute('SELECT balance, discord_id, game_nick FROM users WHERE account_id = ?', (acc_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await safe_send_ephemeral(interaction, "❌ Счет не найден!")
                    return

                current_bal, target_did, game_nick = row

                # Если редактируется сам счет Центрального Банка (0000000000)
                if acc_id == BANK_ACCOUNT_ID:
                    if self.action_type == "add":
                        await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, BANK_ACCOUNT_ID))
                        await log_transaction(db, "admin_deposit", None, BANK_ACCOUNT_ID, amount)
                    elif self.action_type == "remove":
                        cur_rem = await db.execute(
                            'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                            (amount, BANK_ACCOUNT_ID, amount)
                        )
                        if cur_rem.rowcount == 0:
                            await safe_send_ephemeral(
                                interaction,
                                f"❌ В казне Банка недостаточно средств (доступно: **{format_money(current_bal)}**)."
                            )
                            return
                        await log_transaction(db, "admin_withdraw", BANK_ACCOUNT_ID, None, amount)
                    elif self.action_type == "set":
                        await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (amount, BANK_ACCOUNT_ID))
                        await log_transaction(db, "admin_set", BANK_ACCOUNT_ID, BANK_ACCOUNT_ID, amount)

                # Если редактируется счет игрока или казны фракции (средства идут со счета Банка / на счет Банка)
                else:
                    if self.action_type == "add":
                        cur_bank = await db.execute(
                            'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                            (amount, BANK_ACCOUNT_ID, amount)
                        )
                        if cur_bank.rowcount == 0:
                            bank_bal = await get_bank_balance()
                            await safe_send_ephemeral(
                                interaction,
                                f"❌ На счете Центрального Банка (`{BANK_ACCOUNT_ID}`) недостаточно средств для выдачи!\n"
                                f"Доступно в Банке: **{format_money(bank_bal)}**. Пополните счет Банка в админ-панели."
                            )
                            return
                        await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, acc_id))
                        await log_transaction(db, "admin_deposit", BANK_ACCOUNT_ID, acc_id, amount)
                        if current_bal < 0 and (current_bal + amount) >= 0:
                            await add_notification(target_did, f"💰 Зачисление от Банка: **+{format_money(amount)}**. 🔓 Долг погашен, ваш счет разблокирован!")
                        else:
                            await add_notification(target_did, f"💰 Зачисление от Банка: **+{format_money(amount)}**.")

                    elif self.action_type == "remove":
                        cur_rem = await db.execute(
                            'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                            (amount, acc_id, amount)
                        )
                        if cur_rem.rowcount == 0:
                            await safe_send_ephemeral(
                                interaction,
                                f"❌ Недостаточно средств на счете `{acc_id}` (доступно: **{format_money(current_bal)}**)."
                            )
                            return
                        await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, BANK_ACCOUNT_ID))
                        await log_transaction(db, "admin_withdraw", acc_id, BANK_ACCOUNT_ID, amount)
                        await add_notification(target_did, f"📉 Списание в пользу Банка: **-{format_money(amount)}**.")

                    elif self.action_type == "set":
                        await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (amount, acc_id))
                        await log_transaction(db, "admin_set", acc_id, acc_id, amount)
                        await add_notification(target_did, f"⚙️ Ваш баланс установлен Банком на **{format_money(amount)}**.")

                await db.commit()

                async with db.execute('SELECT balance FROM users WHERE account_id = ?', (acc_id,)) as cur_new:
                    new_bal_row = await cur_new.fetchone()
                    new_bal = new_bal_row[0] if new_bal_row else 0

        bank_bal_now = await get_bank_balance()
        action_names = {"add": "Зачислено", "remove": "Списано", "set": "Установлен баланс"}
        await safe_send_ephemeral(
            interaction,
            f"✅ {action_names[self.action_type]}: **{format_money(amount)}**\n"
            f"Счет: **{game_nick}** (`{acc_id}`)\n"
            f"Новый баланс счета: **{format_money(new_bal)}**\n"
            f"🏦 Баланс Центрального Банка: **{format_money(bank_bal_now)}**"
        )


class TransferModal(Modal, title="Перевод средств"):
    def __init__(self, prefill_target: str | None = None):
        super().__init__()
        self.prefill_target = prefill_target
        if not self.prefill_target:
            self.target_acc = TextInput(
                label="ID Счета получателя",
                placeholder="10 цифр (игрок или казна фракции XX00000000)",
                required=True,
                max_length=10,
                min_length=10
            )
            self.add_item(self.target_acc)

        self.amount = TextInput(
            label="Сумма перевода",
            placeholder="Целое положительное число",
            required=True,
            max_length=12
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        sender_id = interaction.user.id
        target_acc = self.prefill_target or self.target_acc.value.strip()

        try:
            amount = int(self.amount.value.strip())
        except ValueError:
            await safe_send_ephemeral(interaction, "❌ Сумма должна быть числом!")
            return

        if amount <= 0 or amount > MAX_AMOUNT:
            await safe_send_ephemeral(interaction, "❌ Сумма перевода должна быть больше нуля и в пределах лимита!")
            return

        if not target_acc.isdigit() or len(target_acc) != 10:
            await safe_send_ephemeral(interaction, "❌ ID счета получателя должен состоять из 10 цифр!")
            return

        db = await get_db()
        async with db_write_lock:
            async with db.execute('SELECT account_id, balance, game_nick FROM users WHERE discord_id = ?', (sender_id,)) as cur:
                sender_row = await cur.fetchone()
                if not sender_row:
                    await safe_send_ephemeral(interaction, "❌ У вас нет банковского счета!")
                    return

            sender_acc, sender_bal, sender_nick = sender_row

            if sender_bal < 0:
                await safe_send_ephemeral(
                    interaction,
                    f"⛔ **Ваш счет заблокирован из-за задолженности ({format_money(sender_bal)})!**\n"
                    f"Пополните баланс минимум на **{format_money(abs(sender_bal))}**, чтобы разблокировать переводы."
                )
                return

            if target_acc == sender_acc:
                await safe_send_ephemeral(interaction, "❌ Нельзя перевести средства самому себе!")
                return

            if is_treasury_account(target_acc):
                await ensure_faction_treasury(db, target_acc[:2])

            async with db.execute('SELECT balance, discord_id, game_nick FROM users WHERE account_id = ?', (target_acc,)) as cursor:
                recv = await cursor.fetchone()
                if not recv:
                    await safe_send_ephemeral(interaction, "❌ Счет получателя не найден!")
                    return
                recv_bal, recv_did, recv_nick = recv

            cur_deduct = await db.execute(
                'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                (amount, sender_acc, amount)
            )
            if cur_deduct.rowcount == 0:
                await safe_send_ephemeral(
                    interaction,
                    f"❌ Недостаточно средств! Ваш баланс: **{format_money(sender_bal)}**"
                )
                return

            await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, target_acc))
            tx_type = 'faction_deposit' if is_treasury_account(target_acc) else 'user_transfer'
            await log_transaction(db, tx_type, sender_acc, target_acc, amount)
            await db.commit()

            async with db.execute('SELECT balance FROM users WHERE account_id = ?', (sender_acc,)) as cur_b:
                b_row = await cur_b.fetchone()
                new_s_bal = b_row[0] if b_row else (sender_bal - amount)

        await add_notification(sender_id, f"💸 Перевод **{format_money(amount)}** на счет `{target_acc}` ({recv_nick}).")
        if recv_did and recv_did > 0:
            if recv_bal < 0 and (recv_bal + amount) >= 0:
                await add_notification(recv_did, f"💰 Получен перевод **{format_money(amount)}** от `{sender_acc}` ({sender_nick}). 🔓 Ваш счет разблокирован!")
            else:
                await add_notification(recv_did, f"💰 Получен перевод **{format_money(amount)}** от `{sender_acc}` ({sender_nick}).")

        await safe_send_ephemeral(
            interaction,
            f"✅ Перевод **{format_money(amount)}** получателю **{recv_nick}** (`{target_acc}`) выполнен!\n"
            f"Остаток на вашем счете: **{format_money(new_s_bal)}**"
        )


class FactionTreasuryModal(Modal):
    """Модальное окно для пополнения казны фракции (все участники) или снятия средств из казны (только Лидер)"""
    def __init__(self, mode: str, faction_prefix: str, faction_view: "FactionView"):
        title = f"Пополнение казны #{faction_prefix}" if mode == "deposit" else f"Снятие из казны #{faction_prefix}"
        super().__init__(title=title)
        self.mode = mode  # 'deposit' | 'withdraw'
        self.prefix = faction_prefix
        self.faction_view = faction_view
        self.treasury_acc = get_treasury_account_id(faction_prefix)

        self.amount_input = TextInput(
            label="Сумма операции",
            placeholder="Введите целое положительное число",
            required=True,
            max_length=12
        )
        self.add_item(self.amount_input)

        if self.mode == "withdraw":
            self.target_input = TextInput(
                label="Счет получателя (пусто = на ваш личный счет)",
                placeholder="Оставьте пустым или укажите 10-значный счет",
                required=False,
                max_length=10
            )
            self.add_item(self.target_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value.strip())
        except ValueError:
            await safe_send_ephemeral(interaction, "❌ Сумма должна быть целым числом!")
            return

        if amount <= 0 or amount > MAX_AMOUNT:
            await safe_send_ephemeral(interaction, "❌ Сумма должна быть больше 0 и в пределах лимита!")
            return

        user_id = interaction.user.id
        db = await get_db()

        if self.mode == "deposit":
            async with db_write_lock:
                await ensure_faction_treasury(db, self.prefix)
                async with db.execute('SELECT account_id, balance, game_nick FROM users WHERE discord_id = ?', (user_id,)) as cur:
                    u_row = await cur.fetchone()
                    if not u_row:
                        await safe_send_ephemeral(interaction, "❌ У вас нет счета!")
                        return
                u_acc, u_bal, u_nick = u_row

                if u_bal < 0:
                    await safe_send_ephemeral(
                        interaction,
                        f"⛔ Ваш счет заблокирован из-за задолженности (**{format_money(u_bal)}**)! Сначала погасите долг."
                    )
                    return

                cur_ded = await db.execute(
                    'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                    (amount, u_acc, amount)
                )
                if cur_ded.rowcount == 0:
                    await safe_send_ephemeral(
                        interaction,
                        f"❌ Недостаточно средств на вашем счете (доступно: **{format_money(u_bal)}**)."
                    )
                    return

                await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, self.treasury_acc))
                await log_transaction(db, 'faction_deposit', u_acc, self.treasury_acc, amount)
                await db.commit()

            await add_notification(user_id, f"📥 Вы внесли **{format_money(amount)}** в казну фракции `{self.treasury_acc}`.")
            await self.faction_view.refresh_view(
                interaction,
                status_text=f"✅ Вы успешно пополнили казну фракции на **{format_money(amount)}**!"
            )

        elif self.mode == "withdraw":
            if not check_is_leader(interaction):
                await safe_send_ephemeral(interaction, "❌ Снимать средства из казны может только Лидер Фракции!")
                return

            async with db_write_lock:
                await ensure_faction_treasury(db, self.prefix)
                async with db.execute('SELECT account_id, game_nick, faction_prefix FROM users WHERE discord_id = ?', (user_id,)) as cur:
                    leader_row = await cur.fetchone()
                    if not leader_row:
                        await safe_send_ephemeral(interaction, "❌ У вас нет счета!")
                        return
                leader_acc, leader_nick, leader_pfx = leader_row

                if leader_pfx != self.prefix and not check_is_admin(interaction):
                    await safe_send_ephemeral(interaction, "❌ Вы не являетесь лидером этой фракции!")
                    return

                target_acc = self.target_input.value.strip() if self.target_input.value else leader_acc
                if not target_acc.isdigit() or len(target_acc) != 10:
                    await safe_send_ephemeral(interaction, "❌ Некорректный 10-значный счет получателя!")
                    return

                if target_acc == self.treasury_acc:
                    await safe_send_ephemeral(interaction, "❌ Нельзя вывести средства из казны на саму же казну!")
                    return

                async with db.execute('SELECT discord_id, game_nick FROM users WHERE account_id = ?', (target_acc,)) as cur:
                    recv_row = await cur.fetchone()
                    if not recv_row:
                        await safe_send_ephemeral(interaction, "❌ Счет получателя не найден!")
                        return
                recv_did, recv_nick = recv_row

                async with db.execute('SELECT balance FROM users WHERE account_id = ?', (self.treasury_acc,)) as cur:
                    t_row = await cur.fetchone()
                    t_bal = t_row[0] if t_row else 0

                if t_bal < 0:
                    await safe_send_ephemeral(
                        interaction,
                        f"⛔ Казна фракции заблокирована из-за задолженности (**{format_money(t_bal)}**)! Снятие недоступно."
                    )
                    return

                cur_ded = await db.execute(
                    'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                    (amount, self.treasury_acc, amount)
                )
                if cur_ded.rowcount == 0:
                    await safe_send_ephemeral(
                        interaction,
                        f"❌ В казне фракции недостаточно средств (доступно: **{format_money(t_bal)}**)."
                    )
                    return

                await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, target_acc))
                await log_transaction(db, 'faction_withdraw', self.treasury_acc, target_acc, amount)
                await db.commit()

            await add_notification(user_id, f"📤 Вы сняли **{format_money(amount)}** из казны `{self.treasury_acc}` на счет `{target_acc}` ({recv_nick}).")
            if recv_did and recv_did > 0 and recv_did != user_id:
                await add_notification(recv_did, f"💰 Лидер фракции перевел вам **{format_money(amount)}** из казны `{self.treasury_acc}`.")

            await self.faction_view.refresh_view(
                interaction,
                status_text=f"✅ Из казны фракции снято **{format_money(amount)}** на счет `{target_acc}` (**{recv_nick}**)!"
            )


class AutoPaymentStepModal(Modal, title="Параметры автоплатежа"):
    def __init__(
        self,
        unit: str,
        unit_label: str,
        prefill_date: str = "",
        prefill_time: str = "",
        prefill_account: str = "",
        is_bank_mode: bool = False,
        bank_direction: str = "withdraw"  # 'withdraw' (списание в Банк) или 'deposit' (начисление от Банка)
    ):
        super().__init__(title="Автоплатеж Банка" if is_bank_mode else "Параметры автоплатежа")
        self.unit = unit
        self.unit_label = unit_label
        self.is_bank_mode = is_bank_mode
        self.bank_direction = bank_direction

        acc_label = (
            "Счет игрока/фракции (для списания в Банк)"
            if (is_bank_mode and bank_direction == "withdraw")
            else "ID Счета получателя (10 цифр)"
        )

        self.receiver_acc = TextInput(
            label=acc_label,
            placeholder="Например: 0100000001 или казна 0100000000",
            default=prefill_account,
            required=True,
            max_length=10,
            min_length=10
        )
        self.amount = TextInput(
            label="Сумма операции",
            placeholder="Целое число > 0",
            required=True,
            max_length=12
        )
        self.interval_val = TextInput(
            label=f"Интервал (в: {unit_label})",
            placeholder="Например: 1, 5, 12, 24",
            required=True,
            max_length=6
        )
        self.start_date = TextInput(
            label="Дата 1-го исполнения (пусто = сегодня)",
            placeholder="ДД.ММ.ГГГГ или ГГГГ-ММ-ДД (необязательно)",
            default=prefill_date,
            required=False,
            max_length=10
        )
        self.start_time = TextInput(
            label="Время 1-го исполнения по МСК (пусто = сейчас)",
            placeholder="ЧЧ:ММ (если прошло сегодня — перенесется на завтра)",
            default=prefill_time,
            required=False,
            max_length=8
        )

        self.add_item(self.receiver_acc)
        self.add_item(self.amount)
        self.add_item(self.interval_val)
        self.add_item(self.start_date)
        self.add_item(self.start_time)

    async def on_submit(self, interaction: discord.Interaction):
        target_acc = self.receiver_acc.value.strip()
        if not target_acc.isdigit() or len(target_acc) != 10:
            await safe_send_ephemeral(interaction, "❌ Неверный формат ID счета (должно быть ровно 10 цифр)!")
            return

        try:
            amount = int(self.amount.value.strip())
            val = int(self.interval_val.value.strip())
        except ValueError:
            await safe_send_ephemeral(interaction, "❌ Сумма и интервал должны быть целыми числами!")
            return

        if amount <= 0 or amount > MAX_AMOUNT or val <= 0 or val > 10000:
            await safe_send_ephemeral(interaction, "❌ Сумма и интервал должны быть больше 0 и в пределах допустимых лимитов!")
            return

        first_dt, err_msg, shifted_tomorrow = parse_first_run_datetime(
            self.start_date.value,
            self.start_time.value
        )
        if err_msg or first_dt is None:
            await safe_send_ephemeral(interaction, err_msg or "❌ Ошибка даты/времени.")
            return

        next_run_str = first_dt.strftime("%Y-%m-%d %H:%M:%S")
        db = await get_db()

        if is_treasury_account(target_acc):
            async with db_write_lock:
                await ensure_faction_treasury(db, target_acc[:2])
                await db.commit()

        target_user = await get_user_by_account(target_acc)
        if not target_user:
            await safe_send_ephemeral(interaction, "❌ Указанный счет не существует в базе!")
            return

        note = ""
        if shifted_tomorrow:
            note = "\nℹ️ *Указанное время сегодня уже прошло, поэтому первое исполнение автоматически назначено на завтра.*"

        # --- РЕЖИМ 1: СОЗДАНИЕ АВТОПЛАТЕЖА ОТ БАНКА (АДМИН) ---
        if self.is_bank_mode:
            if not check_is_admin(interaction):
                await safe_send_ephemeral(interaction, "❌ Только сотрудники банка могут создавать банковские автоплатежи!")
                return

            if target_acc == BANK_ACCOUNT_ID:
                await safe_send_ephemeral(interaction, "❌ Нельзя назначить автоплатеж Банка самому Банку!")
                return

            if self.bank_direction == "deposit":
                s_acc = BANK_ACCOUNT_ID
                r_acc = target_acc
                dir_label = f"🟢 Начисление от Банка на `{target_acc}` ({target_user[2]})"
                notif_text = f"🏦 Банк назначил вам автоначисление: **+{format_money(amount)}** каждые **{val} {self.unit_label}** (старт: `{next_run_str}` МСК)."
            else:
                s_acc = target_acc
                r_acc = BANK_ACCOUNT_ID
                dir_label = f"🔴 Списание в Банк со счета `{target_acc}` ({target_user[2]})"
                notif_text = f"🏦 Банк назначил обязательное автосписание с вашего счета: **-{format_money(amount)}** каждые **{val} {self.unit_label}** (старт: `{next_run_str}` МСК)."

            async with db_write_lock:
                await db.execute(
                    '''
                    INSERT INTO auto_payments (owner_did, sender_acc, receiver_acc, amount, interval_val, interval_unit, next_run, is_active, is_bank)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1)
                    ''',
                    (BANK_DISCORD_ID, s_acc, r_acc, amount, val, self.unit, next_run_str)
                )
                await db.commit()

            if target_user[0] and target_user[0] > 0:
                await add_notification(target_user[0], notif_text)

            bank_view = await AdminBankManageView.create()
            embed = await bank_view.build_embed()
            await safe_edit_or_send(
                interaction,
                content=(
                    f"✅ Банковский автоплатеж создан!\n"
                    f"{dir_label}: **{format_money(amount)}** каждые **{val} {self.unit_label}**\n"
                    f"🗓️ Первое исполнение: `{next_run_str}` (МСК){note}"
                ),
                embed=embed,
                view=bank_view
            )
            return

        # --- РЕЖИМ 2: ОБЫЧНЫЙ АВТОПЛАТЕЖ ИГРОКА ---
        user_id = interaction.user.id
        user_data = await get_user_data(user_id)
        if not user_data:
            await safe_send_ephemeral(interaction, "❌ У вас нет счета!")
            return

        if user_data[4] < 0:
            await safe_send_ephemeral(
                interaction,
                f"⛔ Ваш счет заблокирован из-за задолженности (**{format_money(user_data[4])}**)! Вы не можете создавать автоплатежи до погашения долга."
            )
            return

        sender_acc = user_data[3]
        if target_acc == sender_acc:
            await safe_send_ephemeral(interaction, "❌ Нельзя настроить автоплатеж самому себе!")
            return

        async with db_write_lock:
            await db.execute(
                '''
                INSERT INTO auto_payments (owner_did, sender_acc, receiver_acc, amount, interval_val, interval_unit, next_run, is_active, is_bank)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0)
                ''',
                (user_id, sender_acc, target_acc, amount, val, self.unit, next_run_str)
            )
            await db.commit()

        payments = await get_user_autopayments(user_id, sender_acc)
        embed = build_autopayments_embed(payments, sender_acc)
        await safe_edit_or_send(
            interaction,
            content=(
                f"✅ Автоплатеж на `{target_acc}` (**{format_money(amount)}** каждые **{val} {self.unit_label}**) создан!\n"
                f"🗓️ Первое списание: `{next_run_str}` (МСК){note}"
            ),
            embed=embed,
            view=AutoPaymentsListView(user_data, payments)
        )


# --- ИНТЕРАКТИВНЫЙ КОНСТРУКТОР ФИЛЬТРОВ ТРАНЗАКЦИЙ ---

class FilterParamsModal(Modal, title="Дополнительные параметры"):
    def __init__(self, dashboard_view: "TransactionFilterDashboardView"):
        super().__init__()
        self.dashboard = dashboard_view

        self.account_input = TextInput(
            label="ID счета (Отправитель или Получатель)",
            placeholder="Оставьте пустым для всех",
            default=self.dashboard.filters.get("account") or "",
            required=False,
            max_length=10
        )
        self.min_amount = TextInput(
            label="Минимальная сумма",
            placeholder="Например: 100",
            default=str(self.dashboard.filters["min_a"]) if self.dashboard.filters.get("min_a") is not None else "",
            required=False,
            max_length=12
        )
        self.max_amount = TextInput(
            label="Максимальная сумма",
            placeholder="Например: 50000",
            default=str(self.dashboard.filters["max_a"]) if self.dashboard.filters.get("max_a") is not None else "",
            required=False,
            max_length=12
        )
        self.tx_id_input = TextInput(
            label="Точный ID транзакции (#)",
            placeholder="Оставьте пустым для всех",
            default=str(self.dashboard.filters["tx_id"]) if self.dashboard.filters.get("tx_id") is not None else "",
            required=False,
            max_length=10
        )

        self.add_item(self.account_input)
        self.add_item(self.min_amount)
        self.add_item(self.max_amount)
        self.add_item(self.tx_id_input)

    async def on_submit(self, interaction: discord.Interaction):
        acc = self.account_input.value.strip()
        self.dashboard.filters["account"] = acc if acc else None

        min_v = self.min_amount.value.strip()
        self.dashboard.filters["min_a"] = int(min_v) if min_v.isdigit() else None

        max_v = self.max_amount.value.strip()
        self.dashboard.filters["max_a"] = int(max_v) if max_v.isdigit() else None

        tx_v = self.tx_id_input.value.strip().lstrip("#0")
        if self.tx_id_input.value.strip() == "0":
            tx_v = "0"
        self.dashboard.filters["tx_id"] = int(tx_v) if tx_v.isdigit() else None

        await safe_edit_or_send(
            interaction,
            embed=self.dashboard.build_embed(),
            view=self.dashboard
        )


class TransactionFilterDashboardView(View):
    """GUI-панель фильтрации транзакций с выпадающими списками"""
    def __init__(self):
        super().__init__(timeout=300)
        self.filters = {
            "type": "all",
            "period": "all",
            "account": None,
            "min_a": None,
            "max_a": None,
            "tx_id": None
        }

        self.type_select = Select(
            placeholder="📂 Выберите тип операции...",
            options=[
                discord.SelectOption(label="Все типы операций", value="all", emoji="📋", default=True),
                discord.SelectOption(label="Переводы игроков", value="user_transfer", emoji="💸"),
                discord.SelectOption(label="Автоплатежи игроков", value="auto_transfer", emoji="🔄"),
                discord.SelectOption(label="Автоначисления (Банк)", value="bank_auto_deposit", emoji="🏦"),
                discord.SelectOption(label="Автосписания (Банк)", value="bank_auto_withdraw", emoji="🏦"),
                discord.SelectOption(label="Взносы в казну фракции", value="faction_deposit", emoji="📥"),
                discord.SelectOption(label="Снятия из казны фракции", value="faction_withdraw", emoji="📤"),
                discord.SelectOption(label="Зачисления (Админ/Банк)", value="admin_deposit", emoji="💰"),
                discord.SelectOption(label="Списания (Админ/Банк)", value="admin_withdraw", emoji="📉"),
                discord.SelectOption(label="Установка баланса", value="admin_set", emoji="⚙️"),
            ],
            row=0
        )
        self.type_select.callback = self.on_type_change
        self.add_item(self.type_select)

        self.period_select = Select(
            placeholder="📅 Выберите период времени...",
            options=[
                discord.SelectOption(label="За всё время", value="all", emoji="♾️", default=True),
                discord.SelectOption(label="За сегодня (24 часа)", value="today", emoji="🕒"),
                discord.SelectOption(label="За последние 7 дней", value="7d", emoji="📅"),
                discord.SelectOption(label="За последние 30 дней", value="30d", emoji="🗓️"),
            ],
            row=1
        )
        self.period_select.callback = self.on_period_change
        self.add_item(self.period_select)

    def build_embed(self) -> discord.Embed:
        type_names = {
            "all": "Все операции",
            "user_transfer": "💸 Переводы игроков",
            "auto_transfer": "🔄 Автоплатежи игроков",
            "bank_auto_deposit": "🏦 Автоначисления (Банк)",
            "bank_auto_withdraw": "🏦 Автосписания (Банк)",
            "faction_deposit": "📥 Взносы в казну",
            "faction_withdraw": "📤 Снятия из казны",
            "admin_deposit": "💰 Зачисления (Банк)",
            "admin_withdraw": "📉 Списания (Банк)",
            "admin_set": "⚙️ Установка баланса"
        }
        period_names = {
            "all": "За всё время",
            "today": "За последние 24 часа",
            "7d": "За последние 7 дней",
            "30d": "За последние 30 дней"
        }

        embed = discord.Embed(
            title="🔍 Фильтр истории транзакций",
            description="Настройте нужные фильтры через меню ниже и нажмите **«Показать результаты»**.",
            color=discord.Color.blurple()
        )
        embed.add_field(name="📂 Тип операции", value=type_names.get(self.filters["type"], "Все"), inline=True)
        embed.add_field(name="📅 Период", value=period_names.get(self.filters["period"], "За всё время"), inline=True)
        embed.add_field(name="💳 Счет (Отпр./Получ.)", value=f"`{self.filters['account']}`" if self.filters["account"] else "Любой", inline=True)

        sum_str = "Любая"
        if self.filters["min_a"] is not None and self.filters["max_a"] is not None:
            sum_str = f"от {format_money(self.filters['min_a'])} до {format_money(self.filters['max_a'])}"
        elif self.filters["min_a"] is not None:
            sum_str = f"от {format_money(self.filters['min_a'])}"
        elif self.filters["max_a"] is not None:
            sum_str = f"до {format_money(self.filters['max_a'])}"

        embed.add_field(name="💰 Сумма", value=sum_str, inline=True)
        embed.add_field(name="🔢 ID транзакции", value=f"`#{self.filters['tx_id']}`" if self.filters["tx_id"] is not None else "Любой", inline=True)
        return embed

    async def on_type_change(self, interaction: discord.Interaction):
        val = self.type_select.values[0]
        self.filters["type"] = val
        for opt in self.type_select.options:
            opt.default = (opt.value == val)
        await safe_edit_or_send(interaction, embed=self.build_embed(), view=self)

    async def on_period_change(self, interaction: discord.Interaction):
        val = self.period_select.values[0]
        self.filters["period"] = val
        for opt in self.period_select.options:
            opt.default = (opt.value == val)
        await safe_edit_or_send(interaction, embed=self.build_embed(), view=self)

    @button(label="⚙️ Счет / Сумма / ID", style=ButtonStyle.secondary, row=2)
    async def params_btn(self, interaction: discord.Interaction, button: Button):
        await safe_send_modal(interaction, FilterParamsModal(self))

    @button(label="🗑️ Сбросить", style=ButtonStyle.gray, row=2)
    async def reset_btn(self, interaction: discord.Interaction, button: Button):
        self.filters = {
            "type": "all",
            "period": "all",
            "account": None,
            "min_a": None,
            "max_a": None,
            "tx_id": None
        }
        for opt in self.type_select.options:
            opt.default = (opt.value == "all")
        for opt in self.period_select.options:
            opt.default = (opt.value == "all")
        await safe_edit_or_send(interaction, embed=self.build_embed(), view=self)

    @button(label="🔎 Показать результаты", style=ButtonStyle.green, row=2)
    async def search_btn(self, interaction: discord.Interaction, button: Button):
        query = "SELECT * FROM transactions WHERE 1=1"
        params = []

        if self.filters["type"] != "all":
            query += " AND type = ?"
            params.append(self.filters["type"])

        now = get_msk_now()
        if self.filters["period"] == "today":
            dt_from = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            query += " AND created_at >= ?"
            params.append(dt_from)
        elif self.filters["period"] == "7d":
            dt_from = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            query += " AND created_at >= ?"
            params.append(dt_from)
        elif self.filters["period"] == "30d":
            dt_from = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            query += " AND created_at >= ?"
            params.append(dt_from)

        if self.filters["account"]:
            query += " AND (sender_acc = ? OR receiver_acc = ?)"
            params.extend([self.filters["account"], self.filters["account"]])

        if self.filters["min_a"] is not None:
            query += " AND amount >= ?"
            params.append(self.filters["min_a"])

        if self.filters["max_a"] is not None:
            query += " AND amount <= ?"
            params.append(self.filters["max_a"])

        if self.filters["tx_id"] is not None:
            query += " AND id = ?"
            params.append(self.filters["tx_id"])

        query += " ORDER BY id DESC LIMIT 200"

        db = await get_db()
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await safe_send_ephemeral(interaction, "❌ По заданным фильтрам транзакций не найдено.")
            return

        total_pages = (len(rows) - 1) // 10 + 1
        await show_transaction_page(interaction, rows, 0, total_pages, dashboard_view=self)

    @button(label="🔙 В админ-панель", style=ButtonStyle.red, row=2)
    async def back_admin_btn(self, interaction: discord.Interaction, button: Button):
        view = await AdminPanelView.create()
        embed = await view.build_embed()
        await safe_edit_or_send(interaction, content=None, embed=embed, view=view)


# --- ПАГИНАЦИЯ ТРАНЗАКЦИЙ ---

class TransactionPaginationView(View):
    def __init__(self, full_data, current_page: int, total_pages: int, dashboard_view: TransactionFilterDashboardView | None = None):
        super().__init__(timeout=180)
        self.full_data = full_data
        self.page = current_page
        self.total = total_pages
        self.dashboard_view = dashboard_view
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total - 1

    @button(label="⬅️", style=ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: Button):
        await show_transaction_page(interaction, self.full_data, self.page - 1, self.total, self.dashboard_view)

    @button(label="➡️", style=ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: Button):
        await show_transaction_page(interaction, self.full_data, self.page + 1, self.total, self.dashboard_view)

    @button(label="🔍 К фильтрам", style=ButtonStyle.primary, row=0)
    async def back_to_filters_btn(self, interaction: discord.Interaction, button: Button):
        view = self.dashboard_view or TransactionFilterDashboardView()
        await safe_edit_or_send(interaction, embed=view.build_embed(), view=view)

    @button(label="📥 Скачать CSV", style=ButtonStyle.green, row=0)
    async def export_btn(self, interaction: discord.Interaction, button: Button):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Дата (МСК)", "Тип", "Отправитель", "Получатель", "Сумма"])
        for tx in self.full_data:
            writer.writerow([sanitize_csv_cell(col) for col in tx])
        file = discord.File(
            fp=io.BytesIO(output.getvalue().encode('utf-8-sig')),
            filename="transactions.csv"
        )
        await safe_send_ephemeral(interaction, content="✅ Выгрузка готова:", file=file)


async def show_transaction_page(
    interaction: discord.Interaction,
    data,
    page: int,
    total_pages: int,
    dashboard_view: TransactionFilterDashboardView | None = None
):
    start, end = page * 10, page * 10 + 10
    items = data[start:end]
    desc = ""
    for tx in items:
        tx_id, created_at, tx_type, s_acc, r_acc, amount = tx
        label_info = TX_TYPE_LABELS.get(tx_type, (f"⚙️ {tx_type}", tx_type))
        desc += (
            f"**#{tx_id:06d}** | `{created_at}` | {label_info[0]}\n"
            f"От: `{s_acc or 'Банк'}` ➔ Кому: `{r_acc or 'Банк'}` | Сумма: **{format_money(amount)}**\n\n"
        )

    embed = discord.Embed(
        title=f"📜 История транзакций (найдено: {len(data)})",
        description=desc,
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Страница {page + 1} из {total_pages}")
    view = TransactionPaginationView(data, page, total_pages, dashboard_view)

    await safe_edit_or_send(interaction, embed=embed, view=view)


# --- ИНТЕРФЕЙСЫ ПОЛЬЗОВАТЕЛЯ ---

def build_autopayments_embed(payments, user_acc: str) -> discord.Embed:
    bank_payments = [p for p in payments if len(p) > 9 and p[9] == 1]
    user_payments = [p for p in payments if len(p) <= 9 or p[9] == 0]

    desc = ""
    if bank_payments:
        desc += "🏦 **Обязательные автоплатежи Центрального Банка** *(нельзя отменить самостоятельно)*:\n"
        for p in bank_payments:
            u_label = UNIT_LABELS.get(p[6], p[6])
            if p[3] == user_acc:
                desc += (
                    f"🟢 **Начисление от Банка**: `+{format_money(p[4])}` каждые **{p[5]} {u_label}**\n"
                    f"⏱️ Следующее: `{p[7]}` (МСК)\n\n"
                )
            else:
                desc += (
                    f"🔴 **Списание в Банк**: `-{format_money(p[4])}` каждые **{p[5]} {u_label}**\n"
                    f"⏱️ Следующее: `{p[7]}` (МСК)\n\n"
                )

    desc += "💸 **Ваши личные автоплатежи:**\n"
    if not user_payments:
        desc += "У вас пока нет личных автоплатежей.\nВыберите периодичность в меню ниже, чтобы создать новый.\n"
    else:
        for p in user_payments:
            u_label = UNIT_LABELS.get(p[6], p[6])
            target_label = f"`{p[3]}` (Казна)" if is_treasury_account(p[3]) else f"`{p[3]}`"
            desc += (
                f"🔹 Кому: {target_label} | Сумма: **{format_money(p[4])}** | Каждые **{p[5]} {u_label}**\n"
                f"⏱️ Следующее списание: `{p[7]}` (МСК)\n\n"
            )

    color = discord.Color.orange() if payments else discord.Color.greyple()
    return discord.Embed(title="🔄 Управление автоплатежами", description=desc, color=color)


class NotificationsView(View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    @button(label="🗑️ Очистить уведомления", style=ButtonStyle.secondary)
    async def clear_btn(self, interaction: discord.Interaction, button: Button):
        user_notifications[self.user_id] = []
        user_data = await get_user_data(self.user_id)
        if not user_data:
            return
        is_leader = check_is_leader(interaction)
        await safe_edit_or_send(
            interaction,
            content=None,
            embed=build_profile_embed(user_data),
            view=UserProfileView(user_data, is_leader=is_leader)
        )

    @button(label="🔙 В профиль", style=ButtonStyle.red)
    async def back_btn(self, interaction: discord.Interaction, button: Button):
        user_data = await get_user_data(self.user_id)
        if not user_data:
            return
        is_leader = check_is_leader(interaction)
        await safe_edit_or_send(
            interaction,
            content=None,
            embed=build_profile_embed(user_data),
            view=UserProfileView(user_data, is_leader=is_leader)
        )


class AutoPaymentBuilderView(View):
    """Мини-календарь и выбор времени в GUI перед открытием модального окна автоплатежа"""
    def __init__(
        self,
        user_data,
        unit: str = "days",
        unit_label: str = "дн.",
        prefill_account: str = "",
        is_bank_mode: bool = False,
        bank_direction: str = "withdraw"
    ):
        super().__init__(timeout=300)
        self.user_data = user_data
        self.unit = unit
        self.unit_label = unit_label
        self.prefill_account = prefill_account
        self.is_bank_mode = is_bank_mode
        self.bank_direction = bank_direction
        self.selected_date: str = ""
        self.selected_time: str = ""

        now = get_msk_now()

        if self.is_bank_mode:
            self.dir_select = Select(
                placeholder="🏦 Тип банковского автоплатежа...",
                options=[
                    discord.SelectOption(
                        label="🔴 Списание со счета в пользу Банка (налог/кредит)",
                        value="withdraw",
                        emoji="📉",
                        default=(self.bank_direction == "withdraw")
                    ),
                    discord.SelectOption(
                        label="🟢 Начисление со счета Банка (зарплата/субсидия)",
                        value="deposit",
                        emoji="💰",
                        default=(self.bank_direction == "deposit")
                    ),
                ],
                row=0
            )
            self.dir_select.callback = self.on_dir_change
            self.add_item(self.dir_select)

        unit_row = 1 if self.is_bank_mode else 0
        self.unit_select = Select(
            placeholder="⏱️ Выберите единицу периодичности...",
            options=[
                discord.SelectOption(label="Каждые N минут", value="minutes", emoji="⏱️", default=(self.unit == "minutes")),
                discord.SelectOption(label="Каждые N часов", value="hours", emoji="🕐", default=(self.unit == "hours")),
                discord.SelectOption(label="Каждые N дней", value="days", emoji="📅", default=(self.unit == "days")),
                discord.SelectOption(label="Каждые N недель", value="weeks", emoji="🗓️", default=(self.unit == "weeks")),
                discord.SelectOption(label="Каждые N месяцев (30 дн.)", value="months", emoji="📆", default=(self.unit == "months")),
                discord.SelectOption(label="Каждые N секунд (тест)", value="seconds", emoji="⚡", default=(self.unit == "seconds")),
            ],
            row=unit_row
        )
        self.unit_select.callback = self.on_unit_change
        self.add_item(self.unit_select)

        date_options = [
            discord.SelectOption(
                label="Авто (Сегодня / Завтра если время прошло)",
                description=f"По умолчанию ({now.strftime('%d.%m.%Y')})",
                value="today_auto",
                emoji="📅",
                default=True
            )
        ]
        for i in range(0, 14):
            d = now.date() + datetime.timedelta(days=i)
            wd = RU_WEEKDAYS[d.weekday()]
            d_str = d.strftime("%d.%m.%Y")
            if i == 0:
                lbl = f"Сегодня — {d_str} ({wd})"
            elif i == 1:
                lbl = f"Завтра — {d_str} ({wd})"
            elif i == 2:
                lbl = f"Послезавтра — {d_str} ({wd})"
            else:
                lbl = f"{d_str} ({wd})"
            date_options.append(discord.SelectOption(label=lbl, value=d_str, emoji="🗓️"))

        date_options.append(
            discord.SelectOption(
                label="Ввести другую дату вручную...",
                description="Вы сможете вписать любую дату в следующем окне",
                value="manual",
                emoji="⌨️"
            )
        )

        date_row = 2 if self.is_bank_mode else 1
        self.date_select = Select(
            placeholder="📅 Выберите дату первого исполнения...",
            options=date_options,
            row=date_row
        )
        self.date_select.callback = self.on_date_change
        self.add_item(self.date_select)

        time_slots = [
            ("Сейчас (текущее время)", "", "⚡"),
            ("00:00 (Полночь)", "00:00", "🕛"),
            ("06:00 (Утро)", "06:00", "🌅"),
            ("09:00", "09:00", "🕘"),
            ("10:00", "10:00", "🕙"),
            ("12:00 (Полдень)", "12:00", "🕛"),
            ("14:00", "14:00", "🕑"),
            ("15:00", "15:00", "🕒"),
            ("16:00", "16:00", "🕓"),
            ("18:00 (Вечер)", "18:00", "🕕"),
            ("19:00", "19:00", "🕖"),
            ("20:00", "20:00", "🕗"),
            ("21:00", "21:00", "🕘"),
            ("22:00", "22:00", "🕙"),
            ("23:00", "23:00", "🕚"),
            ("Ввести точное время вручную (ЧЧ:ММ)...", "manual", "⌨️"),
        ]
        time_options = [
            discord.SelectOption(
                label=lbl,
                value=val if val else "now",
                emoji=emj,
                default=(val == "")
            )
            for lbl, val, emj in time_slots
        ]

        time_row = 3 if self.is_bank_mode else 2
        self.time_select = Select(
            placeholder="🕒 Выберите время первого исполнения (МСК)...",
            options=time_options,
            row=time_row
        )
        self.time_select.callback = self.on_time_change
        self.add_item(self.time_select)

        btn_row = 4 if self.is_bank_mode else 3
        open_btn = Button(label="✍️ Ввести счет, сумму и интервал", style=ButtonStyle.green, row=btn_row)
        open_btn.callback = self.open_modal_btn
        self.add_item(open_btn)

        back_btn = Button(label="🔙 Назад", style=ButtonStyle.red, row=btn_row)
        back_btn.callback = self.back_to_list_btn
        self.add_item(back_btn)

    def build_embed(self) -> discord.Embed:
        now = get_msk_now()
        d_display = self.selected_date if self.selected_date else f"Сегодня ({now.strftime('%d.%m.%Y')}) / Авто"
        t_display = self.selected_time if self.selected_time else f"Сейчас ({now.strftime('%H:%M')} МСК)"

        if self.is_bank_mode:
            title = "🏦 Конструктор автоплатежа Центрального Банка"
            dir_str = "🔴 Списание со счета в Банк" if self.bank_direction == "withdraw" else "🟢 Начисление со счета Банка"
            color = discord.Color.gold()
        else:
            title = f"🗓️ Настройка автоплатежа (Интервал: в {self.unit_label})"
            dir_str = "💸 Личный автоплатеж"
            color = discord.Color.blurple()

        embed = discord.Embed(
            title=title,
            description=(
                "Настройте параметры в выпадающих списках ниже, "
                "затем нажмите зеленую кнопку **«Ввести счет, сумму и интервал»**."
            ),
            color=color
        )
        embed.add_field(name="📂 Тип", value=dir_str, inline=True)
        embed.add_field(name="⏱️ Периодичность", value=f"Каждые **N {self.unit_label}**", inline=True)
        if self.prefill_account:
            embed.add_field(name="💳 Целевой счет", value=f"`{self.prefill_account}`", inline=True)
        embed.add_field(name="📅 Дата 1-го исполнения", value=f"`{d_display}`", inline=True)
        embed.add_field(name="🕒 Время 1-го исполнения", value=f"`{t_display}`", inline=True)
        return embed

    async def on_dir_change(self, interaction: discord.Interaction):
        val = self.dir_select.values[0]
        self.bank_direction = val
        for opt in self.dir_select.options:
            opt.default = (opt.value == val)
        await safe_edit_or_send(interaction, content=None, embed=self.build_embed(), view=self)

    async def on_unit_change(self, interaction: discord.Interaction):
        val = self.unit_select.values[0]
        self.unit = val
        self.unit_label = UNIT_LABELS.get(val, val)
        for opt in self.unit_select.options:
            opt.default = (opt.value == val)
        await safe_edit_or_send(interaction, content=None, embed=self.build_embed(), view=self)

    async def on_date_change(self, interaction: discord.Interaction):
        val = self.date_select.values[0]
        self.selected_date = "" if val in ("today_auto", "manual") else val
        for opt in self.date_select.options:
            opt.default = (opt.value == val)
        await safe_edit_or_send(interaction, content=None, embed=self.build_embed(), view=self)

    async def on_time_change(self, interaction: discord.Interaction):
        val = self.time_select.values[0]
        self.selected_time = "" if val in ("now", "manual") else val
        for opt in self.time_select.options:
            opt.default = (opt.value == val)
        await safe_edit_or_send(interaction, content=None, embed=self.build_embed(), view=self)

    async def open_modal_btn(self, interaction: discord.Interaction):
        await safe_send_modal(
            interaction,
            AutoPaymentStepModal(
                self.unit,
                self.unit_label,
                prefill_date=self.selected_date,
                prefill_time=self.selected_time,
                prefill_account=self.prefill_account,
                is_bank_mode=self.is_bank_mode,
                bank_direction=self.bank_direction
            )
        )

    async def back_to_list_btn(self, interaction: discord.Interaction):
        if self.is_bank_mode:
            bank_view = await AdminBankManageView.create()
            embed = await bank_view.build_embed()
            await safe_edit_or_send(interaction, content=None, embed=embed, view=bank_view)
        else:
            payments = await get_user_autopayments(self.user_data[0], self.user_data[3])
            embed = build_autopayments_embed(payments, self.user_data[3])
            await safe_edit_or_send(
                interaction,
                content=None,
                embed=embed,
                view=AutoPaymentsListView(self.user_data, payments)
            )


class AutoPaymentsListView(View):
    def __init__(self, user_data, payments_list):
        super().__init__(timeout=300)
        self.user_data = user_data
        self.payments = payments_list

        unit_select = Select(
            placeholder="➕ Создать личный автоплатеж: выберите периодичность...",
            options=[
                discord.SelectOption(label="Каждые N минут", value="minutes", emoji="⏱️"),
                discord.SelectOption(label="Каждые N часов", value="hours", emoji="🕐"),
                discord.SelectOption(label="Каждые N дней", value="days", emoji="📅"),
                discord.SelectOption(label="Каждые N недель", value="weeks", emoji="🗓️"),
                discord.SelectOption(label="Каждые N месяцев (30 дн.)", value="months", emoji="📆"),
                discord.SelectOption(label="Каждые N секунд (тест)", value="seconds", emoji="⚡"),
            ],
            row=0
        )
        unit_select.callback = self.create_unit_selected
        self.add_item(unit_select)

        deletable_payments = [p for p in payments_list if len(p) <= 9 or p[9] == 0]
        if deletable_payments:
            options = []
            for p in deletable_payments[:25]:
                u_label = UNIT_LABELS.get(p[6], p[6])
                label = f"На счет {p[3]} ({format_money(p[4])} монет)"
                desc = f"Каждые {p[5]} {u_label} | След: {p[7][:16]}"
                options.append(discord.SelectOption(label=label, description=desc, value=str(p[0]), emoji="🗑️"))

            delete_select = Select(
                placeholder="🗑️ Выберите личный автоплатеж для удаления...",
                options=options,
                row=1
            )
            delete_select.callback = self.delete_callback
            self.add_item(delete_select)

        back_btn = Button(label="🔙 В профиль", style=ButtonStyle.red, row=2)
        back_btn.callback = self.back_callback
        self.add_item(back_btn)

    async def create_unit_selected(self, interaction: discord.Interaction):
        if self.user_data[4] < 0:
            await safe_send_ephemeral(
                interaction,
                f"⛔ Ваш счет заблокирован из-за задолженности (**{format_money(self.user_data[4])}**)! Погасите долг для создания автоплатежей."
            )
            return
        unit = interaction.data['values'][0]
        unit_label = UNIT_LABELS.get(unit, unit)
        builder_view = AutoPaymentBuilderView(self.user_data, unit, unit_label)
        await safe_edit_or_send(
            interaction,
            content=None,
            embed=builder_view.build_embed(),
            view=builder_view
        )

    async def back_callback(self, interaction: discord.Interaction):
        fresh_data = await get_user_data(self.user_data[0]) or self.user_data
        is_leader = check_is_leader(interaction)
        await safe_edit_or_send(
            interaction,
            content=None,
            embed=build_profile_embed(fresh_data),
            view=UserProfileView(fresh_data, is_leader=is_leader)
        )

    async def delete_callback(self, interaction: discord.Interaction):
        if not interaction.data.get('values'):
            return
        try:
            ap_id = int(interaction.data['values'][0])
        except ValueError:
            await safe_send_ephemeral(interaction, "❌ Ошибка ID.")
            return

        db = await get_db()
        async with db_write_lock:
            await db.execute(
                "DELETE FROM auto_payments WHERE id = ? AND owner_did = ? AND COALESCE(is_bank, 0) = 0",
                (ap_id, self.user_data[0])
            )
            await db.commit()

        new_list = await get_user_autopayments(self.user_data[0], self.user_data[3])
        embed = build_autopayments_embed(new_list, self.user_data[3])
        await safe_edit_or_send(
            interaction,
            content="🗑️ Личный автоплатеж удален.",
            embed=embed,
            view=AutoPaymentsListView(self.user_data, new_list)
        )


class FactionView(View):
    """Меню фракции с казной (пополнение для всех, снятие для Лидера), автоплатежом в казну и быстрым переводом участникам"""
    def __init__(self, user_data, members, treasury_data, is_leader: bool = False):
        super().__init__(timeout=600)
        self.user_data = user_data
        self.prefix = user_data[5] if len(user_data) > 5 and user_data[5] else str(user_data[3])[:2]
        self.treasury_data = treasury_data
        self.treasury_acc = get_treasury_account_id(self.prefix)
        self.is_leader = is_leader
        self.members = [m for m in members if m[0] != user_data[0] and m[0] > 0]
        is_user_blocked = user_data[4] < 0

        if self.members:
            options = []
            for m in self.members[:25]:
                options.append(
                    discord.SelectOption(
                        label=f"{m[2]} ({m[3]})",
                        description=f"Discord: {m[1]}",
                        value=str(m[3]),
                        emoji="💸"
                    )
                )
            member_select = Select(
                placeholder="💸 Быстрый перевод участнику фракции...",
                options=options,
                row=0,
                disabled=is_user_blocked
            )
            member_select.callback = self.quick_transfer_cb
            self.add_item(member_select)

        dep_btn = Button(label="📥 Пополнить казну", style=ButtonStyle.green, row=1, disabled=is_user_blocked)
        dep_btn.callback = self.deposit_treasury_cb
        self.add_item(dep_btn)

        if self.is_leader:
            with_btn = Button(label="📤 Снять из казны", style=ButtonStyle.primary, row=1)
            with_btn.callback = self.withdraw_treasury_cb
            self.add_item(with_btn)

        auto_fac_btn = Button(label="🔄 Автоплатеж в казну", style=ButtonStyle.secondary, row=1, disabled=is_user_blocked)
        auto_fac_btn.callback = self.auto_treasury_cb
        self.add_item(auto_fac_btn)

        back_btn = Button(label="🔙 В профиль", style=ButtonStyle.red, row=2)
        back_btn.callback = self.back_cb
        self.add_item(back_btn)

    def build_embed(self, all_members) -> discord.Embed:
        t_bal = self.treasury_data[4] if self.treasury_data else 0
        t_status = f"⛔ **{format_money(t_bal)}** (Заблокирована / Долг)" if t_bal < 0 else f"💰 **{format_money(t_bal)}**"

        embed = discord.Embed(
            title=f"🛡️ Фракция #{self.prefix} — Общая Казна и Состав",
            description=(
                f"🏦 **Счет казны фракции:** `{self.treasury_acc}`\n"
                f"💎 **Баланс казны:** {t_status}\n"
                f"💳 **Ваш личный баланс:** **{format_money(self.user_data[4])}**\n\n"
                f"Каждый участник может пополнить казну или настроить автоплатеж в казну. "
                f"{'Вы как **Лидер Фракции** также можете снимать средства из казны.' if self.is_leader else ''}"
            ),
            color=discord.Color.blue() if t_bal >= 0 else discord.Color.dark_red()
        )

        real_members = [m for m in all_members if m[0] > 0]
        for m in real_members[:24]:
            me_tag = " *(Вы)*" if m[0] == self.user_data[0] else ""
            embed.add_field(
                name=f"👤 {m[2]}{me_tag}",
                value=f"💳 `{m[3]}`\n📛 {m[1]}",
                inline=True
            )
        return embed

    async def refresh_view(self, interaction: discord.Interaction, status_text: str | None = None):
        fresh_user = await get_user_data(interaction.user.id) or self.user_data
        fresh_treasury = await get_faction_treasury_data(self.prefix)
        members = await get_faction_members(self.prefix)
        is_leader = check_is_leader(interaction)

        new_view = FactionView(fresh_user, members, fresh_treasury, is_leader=is_leader)
        embed = new_view.build_embed(members)
        await safe_edit_or_send(interaction, content=status_text, embed=embed, view=new_view)

    async def deposit_treasury_cb(self, interaction: discord.Interaction):
        if self.user_data[4] < 0:
            await safe_send_ephemeral(
                interaction,
                f"⛔ Ваш счет заблокирован из-за задолженности (**{format_money(self.user_data[4])}**)! Сначала погасите долг."
            )
            return
        await safe_send_modal(interaction, FactionTreasuryModal("deposit", self.prefix, self))

    async def withdraw_treasury_cb(self, interaction: discord.Interaction):
        if not check_is_leader(interaction):
            await safe_send_ephemeral(interaction, "❌ Снимать средства из казны может только Лидер Фракции!")
            return
        await safe_send_modal(interaction, FactionTreasuryModal("withdraw", self.prefix, self))

    async def auto_treasury_cb(self, interaction: discord.Interaction):
        if self.user_data[4] < 0:
            await safe_send_ephemeral(
                interaction,
                f"⛔ Ваш счет заблокирован из-за задолженности (**{format_money(self.user_data[4])}**)! Вы не можете создавать автоплатежи."
            )
            return
        builder = AutoPaymentBuilderView(
            self.user_data,
            unit="days",
            unit_label="дн.",
            prefill_account=self.treasury_acc
        )
        await safe_edit_or_send(
            interaction,
            content=f"🛡️ *Счет казны вашей фракции (`{self.treasury_acc}`) автоматически подставлен в качестве получателя.*",
            embed=builder.build_embed(),
            view=builder
        )

    async def quick_transfer_cb(self, interaction: discord.Interaction):
        if self.user_data[4] < 0:
            await safe_send_ephemeral(
                interaction,
                f"⛔ Ваш счет заблокирован из-за задолженности (**{format_money(self.user_data[4])}**)! Переводы недоступны."
            )
            return
        target_acc = interaction.data['values'][0]
        await safe_send_modal(interaction, TransferModal(prefill_target=target_acc))

    async def back_cb(self, interaction: discord.Interaction):
        fresh_data = await get_user_data(self.user_data[0]) or self.user_data
        is_leader = check_is_leader(interaction)
        await safe_edit_or_send(
            interaction,
            content=None,
            embed=build_profile_embed(fresh_data),
            view=UserProfileView(fresh_data, is_leader=is_leader)
        )


class UserProfileView(View):
    def __init__(self, user_data, is_leader: bool = False):
        super().__init__(timeout=900)
        self.user_data = user_data
        is_blocked = user_data[4] < 0

        tr_btn = Button(label="💸 Перевод", style=ButtonStyle.blurple, row=0, disabled=is_blocked)
        tr_btn.callback = self.transfer_callback
        self.add_item(tr_btn)

        auto_btn = Button(label="🔄 Автоплатежи", style=ButtonStyle.secondary, row=0)
        auto_btn.callback = self.auto_callback
        self.add_item(auto_btn)

        fac_btn = Button(label="👥 Фракция и Казна", style=ButtonStyle.green, row=0)
        fac_btn.callback = self.faction_callback
        self.add_item(fac_btn)

        notif_count = len([n for n in user_notifications.get(self.user_data[0], []) if not n['read']])
        label = f"🔔 Уведомления ({notif_count})" if notif_count > 0 else "🔔 Уведомления"
        notif_style = ButtonStyle.primary if notif_count > 0 else ButtonStyle.gray
        notif_btn = Button(label=label, style=notif_style, row=1)
        notif_btn.callback = self.notif_callback
        self.add_item(notif_btn)

        refresh_btn = Button(label="🔄 Обновить", style=ButtonStyle.gray, row=1)
        refresh_btn.callback = self.refresh_callback
        self.add_item(refresh_btn)

    async def refresh_callback(self, interaction: discord.Interaction):
        fresh_data = await get_user_data(interaction.user.id)
        if not fresh_data:
            return
        self.user_data = fresh_data
        is_leader = check_is_leader(interaction)
        await safe_edit_or_send(
            interaction,
            content=None,
            embed=build_profile_embed(fresh_data),
            view=UserProfileView(fresh_data, is_leader=is_leader)
        )

    async def transfer_callback(self, interaction: discord.Interaction):
        if self.user_data[4] < 0:
            await safe_send_ephemeral(
                interaction,
                f"⛔ Ваш счет заблокирован из-за задолженности (**{format_money(self.user_data[4])}**)! Пополните баланс для разблокировки."
            )
            return
        await safe_send_modal(interaction, TransferModal())

    async def auto_callback(self, interaction: discord.Interaction):
        fresh_data = await get_user_data(interaction.user.id) or self.user_data
        payments = await get_user_autopayments(fresh_data[0], fresh_data[3])
        embed = build_autopayments_embed(payments, fresh_data[3])
        await safe_edit_or_send(
            interaction,
            content=None,
            embed=embed,
            view=AutoPaymentsListView(fresh_data, payments)
        )

    async def notif_callback(self, interaction: discord.Interaction):
        notes = user_notifications.get(self.user_data[0], [])
        if not notes:
            await safe_send_ephemeral(interaction, "🔕 У вас пока нет уведомлений.")
            return

        desc = ""
        for n in notes:
            status = "🔵" if not n['read'] else "⚪"
            desc += f"{status} `[{n['time']}]` {n['msg']}\n"

        user_notifications[self.user_data[0]] = [
            {"msg": x['msg'], "read": True, "time": x['time']} for x in notes
        ]

        embed = discord.Embed(title="🔔 Ваши уведомления", description=desc, color=discord.Color.blue())
        await safe_edit_or_send(
            interaction,
            content=None,
            embed=embed,
            view=NotificationsView(self.user_data[0])
        )

    async def faction_callback(self, interaction: discord.Interaction):
        fresh_user = await get_user_data(interaction.user.id) or self.user_data
        my_prefix = fresh_user[5] if len(fresh_user) > 5 and fresh_user[5] else str(fresh_user[3])[:2]
        treasury_data = await get_faction_treasury_data(my_prefix)
        members = await get_faction_members(my_prefix)
        is_leader = check_is_leader(interaction)

        fac_view = FactionView(fresh_user, members, treasury_data, is_leader=is_leader)
        embed = fac_view.build_embed(members)

        await safe_edit_or_send(
            interaction,
            content=None,
            embed=embed,
            view=fac_view
        )


# --- АДМИН-ПАНЕЛЬ: УПРАВЛЕНИЕ ЦЕНТРАЛЬНЫМ БАНКОМ И АВТОПЛАТЕЖАМИ БАНКА ---

class AdminBankManageView(View):
    """Панель управления счетом самого Банка (0000000000) и всеми банковскими автоплатежами"""
    def __init__(self, bank_payments):
        super().__init__(timeout=600)
        self.bank_payments = bank_payments

        if bank_payments:
            options = []
            for p in bank_payments[:25]:
                pay_id, owner_did, s_acc, r_acc, amount, i_val, i_unit, next_run_str, is_active, is_bank = p
                u_label = UNIT_LABELS.get(i_unit, i_unit)
                if s_acc == BANK_ACCOUNT_ID:
                    lbl = f"[Начисление] ➔ {r_acc} (+{format_money(amount)})"
                    emj = "🟢"
                else:
                    lbl = f"[Списание] с {s_acc} (-{format_money(amount)})"
                    emj = "🔴"
                desc = f"Каждые {i_val} {u_label} | След: {next_run_str[:16]}"
                options.append(discord.SelectOption(label=lbl[:100], description=desc[:100], value=str(pay_id), emoji=emj))

            del_select = Select(
                placeholder="🗑️ Выберите автоплатеж Банка для отмены...",
                options=options,
                row=0
            )
            del_select.callback = self.delete_bank_ap_cb
            self.add_item(del_select)

    @classmethod
    async def create(cls):
        db = await get_db()
        async with db.execute(
            '''
            SELECT id, owner_did, sender_acc, receiver_acc, amount, interval_val, interval_unit, next_run, is_active, COALESCE(is_bank, 0)
            FROM auto_payments
            WHERE is_active = 1 AND COALESCE(is_bank, 0) = 1
            ORDER BY next_run ASC
            '''
        ) as cur:
            rows = await cur.fetchall()
        return cls(bank_payments=rows)

    async def build_embed(self) -> discord.Embed:
        bank_bal = await get_bank_balance()
        desc = (
            f"💳 **Номер счета Банка:** `{BANK_ACCOUNT_ID}`\n"
            f"💰 **Капитал Банка:** **{format_money(bank_bal)}**\n\n"
            f"**Активные автоплатежи от имени Банка ({len(self.bank_payments)}):**\n"
        )
        if not self.bank_payments:
            desc += "*Активных банковских автоплатежей пока нет.*\n"
        else:
            for p in self.bank_payments[:15]:
                pay_id, owner_did, s_acc, r_acc, amount, i_val, i_unit, next_run_str, is_active, is_bank = p
                u_label = UNIT_LABELS.get(i_unit, i_unit)
                if s_acc == BANK_ACCOUNT_ID:
                    desc += f"🟢 **#{pay_id}** | Начисление на `{r_acc}`: **+{format_money(amount)}** каждые **{i_val} {u_label}** (След: `{next_run_str}`)\n"
                else:
                    desc += f"🔴 **#{pay_id}** | Списание со счета `{s_acc}`: **-{format_money(amount)}** каждые **{i_val} {u_label}** (След: `{next_run_str}`)\n"

        embed = discord.Embed(
            title="🏦 Центральный Банк — Казна и Автоплатежи",
            description=desc,
            color=discord.Color.gold()
        )
        return embed

    async def delete_bank_ap_cb(self, interaction: discord.Interaction):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        ap_id = int(interaction.data['values'][0])
        db = await get_db()
        async with db_write_lock:
            await db.execute("DELETE FROM auto_payments WHERE id = ? AND COALESCE(is_bank, 0) = 1", (ap_id,))
            await db.commit()

        new_view = await AdminBankManageView.create()
        embed = await new_view.build_embed()
        await safe_edit_or_send(
            interaction,
            content=f"🗑️ Банковский автоплатеж **#{ap_id}** отменен сотрудником банка.",
            embed=embed,
            view=new_view
        )

    @button(label="➕ Создать автоплатеж Банка", style=ButtonStyle.blurple, row=1)
    async def create_bank_ap_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        builder = AutoPaymentBuilderView(
            user_data=None,
            unit="days",
            unit_label="дн.",
            is_bank_mode=True,
            bank_direction="withdraw"
        )
        await safe_edit_or_send(interaction, content=None, embed=builder.build_embed(), view=builder)

    @button(label="➕ Пополнить Банк", style=ButtonStyle.green, row=2)
    async def bank_add_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        await safe_send_modal(interaction, ManageBalanceModal("add", prefill_account=BANK_ACCOUNT_ID))

    @button(label="➖ Списать из Банка", style=ButtonStyle.red, row=2)
    async def bank_remove_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        await safe_send_modal(interaction, ManageBalanceModal("remove", prefill_account=BANK_ACCOUNT_ID))

    @button(label="⚙️ Установить баланс Банка", style=ButtonStyle.secondary, row=2)
    async def bank_set_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        await safe_send_modal(interaction, ManageBalanceModal("set", prefill_account=BANK_ACCOUNT_ID))

    @button(label="🔙 В админ-панель", style=ButtonStyle.gray, row=3)
    async def back_admin_btn(self, interaction: discord.Interaction, button: Button):
        view = await AdminPanelView.create()
        embed = await view.build_embed()
        await safe_edit_or_send(interaction, content=None, embed=embed, view=view)


# --- АДМИН-ПАНЕЛЬ: УПРАВЛЕНИЕ КОНКРЕТНЫМ ИГРОКОМ / СЧЕТОМ ---

class AdminUserManageView(View):
    """Карточка управления конкретным игроком или казной в админ-панели"""
    def __init__(self, target_user_data):
        super().__init__(timeout=300)
        self.u_data = target_user_data

    @button(label="➕ Зачислить", style=ButtonStyle.green, row=0)
    async def add_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        await safe_send_modal(interaction, ManageBalanceModal("add", prefill_account=self.u_data[3]))

    @button(label="➖ Списать", style=ButtonStyle.red, row=0)
    async def remove_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        await safe_send_modal(interaction, ManageBalanceModal("remove", prefill_account=self.u_data[3]))

    @button(label="⚙️ Установить", style=ButtonStyle.blurple, row=0)
    async def set_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        await safe_send_modal(interaction, ManageBalanceModal("set", prefill_account=self.u_data[3]))

    @button(label="🏦 Назначить автоплатеж Банка", style=ButtonStyle.primary, row=1)
    async def bank_ap_for_user_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        builder = AutoPaymentBuilderView(
            user_data=None,
            unit="days",
            unit_label="дн.",
            prefill_account=self.u_data[3],
            is_bank_mode=True,
            bank_direction="withdraw"
        )
        await safe_edit_or_send(
            interaction,
            content=f"🏦 *Настройка банковского автоплатежа для счета `{self.u_data[3]}` (**{self.u_data[2]}**).*",
            embed=builder.build_embed(),
            view=builder
        )

    @button(label="📜 Транзакции счета", style=ButtonStyle.secondary, row=1)
    async def user_tx_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        acc = self.u_data[3]
        db = await get_db()
        async with db.execute(
            "SELECT * FROM transactions WHERE sender_acc = ? OR receiver_acc = ? ORDER BY id DESC LIMIT 100",
            (acc, acc)
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            await safe_send_ephemeral(interaction, "ℹ️ У этого счета пока нет транзакций.")
            return
        total_pages = (len(rows) - 1) // 10 + 1
        await show_transaction_page(interaction, rows, 0, total_pages)

    @button(label="🔙 В админ-панель", style=ButtonStyle.gray, row=2)
    async def back_admin_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        view = await AdminPanelView.create()
        embed = await view.build_embed()
        await safe_edit_or_send(interaction, content=None, embed=embed, view=view)


class AdminSearchModal(Modal, title="Поиск счета / игрока / казны"):
    def __init__(self):
        super().__init__()
        self.query_input = TextInput(
            label="Ник, Discord-тег или номер счета",
            placeholder="Введите часть ника, номера счета или 0100000000...",
            required=True,
            max_length=32
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        q = self.query_input.value.strip()
        q_escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{q_escaped}%"
        db = await get_db()
        async with db.execute(
            "SELECT * FROM users WHERE account_id LIKE ? ESCAPE '\\' OR game_nick LIKE ? ESCAPE '\\' OR discord_tag LIKE ? ESCAPE '\\' LIMIT 25",
            (like_pattern, like_pattern, like_pattern)
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            await safe_send_ephemeral(interaction, f"❌ По запросу `{q}` никого не найдено.")
            return

        if len(rows) == 1:
            u = rows[0]
            if u[3] == BANK_ACCOUNT_ID:
                bank_view = await AdminBankManageView.create()
                await safe_edit_or_send(interaction, embed=await bank_view.build_embed(), view=bank_view)
                return
            embed = build_admin_user_embed(u)
            await safe_edit_or_send(interaction, embed=embed, view=AdminUserManageView(u))
            return

        view = AdminPanelView(users_list=rows)
        embed = await view.build_embed(custom_title=f"🔎 Результаты поиска: {q}")
        await safe_edit_or_send(interaction, embed=embed, view=view)


def build_admin_user_embed(u) -> discord.Embed:
    bal = u[4]
    is_blocked = bal < 0
    color = discord.Color.dark_red() if is_blocked else discord.Color.gold()
    title_prefix = "⛔ [ЗАБЛОКИРОВАН] " if is_blocked else "👤 "
    embed = discord.Embed(title=f"{title_prefix}Управление счетом: {u[2]}", color=color)
    embed.add_field(name="Владелец / Тег", value=f"{u[1]} (`{u[0]}`)", inline=True)
    embed.add_field(name="Название / Ник", value=u[2], inline=True)
    embed.add_field(name="Фракция", value=f"`{u[5]}`", inline=True)
    embed.add_field(name="Номер счета", value=f"`{u[3]}`", inline=True)
    embed.add_field(name="Баланс", value=f"**{format_money(bal)}**", inline=True)
    return embed


class AdminPanelView(View):
    def __init__(self, users_list=None):
        super().__init__(timeout=600)
        self.users_list = users_list or []

        if self.users_list:
            options = []
            for u in self.users_list[:25]:
                if u[3] == BANK_ACCOUNT_ID:
                    continue
                emj = "🛡️" if is_treasury_account(u[3]) else ("⛔" if u[4] < 0 else "👤")
                options.append(
                    discord.SelectOption(
                        label=f"{u[2]} | {u[3]}"[:100],
                        description=f"Баланс: {format_money(u[4])} | Фракция: {u[5]}"[:100],
                        value=str(u[3]),
                        emoji=emj
                    )
                )
            if options:
                user_select = Select(
                    placeholder="👤 Выберите счет игрока или казны фракции...",
                    options=options,
                    row=0
                )
                user_select.callback = self.select_user_cb
                self.add_item(user_select)

    @classmethod
    async def create(cls):
        db = await get_db()
        async with db.execute(
            "SELECT * FROM users WHERE account_id != ? ORDER BY CASE WHEN discord_id < 0 THEN 0 ELSE 1 END, balance DESC LIMIT 25",
            (BANK_ACCOUNT_ID,)
        ) as cur:
            users = await cur.fetchall()
        return cls(users_list=users)

    async def build_embed(self, custom_title: str | None = None) -> discord.Embed:
        bank_bal = await get_bank_balance()
        db = await get_db()
        async with db.execute("SELECT COUNT(*) FROM auto_payments WHERE is_active = 1 AND COALESCE(is_bank, 0) = 1") as cur:
            ap_row = await cur.fetchone()
            bank_ap_count = ap_row[0] if ap_row else 0

        embed = discord.Embed(
            title=custom_title or "🛡️ Панель Администратора Банка",
            description=(
                "Выберите счет игрока или казны фракции из списка ниже, либо воспользуйтесь кнопками управления."
            ),
            color=discord.Color.red()
        )
        embed.add_field(
            name="🏦 Счет Центрального Банка",
            value=f"`{BANK_ACCOUNT_ID}`",
            inline=True
        )
        embed.add_field(
            name="💰 Капитал Банка",
            value=f"**{format_money(bank_bal)}**",
            inline=True
        )
        embed.add_field(
            name="🔄 Автоплатежи Банка",
            value=f"Активно: **{bank_ap_count}**",
            inline=True
        )
        return embed

    async def select_user_cb(self, interaction: discord.Interaction):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        acc_id = interaction.data['values'][0]
        u = await get_user_by_account(acc_id)
        if not u:
            await safe_send_ephemeral(interaction, "❌ Счет не найден.")
            return
        await safe_edit_or_send(interaction, content=None, embed=build_admin_user_embed(u), view=AdminUserManageView(u))

    @button(label="🏦 Банк и Автоплатежи Банка", style=ButtonStyle.primary, row=1)
    async def bank_manage_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        bank_view = await AdminBankManageView.create()
        embed = await bank_view.build_embed()
        await safe_edit_or_send(interaction, content=None, embed=embed, view=bank_view)

    @button(label="🔎 Поиск счета", style=ButtonStyle.secondary, row=1)
    async def search_user_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        await safe_send_modal(interaction, AdminSearchModal())

    @button(label="📜 История и фильтры", style=ButtonStyle.blurple, row=1)
    async def history_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        dashboard = TransactionFilterDashboardView()
        await safe_edit_or_send(interaction, content=None, embed=dashboard.build_embed(), view=dashboard)

    @button(label="➕ Зачислить (по ID)", style=ButtonStyle.green, row=2)
    async def add_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        await safe_send_modal(interaction, ManageBalanceModal("add"))

    @button(label="➖ Списать (по ID)", style=ButtonStyle.red, row=2)
    async def remove_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        await safe_send_modal(interaction, ManageBalanceModal("remove"))

    @button(label="⚙️ Установить (по ID)", style=ButtonStyle.secondary, row=2)
    async def set_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await safe_send_ephemeral(interaction, "❌ Нет прав.")
            return
        await safe_send_modal(interaction, ManageBalanceModal("set"))


# --- СОБЫТИЯ И СЛЭШ-КОМАНДЫ (ТОЛЬКО 3 ОСНОВНЫЕ КОМАНДЫ) ---

@bot.event
async def on_ready():
    await init_db()
    print(f'Бот запущен: {bot.user} (БД подключена в памяти)')
    if not auto_payment_task.is_running():
        auto_payment_task.start()
    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано {len(synced)} команд.")
    except Exception as e:
        print(f"Ошибка синхронизации: {e}")


@bot.tree.command(name="start", description="Открыть личный кабинет банка")
async def start_cmd(interaction: discord.Interaction):
    user_data = await get_user_data(interaction.user.id)
    if not user_data:
        await safe_send_ephemeral(interaction, "❌ У вас еще нет банковского счета. Обратитесь к технику.")
        return
    is_leader = check_is_leader(interaction)
    await safe_send_ephemeral(
        interaction,
        embed=build_profile_embed(user_data),
        view=UserProfileView(user_data, is_leader=is_leader)
    )


@bot.tree.command(name="admin_panel", description="Открыть панель администратора банка")
@app_commands.check(check_is_admin)
async def admin_panel_cmd(interaction: discord.Interaction):
    view = await AdminPanelView.create()
    embed = await view.build_embed()
    await safe_send_ephemeral(interaction, embed=embed, view=view)


@bot.tree.command(name="admin_create", description="Создать банковский счет пользователю")
@app_commands.describe(user="Пользователь Discord, которому создается счет")
@app_commands.check(check_is_admin)
async def admin_create_cmd(interaction: discord.Interaction, user: discord.Member):
    await safe_send_modal(interaction, CreateAccountModal(user))


@admin_panel_cmd.error
@admin_create_cmd.error
async def admin_error_handler(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await safe_send_ephemeral(interaction, "❌ У вас нет прав для выполнения этой команды.")
    else:
        await safe_send_ephemeral(interaction, f"❌ Произошла ошибка: {error}")


if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("Токен не найден! Проверьте файл .env")