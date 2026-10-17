import os
import discord
from discord import app_commands, ButtonStyle
from discord.ui import View, Button, Modal, TextInput, button, Select
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiosqlite
import datetime
import io
import csv

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DB_NAME = 'bank.db'
ADMIN_ROLE_NAME = "Техник"
LEADER_ROLE_NAME = "Лидер Фракции"
MAX_AMOUNT = 1_000_000_000_000  # Защита от переполнения SQLite INTEGER (макс. 1 триллион за операцию)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Глобальное хранилище уведомлений (в памяти)
# Структура: {user_id: [{"msg": str, "read": bool, "time": str}]}
user_notifications = {}

RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MSK_TZ = datetime.timezone(datetime.timedelta(hours=3), name="MSK")


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_msk_now() -> datetime.datetime:
    """Возвращает точное московское время (UTC+3) независимо от часового пояса хостинга"""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(MSK_TZ)


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
            # Если пропущено несколько циклов во время рестарта — прыгаем к ближайшему будущему слоту по сетке
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
    'admin_deposit': ('💰 Зачисление (Админ)', 'admin_deposit'),
    'admin_withdraw': ('📉 Списание (Админ)', 'admin_withdraw'),
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

    # 1. Если ОБА поля пустые -> берем текущую дату и текущее время (списание начнется сразу)
    if not date_str and not time_str:
        return now, None, False

    # 2. Разбираем дату (если указана)
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

    # 3. Разбираем время (если указано)
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

    # 4. Комбинируем дату и время по правилам:
    # Случай А: Дата ПУСТАЯ, а Время УКАЗАНО
    if parsed_date is None and parsed_time is not None:
        candidate = datetime.datetime.combine(
            now.date(),
            parsed_time,
            tzinfo=MSK_TZ
        )
        # Если указанное время сегодня уже прошло -> автоматом переносим на завтра
        if candidate <= now:
            candidate += datetime.timedelta(days=1)
            shifted_tomorrow = True
        return candidate, None, shifted_tomorrow

    # Случай Б: Дата УКАЗАНА, а Время ПУСТОЕ -> ставим указанную дату и текущее время
    if parsed_date is not None and parsed_time is None:
        candidate = datetime.datetime.combine(
            parsed_date,
            now.time(),
            tzinfo=MSK_TZ
        )
        return candidate, None, False

    # Случай В: И Дата, и Время УКАЗАНЫ явно
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
    embed = discord.Embed(title="🏦 Личный кабинет", color=discord.Color.green())
    embed.add_field(name="👤 Пользователь", value=user_data[1], inline=True)
    embed.add_field(name="🎮 Ник в игре", value=user_data[2], inline=True)
    embed.add_field(name="🛡️ Код фракции", value=f"`{user_data[5]}`", inline=True)
    embed.add_field(name="💳 Номер счета", value=f"`{user_data[3]}`", inline=True)
    embed.add_field(name="💰 Баланс", value=f"**{user_data[4]:,}**".replace(",", " "), inline=True)
    return embed


async def safe_edit_or_send(
    interaction: discord.Interaction,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: View | None = None
):
    """Безопасное обновление сообщения из модалки/кнопки с защитой от ошибки 10062 (Unknown interaction)"""
    kwargs = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view

    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(**kwargs)
        else:
            await interaction.edit_original_response(**kwargs)
    except discord.NotFound:
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(**kwargs, ephemeral=True)
            else:
                await interaction.followup.send(**kwargs, ephemeral=True)
        except discord.HTTPException:
            pass


# --- БАЗА ДАННЫХ ---

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                discord_id INTEGER PRIMARY KEY,
                discord_tag TEXT,
                game_nick TEXT,
                account_id TEXT UNIQUE,
                balance INTEGER DEFAULT 0,
                faction_prefix TEXT
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                type TEXT NOT NULL,
                sender_acc TEXT,
                receiver_acc TEXT,
                amount INTEGER NOT NULL
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS auto_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_did INTEGER,
                sender_acc TEXT,
                receiver_acc TEXT,
                amount INTEGER,
                interval_val INTEGER,
                interval_unit TEXT,
                next_run TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')

        await db.execute("PRAGMA journal_mode=WAL")
        await db.commit()


async def get_user_data(discord_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT * FROM users WHERE discord_id = ?', (discord_id,)) as cursor:
            return await cursor.fetchone()


async def get_user_by_account(account_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT * FROM users WHERE account_id = ?', (account_id,)) as cursor:
            return await cursor.fetchone()


async def get_faction_members(prefix: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT * FROM users WHERE faction_prefix = ? OR account_id LIKE ?',
            (prefix, f"{prefix}%")
        ) as cursor:
            return await cursor.fetchall()


async def log_transaction(db, t_type: str, sender: str | None, receiver: str | None, amount: int):
    timestamp = get_msk_now().strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO transactions (created_at, type, sender_acc, receiver_acc, amount) VALUES (?, ?, ?, ?, ?)",
        (timestamp, t_type, sender, receiver, amount)
    )


async def add_notification(user_id: int, message: str):
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
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT * FROM auto_payments WHERE is_active = 1 AND next_run <= ?",
                (now_str,)
            ) as cursor:
                payments = await cursor.fetchall()

            for p in payments:
                pay_id, owner_did, s_acc, r_acc, amount, i_val, i_unit, next_run_str, is_active = p

                if amount <= 0 or s_acc == r_acc:
                    await db.execute('UPDATE auto_payments SET is_active = 0 WHERE id = ?', (pay_id,))
                    continue

                # Атомарное списание: баланс уменьшается ТОЛЬКО если на счете сейчас достаточно средств
                cur_deduct = await db.execute(
                    'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                    (amount, s_acc, amount)
                )
                if cur_deduct.rowcount == 0:
                    # Недостаточно средств или счет отправителя не найден — пропускаем до пополнения
                    continue

                async with db.execute('SELECT discord_id FROM users WHERE account_id = ?', (r_acc,)) as cur:
                    r_row = await cur.fetchone()
                    if not r_row:
                        # Возвращаем средства отправителю, если счет получателя удален
                        await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, s_acc))
                        await db.execute('UPDATE auto_payments SET is_active = 0 WHERE id = ?', (pay_id,))
                        continue
                    r_owner_id = r_row[0]

                await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, r_acc))
                await log_transaction(db, 'auto_transfer', s_acc, r_acc, amount)

                await add_notification(owner_did, f"✅ Автоплатеж **{amount}** на `{r_acc}` выполнен.")
                if r_owner_id:
                    await add_notification(r_owner_id, f"💰 Получен автоплатеж **{amount}** от `{s_acc}`.")

                new_next_run = compute_next_run_after(next_run_str, i_val, i_unit)
                await db.execute(
                    'UPDATE auto_payments SET next_run = ? WHERE id = ?',
                    (new_next_run, pay_id)
                )

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
        await interaction.response.defer(ephemeral=True)
        if not check_is_admin(interaction):
            await interaction.followup.send("❌ У вас нет прав администратора!", ephemeral=True)
            return

        acc_id = self.account_id.value.strip()
        if not acc_id.isdigit() or len(acc_id) != 10:
            await interaction.followup.send("❌ ID счета должен состоять ровно из 10 цифр!", ephemeral=True)
            return

        prefix = acc_id[:2]
        async with aiosqlite.connect(DB_NAME) as db:
            try:
                await db.execute(
                    'INSERT INTO users (discord_id, discord_tag, game_nick, account_id, balance, faction_prefix) VALUES (?, ?, ?, ?, 0, ?)',
                    (self.target_user.id, self.target_user.name, self.game_nick.value.strip(), acc_id, prefix)
                )
                await db.commit()
                await add_notification(self.target_user.id, f"🎉 Вам открыт счет `{acc_id}`!")
                await interaction.followup.send(
                    f"✅ Счет `{acc_id}` успешно создан для {self.target_user.mention} (Ник: **{self.game_nick.value.strip()}**, Фракция: `{prefix}`).",
                    ephemeral=True
                )
            except aiosqlite.IntegrityError:
                await interaction.followup.send(
                    "❌ Такой ID счета или пользователь уже зарегистрирован в системе!",
                    ephemeral=True
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
            placeholder="Введите целое положительное число",
            required=True,
            max_length=12
        )
        self.add_item(self.amount_input)

        if not self.prefill_account:
            self.account_input = TextInput(
                label="ID Счета (10 цифр)",
                placeholder="Введите 10-значный номер счета",
                required=True,
                max_length=10,
                min_length=10
            )
            self.add_item(self.account_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not check_is_admin(interaction):
            await interaction.followup.send("❌ У вас нет прав администратора!", ephemeral=True)
            return

        try:
            amount = int(self.amount_input.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Сумма должна быть числом!", ephemeral=True)
            return

        if (self.action_type in ("add", "remove") and amount <= 0) or (self.action_type == "set" and amount < 0):
            await interaction.followup.send("❌ Указана некорректная сумма!", ephemeral=True)
            return

        if amount > MAX_AMOUNT:
            await interaction.followup.send(f"❌ Сумма превышает максимально допустимый лимит ({MAX_AMOUNT:,})!", ephemeral=True)
            return

        acc_id = self.prefill_account or self.account_input.value.strip()
        if not acc_id.isdigit() or len(acc_id) != 10:
            await interaction.followup.send("❌ ID счета должен состоять из 10 цифр!", ephemeral=True)
            return

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute('SELECT balance, discord_id, game_nick FROM users WHERE account_id = ?', (acc_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.followup.send("❌ Счет не найден!", ephemeral=True)
                    return

                current_bal, target_did, game_nick = row

                if self.action_type == "add":
                    await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, acc_id))
                    await log_transaction(db, "admin_deposit", None, acc_id, amount)
                    await add_notification(target_did, f"💰 Зачисление от банка: **+{amount}**.")
                elif self.action_type == "remove":
                    cur_rem = await db.execute(
                        'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                        (amount, acc_id, amount)
                    )
                    if cur_rem.rowcount == 0:
                        await interaction.followup.send(
                            f"❌ Недостаточно средств на счете (доступно: **{current_bal}**).",
                            ephemeral=True
                        )
                        return
                    await log_transaction(db, "admin_withdraw", acc_id, None, amount)
                    await add_notification(target_did, f"📉 Списание банком: **-{amount}**.")
                elif self.action_type == "set":
                    await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (amount, acc_id))
                    await log_transaction(db, "admin_set", acc_id, acc_id, amount)
                    await add_notification(target_did, f"⚙️ Ваш баланс установлен на **{amount}**.")

                await db.commit()

                async with db.execute('SELECT balance FROM users WHERE account_id = ?', (acc_id,)) as cur_new:
                    new_bal_row = await cur_new.fetchone()
                    new_bal = new_bal_row[0] if new_bal_row else 0

        action_names = {"add": "Зачислено", "remove": "Списано", "set": "Установлен баланс"}
        await interaction.followup.send(
            f"✅ {action_names[self.action_type]}: **{amount}**\n"
            f"Игрок: **{game_nick}** (`{acc_id}`)\n"
            f"Текущий баланс: **{new_bal}**",
            ephemeral=True
        )


class TransferModal(Modal, title="Перевод средств"):
    def __init__(self, prefill_target: str | None = None):
        super().__init__()
        self.prefill_target = prefill_target
        if not self.prefill_target:
            self.target_acc = TextInput(
                label="ID Счета получателя",
                placeholder="10 цифр",
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
        await interaction.response.defer(ephemeral=True)
        sender_id = interaction.user.id
        target_acc = self.prefill_target or self.target_acc.value.strip()

        try:
            amount = int(self.amount.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Сумма должна быть числом!", ephemeral=True)
            return

        if amount <= 0 or amount > MAX_AMOUNT:
            await interaction.followup.send("❌ Сумма перевода должна быть больше нуля и в пределах лимита!", ephemeral=True)
            return

        if not target_acc.isdigit() or len(target_acc) != 10:
            await interaction.followup.send("❌ ID счета получателя должен состоять из 10 цифр!", ephemeral=True)
            return

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute('SELECT account_id, balance, game_nick FROM users WHERE discord_id = ?', (sender_id,)) as cur:
                sender_row = await cur.fetchone()
                if not sender_row:
                    await interaction.followup.send("❌ У вас нет банковского счета!", ephemeral=True)
                    return

            sender_acc, sender_bal, sender_nick = sender_row

            if target_acc == sender_acc:
                await interaction.followup.send("❌ Нельзя перевести средства самому себе!", ephemeral=True)
                return

            async with db.execute('SELECT discord_id, game_nick FROM users WHERE account_id = ?', (target_acc,)) as cursor:
                recv = await cursor.fetchone()
                if not recv:
                    await interaction.followup.send("❌ Счет получателя не найден!", ephemeral=True)
                    return
                recv_did, recv_nick = recv

            # Атомарное списание (защита от двойного клика / Race Condition)
            cur_deduct = await db.execute(
                'UPDATE users SET balance = balance - ? WHERE account_id = ? AND balance >= ?',
                (amount, sender_acc, amount)
            )
            if cur_deduct.rowcount == 0:
                await interaction.followup.send(
                    f"❌ Недостаточно средств! Ваш баланс: **{sender_bal}**",
                    ephemeral=True
                )
                return

            await db.execute('UPDATE users SET balance = balance + ? WHERE account_id = ?', (amount, target_acc))
            await log_transaction(db, 'user_transfer', sender_acc, target_acc, amount)
            await db.commit()

            async with db.execute('SELECT balance FROM users WHERE account_id = ?', (sender_acc,)) as cur_b:
                b_row = await cur_b.fetchone()
                new_s_bal = b_row[0] if b_row else (sender_bal - amount)

        await add_notification(sender_id, f"💸 Перевод **{amount}** на счет `{target_acc}` ({recv_nick}).")
        await add_notification(recv_did, f"💰 Получен перевод **{amount}** от `{sender_acc}` ({sender_nick}).")

        await interaction.followup.send(
            f"✅ Перевод **{amount}** игроку **{recv_nick}** (`{target_acc}`) выполнен!\n"
            f"Остаток на счете: **{new_s_bal}**",
            ephemeral=True
        )


class AutoPaymentStepModal(Modal, title="Параметры автоплатежа"):
    def __init__(
        self,
        unit: str,
        unit_label: str,
        prefill_date: str = "",
        prefill_time: str = ""
    ):
        super().__init__()
        self.unit = unit
        self.unit_label = unit_label

        self.receiver_acc = TextInput(
            label="ID Счета получателя (10 цифр)",
            placeholder="Например: 0100000002",
            required=True,
            max_length=10,
            min_length=10
        )
        self.amount = TextInput(
            label="Сумма перевода",
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
            label="Дата 1-го списания (пусто = сегодня)",
            placeholder="ДД.ММ.ГГГГ или ГГГГ-ММ-ДД (необязательно)",
            default=prefill_date,
            required=False,
            max_length=10
        )
        self.start_time = TextInput(
            label="Время 1-го списания по МСК (пусто = сейчас)",
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
        await interaction.response.defer()
        user_id = interaction.user.id
        user_data = await get_user_data(user_id)
        if not user_data:
            await interaction.followup.send("❌ У вас нет счета!", ephemeral=True)
            return

        sender_acc = user_data[3]
        r_acc = self.receiver_acc.value.strip()

        if not r_acc.isdigit() or len(r_acc) != 10:
            await interaction.followup.send("❌ Неверный формат ID счета получателя!", ephemeral=True)
            return

        if r_acc == sender_acc:
            await interaction.followup.send("❌ Нельзя настроить автоплатеж самому себе!", ephemeral=True)
            return

        target_user = await get_user_by_account(r_acc)
        if not target_user:
            await interaction.followup.send("❌ Счет получателя не существует в базе!", ephemeral=True)
            return

        try:
            amount = int(self.amount.value.strip())
            val = int(self.interval_val.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Сумма и интервал должны быть целыми числами!", ephemeral=True)
            return

        if amount <= 0 or amount > MAX_AMOUNT or val <= 0 or val > 10000:
            await interaction.followup.send("❌ Сумма и интервал должны быть больше 0 и в пределах допустимых лимитов!", ephemeral=True)
            return

        first_dt, err_msg, shifted_tomorrow = parse_first_run_datetime(
            self.start_date.value,
            self.start_time.value
        )
        if err_msg or first_dt is None:
            await interaction.followup.send(err_msg or "❌ Ошибка даты/времени.", ephemeral=True)
            return

        next_run_str = first_dt.strftime("%Y-%m-%d %H:%M:%S")

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO auto_payments (owner_did, sender_acc, receiver_acc, amount, interval_val, interval_unit, next_run, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (user_id, sender_acc, r_acc, amount, val, self.unit, next_run_str)
            )
            await db.commit()
            async with db.execute(
                "SELECT * FROM auto_payments WHERE owner_did = ? ORDER BY next_run ASC",
                (user_id,)
            ) as cur:
                payments = await cur.fetchall()

        note = ""
        if shifted_tomorrow:
            note = "\nℹ️ *Указанное время сегодня уже прошло, поэтому первое списание автоматически назначено на завтра.*"

        embed = build_autopayments_embed(payments)
        await safe_edit_or_send(
            interaction,
            content=(
                f"✅ Автоплатеж на `{r_acc}` (**{amount}** каждые **{val} {self.unit_label}**) создан!\n"
                f"🗓️ Первое списание: `{next_run_str}` (МСК){note}"
            ),
            embed=embed,
            view=AutoPaymentsListView(user_data, payments)
        )


# --- ИНТЕРАКТИВНЫЙ КОНСТРУКТОР ФИЛЬТРОВ ТРАНЗАКЦИЙ (GUI БЕЗ 2 МОДАЛОК) ---

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
        await interaction.response.defer()
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
    """GUI-панель фильтрации транзакций с выпадающими списками вместо двух модальных окон"""
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

        # Select 1: Тип операции
        self.type_select = Select(
            placeholder="📂 Выберите тип операции...",
            options=[
                discord.SelectOption(label="Все типы операций", value="all", emoji="📋", default=True),
                discord.SelectOption(label="Переводы игроков", value="user_transfer", emoji="💸"),
                discord.SelectOption(label="Автоплатежи", value="auto_transfer", emoji="🔄"),
                discord.SelectOption(label="Зачисления (Админ)", value="admin_deposit", emoji="💰"),
                discord.SelectOption(label="Списания (Админ)", value="admin_withdraw", emoji="📉"),
                discord.SelectOption(label="Установка баланса", value="admin_set", emoji="⚙️"),
            ],
            row=0
        )
        self.type_select.callback = self.on_type_change
        self.add_item(self.type_select)

        # Select 2: Период времени
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
            "auto_transfer": "🔄 Автоплатежи",
            "admin_deposit": "💰 Зачисления (Админ)",
            "admin_withdraw": "📉 Списания (Админ)",
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
            sum_str = f"от {self.filters['min_a']} до {self.filters['max_a']}"
        elif self.filters["min_a"] is not None:
            sum_str = f"от {self.filters['min_a']}"
        elif self.filters["max_a"] is not None:
            sum_str = f"до {self.filters['max_a']}"

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
        await interaction.response.send_modal(FilterParamsModal(self))

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
        await interaction.response.defer()
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

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send("❌ По заданным фильтрам транзакций не найдено.", ephemeral=True)
            return

        total_pages = (len(rows) - 1) // 10 + 1
        await show_transaction_page(interaction, rows, 0, total_pages, dashboard_view=self)


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
        await interaction.response.send_message("✅ Выгрузка готова:", file=file, ephemeral=True)


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
            f"От: `{s_acc or 'Банк'}` ➔ Кому: `{r_acc or 'Банк'}` | Сумма: **{amount}**\n\n"
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

def build_autopayments_embed(payments) -> discord.Embed:
    desc = "**Ваши активные автоплатежи:**\n\n"
    if not payments:
        desc += "У вас пока нет активных автоплатежей.\nВыберите периодичность в меню ниже, чтобы создать новый."
        color = discord.Color.greyple()
    else:
        for p in payments:
            u_label = UNIT_LABELS.get(p[6], p[6])
            desc += (
                f"🔹 Кому: `{p[3]}` | Сумма: **{p[4]}** | Каждые **{p[5]} {u_label}**\n"
                f"⏱️ Следующее списание: `{p[7]}`\n\n"
            )
        color = discord.Color.orange()
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
    def __init__(self, user_data, unit: str, unit_label: str):
        super().__init__(timeout=300)
        self.user_data = user_data
        self.unit = unit
        self.unit_label = unit_label
        self.selected_date: str = ""  # пусто = сегодня
        self.selected_time: str = ""  # пусто = сейчас

        now = get_msk_now()

        # Select 1: Мини-календарь на 14 дней вперед
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
            date_options.append(
                discord.SelectOption(
                    label=lbl,
                    value=d_str,
                    emoji="🗓️"
                )
            )
        date_options.append(
            discord.SelectOption(
                label="Ввести другую дату вручную...",
                description="Вы сможете вписать любую дату в следующем окне",
                value="manual",
                emoji="⌨️"
            )
        )

        self.date_select = Select(
            placeholder="📅 Выберите дату первого списания...",
            options=date_options,
            row=0
        )
        self.date_select.callback = self.on_date_change
        self.add_item(self.date_select)

        # Select 2: Быстрый выбор времени (МСК)
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

        self.time_select = Select(
            placeholder="🕒 Выберите время первого списания (МСК)...",
            options=time_options,
            row=1
        )
        self.time_select.callback = self.on_time_change
        self.add_item(self.time_select)

    def build_embed(self) -> discord.Embed:
        now = get_msk_now()
        d_display = self.selected_date if self.selected_date else f"Сегодня ({now.strftime('%d.%m.%Y')}) / Авто"
        t_display = self.selected_time if self.selected_time else f"Сейчас ({now.strftime('%H:%M')} МСК)"

        embed = discord.Embed(
            title=f"🗓️ Настройка автоплатежа (Интервал: в {self.unit_label})",
            description=(
                "Выберите **дату** и **время** первого списания в списках ниже (или оставьте по умолчанию), "
                "затем нажмите зеленую кнопку **«Ввести счет и сумму»**.\n\n"
                "💡 *В открывшемся окне вы также сможете подкорректировать дату и время вручную или оставить их пустыми.*"
            ),
            color=discord.Color.blurple()
        )
        embed.add_field(name="⏱️ Единица интервала", value=f"Каждые **N {self.unit_label}**", inline=True)
        embed.add_field(name="📅 Дата 1-го списания", value=f"`{d_display}`", inline=True)
        embed.add_field(name="🕒 Время 1-го списания", value=f"`{t_display}`", inline=True)
        return embed

    async def on_date_change(self, interaction: discord.Interaction):
        val = self.date_select.values[0]
        if val in ("today_auto", "manual"):
            self.selected_date = ""
        else:
            self.selected_date = val

        for opt in self.date_select.options:
            opt.default = (opt.value == val)

        await safe_edit_or_send(interaction, content=None, embed=self.build_embed(), view=self)

    async def on_time_change(self, interaction: discord.Interaction):
        val = self.time_select.values[0]
        if val in ("now", "manual"):
            self.selected_time = ""
        else:
            self.selected_time = val

        for opt in self.time_select.options:
            opt.default = (opt.value == val)

        await safe_edit_or_send(interaction, content=None, embed=self.build_embed(), view=self)

    @button(label="✍️ Ввести счет и сумму", style=ButtonStyle.green, row=2)
    async def open_modal_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(
            AutoPaymentStepModal(
                self.unit,
                self.unit_label,
                prefill_date=self.selected_date,
                prefill_time=self.selected_time
            )
        )

    @button(label="🔙 Назад к автоплатежам", style=ButtonStyle.red, row=2)
    async def back_to_list_btn(self, interaction: discord.Interaction, button: Button):
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT * FROM auto_payments WHERE owner_did = ? ORDER BY next_run ASC",
                (self.user_data[0],)
            ) as cur:
                payments = await cur.fetchall()

        embed = build_autopayments_embed(payments)
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

        # Выпадающий список для СОЗДАНИЯ автоплатежа (выбор единицы времени -> переход в календарик)
        unit_select = Select(
            placeholder="➕ Создать автоплатеж: выберите периодичность...",
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

        # Выпадающий список для УДАЛЕНИЯ (если есть платежи)
        if payments_list:
            options = []
            for p in payments_list[:25]:
                u_label = UNIT_LABELS.get(p[6], p[6])
                label = f"На счет {p[3]} ({p[4]} монет)"
                desc = f"Каждые {p[5]} {u_label} | След: {p[7][:16]}"
                options.append(discord.SelectOption(label=label, description=desc, value=str(p[0]), emoji="🗑️"))

            delete_select = Select(
                placeholder="🗑️ Выберите автоплатеж для удаления...",
                options=options,
                row=1
            )
            delete_select.callback = self.delete_callback
            self.add_item(delete_select)

        back_btn = Button(label="🔙 В профиль", style=ButtonStyle.red, row=2)
        back_btn.callback = self.back_callback
        self.add_item(back_btn)

    async def create_unit_selected(self, interaction: discord.Interaction):
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
            await interaction.response.send_message("❌ Ошибка ID.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM auto_payments WHERE id = ? AND owner_did = ?", (ap_id, self.user_data[0]))
            await db.commit()
            async with db.execute(
                "SELECT * FROM auto_payments WHERE owner_did = ? ORDER BY next_run ASC",
                (self.user_data[0],)
            ) as cur:
                new_list = await cur.fetchall()

        embed = build_autopayments_embed(new_list)
        await safe_edit_or_send(
            interaction,
            content="🗑️ Автоплатеж удален.",
            embed=embed,
            view=AutoPaymentsListView(self.user_data, new_list)
        )


class FactionView(View):
    """Меню фракции с быстрым переводом участнику через выпадающий список"""
    def __init__(self, user_data, members):
        super().__init__(timeout=600)
        self.user_data = user_data
        self.members = [m for m in members if m[0] != user_data[0]]

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
                row=0
            )
            member_select.callback = self.quick_transfer_cb
            self.add_item(member_select)

        back_btn = Button(label="🔙 В профиль", style=ButtonStyle.red, row=1)
        back_btn.callback = self.back_cb
        self.add_item(back_btn)

        tr_btn = Button(label="💸 Перевод по номеру счета", style=ButtonStyle.blurple, row=1)
        tr_btn.callback = self.manual_transfer_cb
        self.add_item(tr_btn)

    async def quick_transfer_cb(self, interaction: discord.Interaction):
        target_acc = interaction.data['values'][0]
        await interaction.response.send_modal(TransferModal(prefill_target=target_acc))

    async def manual_transfer_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TransferModal())

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

        tr_btn = Button(label="💸 Перевод", style=ButtonStyle.blurple, row=0)
        tr_btn.callback = self.transfer_callback
        self.add_item(tr_btn)

        auto_btn = Button(label="🔄 Автоплатежи", style=ButtonStyle.secondary, row=0)
        auto_btn.callback = self.auto_callback
        self.add_item(auto_btn)

        if is_leader:
            fac_btn = Button(label="👥 Фракция", style=ButtonStyle.green, row=0)
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
        await interaction.response.send_modal(TransferModal())

    async def auto_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT * FROM auto_payments WHERE owner_did = ? ORDER BY next_run ASC",
                (self.user_data[0],)
            ) as cur:
                payments = await cur.fetchall()

        embed = build_autopayments_embed(payments)
        await safe_edit_or_send(
            interaction,
            content=None,
            embed=embed,
            view=AutoPaymentsListView(self.user_data, payments)
        )

    async def notif_callback(self, interaction: discord.Interaction):
        notes = user_notifications.get(self.user_data[0], [])
        if not notes:
            await interaction.response.send_message("🔕 У вас пока нет уведомлений.", ephemeral=True)
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
        if not check_is_leader(interaction):
            await interaction.response.send_message("❌ Доступ запрещен.", ephemeral=True)
            return

        my_prefix = self.user_data[5] if len(self.user_data) > 5 and self.user_data[5] else str(self.user_data[3])[:2]
        members = await get_faction_members(my_prefix)
        other_members = [m for m in members if m[0] != self.user_data[0]]

        if not other_members:
            await interaction.response.send_message(f"ℹ️ В вашей фракции (`{my_prefix}`) пока нет других участников.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🛡️ Участники фракции (Код: {my_prefix})",
            description="Вы можете быстро перевести средства участнику, выбрав его в списке ниже.",
            color=discord.Color.blue()
        )
        for m in other_members[:25]:
            embed.add_field(
                name=f"👤 {m[2]}",
                value=f"💳 `{m[3]}`\n📛 {m[1]}",
                inline=True
            )

        await safe_edit_or_send(
            interaction,
            content=None,
            embed=embed,
            view=FactionView(self.user_data, members)
        )


# --- АДМИН-ПАНЕЛЬ И УПРАВЛЕНИЕ ИГРОКАМИ ---

class AdminUserManageView(View):
    """Карточка управления конкретным игроком в админ-панели (без ручного ввода 10 цифр счета)"""
    def __init__(self, target_user_data):
        super().__init__(timeout=300)
        self.u_data = target_user_data  # (discord_id, discord_tag, game_nick, account_id, balance, faction_prefix)

    @button(label="➕ Зачислить", style=ButtonStyle.green, row=0)
    async def add_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        await interaction.response.send_modal(ManageBalanceModal("add", prefill_account=self.u_data[3]))

    @button(label="➖ Списать", style=ButtonStyle.red, row=0)
    async def remove_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        await interaction.response.send_modal(ManageBalanceModal("remove", prefill_account=self.u_data[3]))

    @button(label="⚙️ Установить", style=ButtonStyle.blurple, row=0)
    async def set_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        await interaction.response.send_modal(ManageBalanceModal("set", prefill_account=self.u_data[3]))

    @button(label="📜 Транзакции игрока", style=ButtonStyle.secondary, row=1)
    async def user_tx_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        await interaction.response.defer()
        acc = self.u_data[3]
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT * FROM transactions WHERE sender_acc = ? OR receiver_acc = ? ORDER BY id DESC LIMIT 100",
                (acc, acc)
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            await interaction.followup.send("ℹ️ У этого счета пока нет транзакций.", ephemeral=True)
            return
        total_pages = (len(rows) - 1) // 10 + 1
        await show_transaction_page(interaction, rows, 0, total_pages)

    @button(label="🔙 В админ-панель", style=ButtonStyle.gray, row=1)
    async def back_admin_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🛡️ Панель Администратора",
            description="Выберите действие или найдите игрока в списке ниже.",
            color=discord.Color.red()
        )
        await safe_edit_or_send(interaction, embed=embed, view=await AdminPanelView.create())


class AdminSearchModal(Modal, title="Поиск счета / игрока"):
    def __init__(self):
        super().__init__()
        self.query_input = TextInput(
            label="Ник, Discord-тег или номер счета",
            placeholder="Введите часть ника или номера счета...",
            required=True,
            max_length=32
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not check_is_admin(interaction):
            await interaction.followup.send("❌ Нет прав.", ephemeral=True)
            return
        q = self.query_input.value.strip()
        # Экранирование спецсимволов SQL LIKE (% и _)
        q_escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{q_escaped}%"
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT * FROM users WHERE account_id LIKE ? ESCAPE '\\' OR game_nick LIKE ? ESCAPE '\\' OR discord_tag LIKE ? ESCAPE '\\' LIMIT 25",
                (like_pattern, like_pattern, like_pattern)
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await interaction.followup.send(f"❌ По запросу `{q}` никого не найдено.", ephemeral=True)
            return

        if len(rows) == 1:
            u = rows[0]
            embed = build_admin_user_embed(u)
            await safe_edit_or_send(interaction, embed=embed, view=AdminUserManageView(u))
            return

        embed = discord.Embed(
            title=f"🔎 Результаты поиска: {q}",
            description="Выберите игрока из выпадающего списка ниже для управления его счетом.",
            color=discord.Color.blurple()
        )
        await safe_edit_or_send(interaction, embed=embed, view=AdminPanelView(users_list=rows))


def build_admin_user_embed(u) -> discord.Embed:
    embed = discord.Embed(title=f"👤 Управление счетом: {u[2]}", color=discord.Color.gold())
    embed.add_field(name="Discord", value=f"{u[1]} (`{u[0]}`)", inline=True)
    embed.add_field(name="Игровой ник", value=u[2], inline=True)
    embed.add_field(name="Фракция", value=f"`{u[5]}`", inline=True)
    embed.add_field(name="Номер счета", value=f"`{u[3]}`", inline=True)
    embed.add_field(name="Баланс", value=f"**{u[4]:,}**".replace(",", " "), inline=True)
    return embed


class AdminPanelView(View):
    def __init__(self, users_list=None):
        super().__init__(timeout=600)
        self.users_list = users_list or []

        if self.users_list:
            options = []
            for u in self.users_list[:25]:
                options.append(
                    discord.SelectOption(
                        label=f"{u[2]} | Счет: {u[3]}",
                        description=f"Баланс: {u[4]} | Discord: {u[1]}",
                        value=str(u[3]),
                        emoji="👤"
                    )
                )
            user_select = Select(
                placeholder="👤 Выберите игрока для управления балансом...",
                options=options,
                row=0
            )
            user_select.callback = self.select_user_cb
            self.add_item(user_select)

    @classmethod
    async def create(cls):
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT * FROM users ORDER BY balance DESC LIMIT 25") as cur:
                users = await cur.fetchall()
        return cls(users_list=users)

    async def select_user_cb(self, interaction: discord.Interaction):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        acc_id = interaction.data['values'][0]
        u = await get_user_by_account(acc_id)
        if not u:
            await interaction.response.send_message("❌ Счет не найден.", ephemeral=True)
            return
        await safe_edit_or_send(interaction, embed=build_admin_user_embed(u), view=AdminUserManageView(u))

    @button(label="🔎 Поиск игрока", style=ButtonStyle.primary, row=1)
    async def search_user_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        await interaction.response.send_modal(AdminSearchModal())

    @button(label="📜 История и фильтры", style=ButtonStyle.blurple, row=1)
    async def history_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        dashboard = TransactionFilterDashboardView()
        await safe_edit_or_send(interaction, embed=dashboard.build_embed(), view=dashboard)

    @button(label="➕ Зачислить (по ID)", style=ButtonStyle.green, row=2)
    async def add_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        await interaction.response.send_modal(ManageBalanceModal("add"))

    @button(label="➖ Списать (по ID)", style=ButtonStyle.red, row=2)
    async def remove_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        await interaction.response.send_modal(ManageBalanceModal("remove"))

    @button(label="⚙️ Установить (по ID)", style=ButtonStyle.secondary, row=2)
    async def set_btn(self, interaction: discord.Interaction, button: Button):
        if not check_is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        await interaction.response.send_modal(ManageBalanceModal("set"))


# --- СОБЫТИЯ И КОМАНДЫ ---

@bot.event
async def on_ready():
    print(f'Бот запущен: {bot.user}')
    await init_db()
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
        await interaction.response.send_message("❌ У вас еще нет банковского счета. Обратитесь к технику.", ephemeral=True)
        return
    is_leader = check_is_leader(interaction)
    await interaction.response.send_message(
        embed=build_profile_embed(user_data),
        view=UserProfileView(user_data, is_leader=is_leader),
        ephemeral=True
    )


@bot.tree.command(name="admin_panel", description="Открыть панель администратора банка")
@app_commands.check(check_is_admin)
async def admin_panel_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ Панель Администратора",
        description="Выберите игрока из выпадающего списка для быстрых операций или воспользуйтесь кнопками ниже.",
        color=discord.Color.red()
    )
    view = await AdminPanelView.create()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="admin_create", description="Создать банковский счет пользователю")
@app_commands.describe(user="Пользователь Discord, которому создается счет")
@app_commands.check(check_is_admin)
async def admin_create_cmd(interaction: discord.Interaction, user: discord.Member):
    data = await get_user_data(user.id)
    if data:
        await interaction.response.send_message(f"⚠️ У пользователя уже есть счет: `{data[3]}`", ephemeral=True)
        return
    await interaction.response.send_modal(CreateAccountModal(user))


@bot.tree.command(name="admin_add", description="Зачислить средства пользователю")
@app_commands.describe(user="Пользователь, которому нужно зачислить средства")
@app_commands.check(check_is_admin)
async def admin_add_cmd(interaction: discord.Interaction, user: discord.Member):
    data = await get_user_data(user.id)
    if not data:
        await interaction.response.send_message("❌ У выбранного пользователя нет счета!", ephemeral=True)
        return
    await interaction.response.send_modal(ManageBalanceModal("add", prefill_account=data[3]))


@bot.tree.command(name="admin_remove", description="Списать средства у пользователя")
@app_commands.describe(user="Пользователь, у которого нужно списать средства")
@app_commands.check(check_is_admin)
async def admin_remove_cmd(interaction: discord.Interaction, user: discord.Member):
    data = await get_user_data(user.id)
    if not data:
        await interaction.response.send_message("❌ У выбранного пользователя нет счета!", ephemeral=True)
        return
    await interaction.response.send_modal(ManageBalanceModal("remove", prefill_account=data[3]))


@bot.tree.command(name="admin_set", description="Установить баланс пользователю")
@app_commands.describe(user="Пользователь, которому нужно установить баланс")
@app_commands.check(check_is_admin)
async def admin_set_cmd(interaction: discord.Interaction, user: discord.Member):
    data = await get_user_data(user.id)
    if not data:
        await interaction.response.send_message("❌ У выбранного пользователя нет счета!", ephemeral=True)
        return
    await interaction.response.send_modal(ManageBalanceModal("set", prefill_account=data[3]))


@admin_panel_cmd.error
@admin_create_cmd.error
@admin_add_cmd.error
@admin_remove_cmd.error
@admin_set_cmd.error
async def admin_error_handler(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await safe_edit_or_send(interaction, content="❌ У вас нет прав для выполнения этой команды.")
    else:
        await safe_edit_or_send(interaction, content=f"❌ Произошла ошибка: {error}")


if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("Токен не найден! Проверьте файл .env")