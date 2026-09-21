import os
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

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DB_NAME = 'bank.db'
ADMIN_ROLE_NAME = "Техник"
LEADER_ROLE_NAME = "Лидер Фракции"

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Глобальное хранилище уведомлений (в памяти)
# Структура: {user_id: [list of notifications]}
user_notifications = {}


# --- БАЗА ДАННЫХ ---

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица пользователей
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

        # Таблица транзакций
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

        # Таблица автоплатежей
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


async def get_user_data(discord_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT * FROM users WHERE discord_id = ?', (discord_id,)) as cursor:
            return await cursor.fetchone()


async def get_faction_members(prefix):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT * FROM users WHERE faction_prefix = ? OR account_id LIKE ?',
                              (prefix, f"{prefix}%")) as cursor:
            return await cursor.fetchall()


async def log_transaction(db, t_type, sender, receiver, amount):
    now_msk = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
    timestamp = now_msk.strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO transactions (created_at, type, sender_acc, receiver_acc, amount) VALUES (?, ?, ?, ?, ?)",
        (timestamp, t_type, sender, receiver, amount)
    )


async def add_notification(user_id, message):
    if user_id not in user_notifications:
        user_notifications[user_id] = []
    # Добавляем в начало списка
    user_notifications[user_id].insert(0, {"msg": message, "read": False,
                                           "time": datetime.datetime.now().strftime("%H:%M")})
    # Храним только последние 10
    if len(user_notifications[user_id]) > 10:
        user_notifications[user_id] = user_notifications[user_id][:10]


# --- ФОНОВАЯ ЗАДАЧА АВТОПЛАТЕЖЕЙ ---

@tasks.loop(seconds=10)
async def auto_payment_task():
    now = datetime.datetime.now()
    async with aiosqlite.connect(DB_NAME) as db:
        # Ищем активные платежи, время которых наступило
        async with db.execute("SELECT * FROM auto_payments WHERE is_active = 1 AND next_run <= ?",
                              (now.strftime("%Y-%m-%d %H:%M:%S"),)) as cursor:
            payments = await cursor.fetchall()

        for p in payments:
            # p: (id, owner_did, sender_acc, receiver_acc, amount, interval_val, interval_unit, next_run, is_active)
            pay_id, owner_did, s_acc, r_acc, amount, i_val, i_unit, next_run_str, is_active = p

            # Проверяем баланс отправителя
            async with db.execute('SELECT balance FROM users WHERE account_id = ?', (s_acc,)) as cur:
                row = await cur.fetchone()
                if not row or row[0] < amount:
                    # Недостаточно средств - пропускаем, но не удаляем (или можно деактивировать)
                    # Для надежности просто пропускаем этот цикл, ждем пополнения
                    continue

            # Выполняем перевод
            async with db.execute('SELECT balance FROM users WHERE account_id = ?', (r_acc,)) as cur:
                r_row = await cur.fetchone()
                if not r_row: continue  # Получатель исчез

                new_s_bal = row[0] - amount
                new_r_bal = r_row[0] + amount

                await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (new_s_bal, s_acc))
                await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (new_r_bal, r_acc))
                await log_transaction(db, 'auto_transfer', s_acc, r_acc, amount)

                # Уведомления
                await add_notification(owner_did, f"✅ Автоплатеж {amount} на {r_acc} выполнен.")
                # Найдем ID получателя для уведомления (если есть в БД)
                async with db.execute('SELECT discord_id FROM users WHERE account_id = ?', (r_acc,)) as cur_r:
                    r_owner = await cur_r.fetchone()
                    if r_owner:
                        await add_notification(r_owner[0], f"💰 Получен автоплатеж {amount} от {s_acc}.")

                # Рассчитываем следующее время
                delta = 0
                if i_unit == 'seconds':
                    delta = i_val
                elif i_unit == 'minutes':
                    delta = i_val * 60
                elif i_unit == 'hours':
                    delta = i_val * 3600
                elif i_unit == 'days':
                    delta = i_val * 86400
                elif i_unit == 'weeks':
                    delta = i_val * 604800
                elif i_unit == 'months':
                    delta = i_val * 2592000  # approx

                next_dt = datetime.datetime.now() + datetime.timedelta(seconds=delta)
                await db.execute('UPDATE auto_payments SET next_run = ? WHERE id = ?',
                                 (next_dt.strftime("%Y-%m-%d %H:%M:%S"), pay_id))

        await db.commit()


@auto_payment_task.before_loop
async def before_auto_payment():
    await bot.wait_until_ready()


# --- МОДАЛЬНЫЕ ОКНА ---

class CreateAccountModal(Modal, title="Создание счета"):
    def __init__(self, target_user: discord.Member):
        super().__init__()
        self.target_user = target_user
        self.game_nick = TextInput(label="Ник в игре", placeholder="Введите ник", required=True)
        self.account_id = TextInput(label="ID Счета (10 цифр)", placeholder="Только цифры", required=True,
                                    max_length=10, min_length=10)
        self.add_item(self.game_nick)
        self.add_item(self.account_id)

    async def on_submit(self, interaction: discord.Interaction):
        acc_id = self.account_id.value
        if not acc_id.isdigit():
            await interaction.response.send_message("❌ ID должен быть числом!", ephemeral=True)
            return

        prefix = acc_id[:2]
        async with aiosqlite.connect(DB_NAME) as db:
            try:
                await db.execute(
                    'INSERT INTO users (discord_id, discord_tag, game_nick, account_id, balance, faction_prefix) VALUES (?, ?, ?, ?, 0, ?)',
                    (self.target_user.id, self.target_user.name, self.game_nick.value, acc_id, prefix))
                await db.commit()
                await interaction.response.send_message(f"✅ Счет создан: `{acc_id}`", ephemeral=True)
            except aiosqlite.IntegrityError:
                await interaction.response.send_message("❌ Такой ID или пользователь уже существует!", ephemeral=True)


class ManageBalanceModal(Modal, title="Операция с балансом"):
    def __init__(self, action_type: str):
        super().__init__()
        self.action_type = action_type
        labels = {"add": "Сумма зачисления", "remove": "Сумма списания", "set": "Новый баланс"}
        self.amount_input = TextInput(label=labels.get(action_type, "Сумма"), placeholder="Число", required=True)
        self.account_input = TextInput(label="ID Счета", placeholder="10 цифр", required=True, max_length=10,
                                       min_length=10)
        self.add_item(self.amount_input)
        self.add_item(self.account_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message("❌ Неверная сумма!", ephemeral=True)
            return

        acc_id = self.account_input.value
        if not acc_id.isdigit() or len(acc_id) != 10:
            await interaction.response.send_message("❌ ID должен быть 10 цифр!", ephemeral=True)
            return

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute('SELECT balance FROM users WHERE account_id = ?', (acc_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message("❌ Счет не найден!", ephemeral=True)
                    return

                current_bal = row[0]
                new_bal = current_bal

                if self.action_type == "add":
                    new_bal += amount
                    t_type = "admin_deposit"
                    await log_transaction(db, t_type, None, acc_id, amount)
                elif self.action_type == "remove":
                    if current_bal < amount:
                        await interaction.response.send_message("❌ Недостаточно средств!", ephemeral=True)
                        return
                    new_bal -= amount
                    t_type = "admin_withdraw"
                    await log_transaction(db, t_type, acc_id, None, amount)
                elif self.action_type == "set":
                    new_bal = amount
                    t_type = "admin_set"
                    await log_transaction(db, t_type, acc_id, acc_id, amount)

                await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (new_bal, acc_id))
                await db.commit()

        action_names = {"add": "Зачислено", "remove": "Списано", "set": "Установлен"}
        await interaction.response.send_message(
            f"✅ {action_names[self.action_type]}: **{amount}**\nСчет: `{acc_id}`\nБаланс: **{new_bal}**",
            ephemeral=True)


class TransferModal(Modal, title="Перевод средств"):
    def __init__(self):
        super().__init__()
        self.target_acc = TextInput(label="ID Счета получателя", placeholder="10 цифр", required=True, max_length=10,
                                    min_length=10)
        self.amount = TextInput(label="Сумма", placeholder="Число", required=True)
        self.add_item(self.target_acc)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        sender_id = interaction.user.id
        async with aiosqlite.connect(DB_NAME) as db:
            sender_data = await get_user_data(sender_id)
            if not sender_data:
                await interaction.response.send_message("❌ У вас нет счета!", ephemeral=True)
                return

            sender_acc = sender_data[3]
            sender_bal = sender_data[4]

            try:
                amount = int(self.amount.value)
            except ValueError:
                await interaction.response.send_message("❌ Неверная сумма!", ephemeral=True)
                return

            if amount <= 0 or sender_bal < amount:
                await interaction.response.send_message("❌ Ошибка суммы или баланса!", ephemeral=True)
                return

            target_acc = self.target_acc.value
            if not target_acc.isdigit() or target_acc == sender_acc:
                await interaction.response.send_message("❌ Неверный ID или перевод себе!", ephemeral=True)
                return

            async with db.execute('SELECT balance FROM users WHERE account_id = ?', (target_acc,)) as cursor:
                recv = await cursor.fetchone()
                if not recv:
                    await interaction.response.send_message("❌ Получатель не найден!", ephemeral=True)
                    return

                new_s_bal = sender_bal - amount
                new_r_bal = recv[0] + amount

                await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (new_s_bal, sender_acc))
                await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (new_r_bal, target_acc))
                await log_transaction(db, 'user_transfer', sender_acc, target_acc, amount)
                await db.commit()

        await interaction.response.send_message(f"✅ Перевод **{amount}** на `{target_acc}` успешен!", ephemeral=True)


class AutoPaymentModal(Modal, title="Настройка автоплатежа"):
    def __init__(self):
        super().__init__()
        self.receiver_acc = TextInput(label="ID Счета получателя", placeholder="10 цифр", required=True, max_length=10,
                                      min_length=10)
        self.amount = TextInput(label="Сумма перевода", placeholder="Число", required=True)
        self.interval_val = TextInput(label="Интервал (число)", placeholder="Например: 1, 5, 24", required=True)
        self.interval_unit = TextInput(label="Единица времени",
                                       placeholder="seconds, minutes, hours, days, weeks, months", required=True)

        self.add_item(self.receiver_acc)
        self.add_item(self.amount)
        self.add_item(self.interval_val)
        self.add_item(self.interval_unit)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        user_data = await get_user_data(user_id)
        if not user_data:
            await interaction.response.send_message("❌ У вас нет счета!", ephemeral=True)
            return

        sender_acc = user_data[3]
        if user_data[4] < 1:  # Проверка наличия хоть каких-то денег для первого платежа (опционально)
            pass  # Можно не блокировать создание, если деньги придут позже

        try:
            amount = int(self.amount.value)
            val = int(self.interval_val.value)
        except ValueError:
            await interaction.response.send_message("❌ Сумма и интервал должны быть числами!", ephemeral=True)
            return

        r_acc = self.receiver_acc.value
        unit = self.interval_unit.value.lower()
        valid_units = ['seconds', 'minutes', 'hours', 'days', 'weeks', 'months']
        if unit not in valid_units:
            await interaction.response.send_message(
                f"❌ Неверная единица времени. Используйте: {', '.join(valid_units)}", ephemeral=True)
            return

        if not r_acc.isdigit() or len(r_acc) != 10:
            await interaction.response.send_message("❌ Неверный ID получателя!", ephemeral=True)
            return

        # Расчет первой даты запуска
        delta = 0
        if unit == 'seconds':
            delta = val
        elif unit == 'minutes':
            delta = val * 60
        elif unit == 'hours':
            delta = val * 3600
        elif unit == 'days':
            delta = val * 86400
        elif unit == 'weeks':
            delta = val * 604800
        elif unit == 'months':
            delta = val * 2592000

        next_dt = datetime.datetime.now() + datetime.timedelta(seconds=delta)
        next_run_str = next_dt.strftime("%Y-%m-%d %H:%M:%S")

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO auto_payments (owner_did, sender_acc, receiver_acc, amount, interval_val, interval_unit, next_run, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (user_id, sender_acc, r_acc, amount, val, unit, next_run_str)
            )
            await db.commit()

        await interaction.response.send_message(
            f"✅ Автоплатеж создан!\nКому: `{r_acc}`\nСумма: **{amount}**\nИнтервал: каждые **{val} {unit}**\nПервый запуск: {next_run_str}",
            ephemeral=True)


# --- ФИЛЬТРЫ ТРАНЗАКЦИЙ ---

class TransactionFilterStep1(Modal, title="Фильтры (1/2)"):
    def __init__(self):
        super().__init__()
        self.type_input = TextInput(label="Тип (transfer/deposit...)", placeholder="Оставь пустым", required=False,
                                    max_length=20)
        self.date_from = TextInput(label="Дата от (ГГГГ-ММ-ДД)", placeholder="2023-01-01", required=False,
                                   max_length=10)
        self.date_to = TextInput(label="Дата до (ГГГГ-ММ-ДД)", placeholder="2023-12-31", required=False, max_length=10)
        self.min_amount = TextInput(label="Мин. сумма", placeholder="0", required=False, max_length=10)
        self.max_amount = TextInput(label="Макс. сумма", placeholder="9999999", required=False, max_length=10)
        for item in [self.type_input, self.date_from, self.date_to, self.min_amount, self.max_amount]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        data = {'type': self.type_input.value, 'd_from': self.date_from.value, 'd_to': self.date_to.value,
                'min_a': self.min_amount.value, 'max_a': self.max_amount.value}
        view = Step2LauncherView(data)
        await interaction.response.send_message("🔽 Нажмите кнопку для ввода ID:", view=view, ephemeral=True)


class Step2LauncherView(View):
    def __init__(self, data):
        super().__init__(timeout=120)
        self.data = data

    @button(label="Заполнить ID счетов", style=ButtonStyle.primary)
    async def launch_step2(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(TransactionFilterStep2(self.data))


class TransactionFilterStep2(Modal, title="Фильтры (2/2)"):
    def __init__(self, step1_data):
        super().__init__()
        self.step1_data = step1_data
        self.sender_filter = TextInput(label="ID Отправителя", required=False, max_length=15)
        self.receiver_filter = TextInput(label="ID Получателя", required=False, max_length=15)
        self.tx_id_filter = TextInput(label="ID Транзакции", required=False, max_length=10)
        for item in [self.sender_filter, self.receiver_filter, self.tx_id_filter]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        query = "SELECT * FROM transactions WHERE 1=1"
        params = []
        d = self.step1_data
        if d['type']:
            t = d['type'].lower()
            val = 'user_transfer' if 'transfer' in t else (
                'admin_deposit' if 'deposit' in t else ('admin_withdraw' if 'withdraw' in t else 'admin_set'))
            query += " AND type = ?";
            params.append(val)
        if d['d_from']: query += " AND created_at >= ?"; params.append(f"{d['d_from']} 00:00:00")
        if d['d_to']: query += " AND created_at <= ?"; params.append(f"{d['d_to']} 23:59:59")
        if d['min_a']: query += " AND amount >= ?"; params.append(int(d['min_a']) if d['min_a'].isdigit() else 0)
        if d['max_a']: query += " AND amount <= ?"; params.append(int(d['max_a']) if d['max_a'].isdigit() else 9999999)
        if self.sender_filter.value: query += " AND sender_acc = ?"; params.append(self.sender_filter.value)
        if self.receiver_filter.value: query += " AND receiver_acc = ?"; params.append(self.receiver_filter.value)
        if self.tx_id_filter.value and self.tx_id_filter.value.isdigit(): query += " AND id = ?"; params.append(
            int(self.tx_id_filter.value))
        query += " ORDER BY id DESC LIMIT 100"

        try:
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute(query, params) as cursor: rows = await cursor.fetchall()
            if not rows: await interaction.followup.send("❌ Ничего не найдено.", ephemeral=True); return
            total_pages = (len(rows) - 1) // 10 + 1
            await show_transaction_page(interaction, rows, 0, total_pages)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


# --- ПАГИНАЦИЯ ---

class TransactionPaginationView(View):
    def __init__(self, full_data, current_page, total_pages):
        super().__init__(timeout=120)
        self.full_data = full_data;
        self.page = current_page;
        self.total = total_pages
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == self.total - 1

    @button(label="⬅️", style=ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: Button):
        await show_transaction_page(interaction, self.full_data, self.page - 1, self.total)

    @button(label="➡️", style=ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: Button):
        await show_transaction_page(interaction, self.full_data, self.page + 1, self.total)

    @button(label="📥 CSV", style=ButtonStyle.green)
    async def export_btn(self, interaction: discord.Interaction, button: Button):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Дата", "Тип", "Отправитель", "Получатель", "Сумма"])
        for tx in self.full_data: writer.writerow(tx)
        file = discord.File(fp=io.BytesIO(output.getvalue().encode('utf-8-sig')), filename="transactions.csv")
        await interaction.response.send_message(file=file, ephemeral=True)


async def show_transaction_page(interaction, data, page, total_pages):
    start, end = page * 10, page * 10 + 10
    items = data[start:end]
    desc = ""
    for tx in items:
        icon = "💸" if tx[2] == 'user_transfer' else "💰" if 'deposit' in tx[2] else "📉" if 'withdraw' in tx[2] else "⚙️"
        desc += f"**#{tx[0]:08d}** | {tx[1]} | {icon} `{tx[2]}`\nОт: `{tx[3] or '—'}` → `{tx[4] or '—'}` | **{tx[5]}**\n\n"
    embed = discord.Embed(title="📜 История", description=desc, color=discord.Color.blue())
    embed.set_footer(text=f"Стр. {page + 1}/{total_pages}")
    view = TransactionPaginationView(data, page, total_pages)
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# --- ИНТЕРФЕЙСЫ ПОЛЬЗОВАТЕЛЯ ---

class NotificationsView(View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.user_id = user_id

    @button(label="🔙 Назад", style=ButtonStyle.red)
    async def back_btn(self, interaction: discord.Interaction, button: Button):
        user_data = await get_user_data(self.user_id)
        if not user_data: return
        is_leader = any(
            role.name == LEADER_ROLE_NAME for role in interaction.user.roles) if interaction.guild else False
        emb = discord.Embed(title="🏦 Личный кабинет", color=discord.Color.green())
        emb.add_field(name="Пользователь", value=user_data[1], inline=False)
        emb.add_field(name="Ник", value=user_data[2], inline=True)
        emb.add_field(name="ID", value=f"`{user_data[3]}`", inline=True)
        emb.add_field(name="Баланс", value=f"**{user_data[4]}**", inline=False)
        await interaction.response.edit_message(embed=emb, view=UserProfileView(user_data, is_leader=is_leader))


class AutoPaymentsListView(View):
    def __init__(self, user_data, payments_list):
        super().__init__(timeout=300)
        self.user_data = user_data
        self.payments = payments_list

        # Кнопка создания
        create_btn = Button(label="➕ Создать автоплатеж", style=ButtonStyle.green)
        create_btn.callback = self.create_callback
        self.add_item(create_btn)

        # Кнопка назад
        back_btn = Button(label="🔙 В профиль", style=ButtonStyle.red)
        back_btn.callback = self.back_callback
        self.add_item(back_btn)

        # Выпадающий список для удаления (только если есть платежи)
        if payments_list:
            options = []
            for p in payments_list:
                # p: (id, owner_did, sender_acc, receiver_acc, amount, interval_val, interval_unit, next_run, is_active)
                label = f"На {p[3]} ({p[4]} монет)"
                # Обрезаем дату для компактности
                desc = f"Каждые {p[5]} {p[6]} | След: {p[7][:16]}"
                options.append(discord.SelectOption(label=label, description=desc, value=str(p[0])))

            # Ограничение Discord: максимум 25 опций
            if len(options) > 25:
                options = options[:25]

            select = Select(
                placeholder="Выберите платеж для удаления...",
                options=options,
                custom_id="auto_pay_delete_select"
            )
            select.callback = self.delete_callback
            self.add_item(select)

    async def create_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AutoPaymentModal())

    async def back_callback(self, interaction: discord.Interaction):
        is_leader = any(
            role.name == LEADER_ROLE_NAME for role in interaction.user.roles) if interaction.guild else False
        if not is_leader and interaction.guild and interaction.user.id != interaction.guild.owner_id:
            is_leader = False

        embed = discord.Embed(title="🏦 Личный кабинет", color=discord.Color.green())
        embed.add_field(name="Пользователь", value=self.user_data[1], inline=False)
        embed.add_field(name="Ник", value=self.user_data[2], inline=True)
        embed.add_field(name="ID", value=f"`{self.user_data[3]}`", inline=True)
        embed.add_field(name="Баланс", value=f"**{self.user_data[4]}**", inline=False)

        new_view = UserProfileView(self.user_data, is_leader=is_leader)
        await interaction.response.edit_message(embed=embed, view=new_view)

    async def delete_callback(self, interaction: discord.Interaction):
        # Получаем ID выбранного элемента
        if not interaction.data.get('values'):
            return
        select_val = interaction.data['values'][0]

        try:
            ap_id = int(select_val)
        except ValueError:
            await interaction.response.send_message("❌ Ошибка ID.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM auto_payments WHERE id = ? AND owner_did = ?", (ap_id, self.user_data[0]))
            await db.commit()

            # Обновляем список платежей из БД
            async with db.execute("SELECT * FROM auto_payments WHERE owner_did = ? ORDER BY next_run ASC",
                                  (self.user_data[0],)) as cur:
                new_list = await cur.fetchall()

            # Формируем новый Embed
            desc = "**Ваши активные автоплатежи:**\n\n"
            if not new_list:
                desc += "Нет активных платежей."
                color = discord.Color.greyple()
            else:
                for p in new_list:
                    desc += f"🆔 `{p[3]}` | Сумма: **{p[4]}** | Каждые **{p[5]} {p[6]}**\nСледующий: `{p[7]}`\n\n"
                color = discord.Color.orange()

            embed = discord.Embed(title="🔄 Автоплатежи", description=desc, color=color)

            # Создаем НОВЫЙ вид. Если список пуст, выпадающего списка в нем не будет.
            new_view = AutoPaymentsListView(self.user_data, new_list)

            # Редактируем ОРИГИНАЛЬНОЕ сообщение (кнопка сработала там)
            await interaction.response.edit_message(embed=embed, view=new_view)


class UserProfileView(View):
    def __init__(self, user_data, is_leader: bool = False):
        super().__init__(timeout=900)
        self.user_data = user_data
        tr_btn = Button(label="💸 Перевод", style=ButtonStyle.blurple)
        tr_btn.callback = self.transfer_callback
        self.add_item(tr_btn)
        if is_leader:
            fac_btn = Button(label="👥 Фракция", style=ButtonStyle.green)
            fac_btn.callback = self.faction_callback
            self.add_item(fac_btn)

        # Кнопка автоплатежей
        auto_btn = Button(label="🔄 Автоплатежи", style=ButtonStyle.secondary)
        auto_btn.callback = self.auto_callback
        self.add_item(auto_btn)

        # Кнопка уведомлений
        notif_count = len([n for n in user_notifications.get(self.user_data[0], []) if not n['read']])
        label = f"🔔 Уведомления ({notif_count})" if notif_count > 0 else "🔔 Уведомления"
        notif_btn = Button(label=label, style=ButtonStyle.gray)
        notif_btn.callback = self.notif_callback
        self.add_item(notif_btn)

    async def transfer_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TransferModal())

    async def auto_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT * FROM auto_payments WHERE owner_did = ? ORDER BY next_run ASC",
                                  (self.user_data[0],)) as cur:
                payments = await cur.fetchall()

        desc = "**Ваши активные автоплатежи:**\n\n"
        if not payments:
            desc += "Нет активных платежей."
        else:
            for p in payments:
                # p: id, owner_did, sender_acc, receiver_acc, amount, interval_val, interval_unit, next_run, is_active
                desc += f"🆔 `{p[3]}` | Сумма: **{p[4]}** | Каждые **{p[5]} {p[6]}**\nСледующий: `{p[7]}`\n\n"

        embed = discord.Embed(title="🔄 Автоплатежи", description=desc, color=discord.Color.orange())

        # ИСПРАВЛЕНИЕ: Передаем второй аргумент payments
        await interaction.response.edit_message(embed=embed, view=AutoPaymentsListView(self.user_data, payments))

    async def notif_callback(self, interaction: discord.Interaction):
        notes = user_notifications.get(self.user_data[0], [])
        if not notes:
            await interaction.response.send_message("🔕 Нет новых уведомлений.", ephemeral=True)
            return

        desc = ""
        for n in notes:
            status = "🔵" if not n['read'] else "⚪"
            desc += f"{status} [{n['time']}] {n['msg']}\n"

        # Помечаем все как прочитанные при просмотре
        user_notifications[self.user_data[0]] = [{"msg": x['msg'], "read": True, "time": x['time']} for x in notes]

        embed = discord.Embed(title="🔔 Уведомления", description=desc, color=discord.Color.blue())
        view = NotificationsView(self.user_data[0])
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def faction_callback(self, interaction: discord.Interaction):
        is_leader = any(role.name == LEADER_ROLE_NAME for role in interaction.user.roles)
        if not is_leader and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Доступ запрещен.", ephemeral=True);
            return
        my_prefix = self.user_data[5] if len(self.user_data) > 5 else str(self.user_data[3])[:2]
        members = await get_faction_members(my_prefix)
        if not members or len(members) <= 1:
            await interaction.response.send_message("ℹ️ Участников нет.", ephemeral=True);
            return
        embed = discord.Embed(title=f"🛡️ Фракция ({my_prefix})", color=discord.Color.blue())
        for m in members:
            if m[0] == self.user_data[0]: continue
            embed.add_field(name=f"🆔 `{m[3]}`", value=f"👤 `{m[2]}`\n📛 `{m[1]}`", inline=True)
        view_back = View(timeout=900)
        back_btn = Button(label="🔙 В профиль", style=ButtonStyle.red)

        async def back_cb(inter: discord.Interaction):
            emb = discord.Embed(title="🏦 Личный кабинет", color=discord.Color.green())
            emb.add_field(name="Пользователь", value=self.user_data[1], inline=False)
            emb.add_field(name="Ник", value=self.user_data[2], inline=True)
            emb.add_field(name="ID", value=f"`{self.user_data[3]}`", inline=True)
            emb.add_field(name="Баланс", value=f"**{self.user_data[4]}**", inline=False)
            await inter.response.edit_message(embed=emb, view=UserProfileView(self.user_data, is_leader=True))

        back_btn.callback = back_cb
        view_back.add_item(back_btn)
        tr_btn = Button(label="💸 Перевод", style=ButtonStyle.blurple)
        tr_btn.callback = self.transfer_callback
        view_back.add_item(tr_btn)
        await interaction.response.edit_message(embed=embed, view=view_back)


class AdminPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="Зачислить", style=ButtonStyle.green, emoji="➕")
    async def add_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ManageBalanceModal("add"))

    @button(label="Списать", style=ButtonStyle.red, emoji="➖")
    async def remove_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ManageBalanceModal("remove"))

    @button(label="Установить", style=ButtonStyle.blurple, emoji="⚙️")
    async def set_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ManageBalanceModal("set"))

    @button(label="📜 История", style=ButtonStyle.gray, emoji="🔍")
    async def history_btn(self, interaction: discord.Interaction, button: Button):
        if not any(role.name == ADMIN_ROLE_NAME for role in
                   interaction.user.roles) and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True);
            return
        await interaction.response.send_modal(TransactionFilterStep1())

    @button(label="Создать счет", style=ButtonStyle.green, emoji="📝")
    async def create_account_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("ℹ️ Используйте `/admin_create @user`", ephemeral=True)


# --- КОМАНДЫ ---

@bot.event
async def on_ready():
    print(f'Бот запущен: {bot.user}')
    await init_db()
    auto_payment_task.start()
    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано {len(synced)} команд.")
    except Exception as e:
        print(f"Ошибка синхронизации: {e}")


def check_admin_role(interaction: discord.Interaction) -> bool:
    if not interaction.guild: return False
    if interaction.user.id == interaction.guild.owner_id: return True
    return any(role.name == ADMIN_ROLE_NAME for role in interaction.user.roles)


@bot.tree.command(name="start", description="Личный кабинет")
async def start_cmd(interaction: discord.Interaction):
    user_data = await get_user_data(interaction.user.id)
    if not user_data:
        await interaction.response.send_message("❌ Счета нет.", ephemeral=True);
        return
    is_leader = (interaction.guild and (interaction.user.id == interaction.guild.owner_id or any(
        role.name == LEADER_ROLE_NAME for role in interaction.user.roles)))
    embed = discord.Embed(title="🏦 Личный кабинет", color=discord.Color.green())
    embed.add_field(name="Пользователь", value=user_data[1], inline=False)
    embed.add_field(name="Ник", value=user_data[2], inline=True)
    embed.add_field(name="ID", value=f"`{user_data[3]}`", inline=True)
    embed.add_field(name="Баланс", value=f"**{user_data[4]}**", inline=False)
    await interaction.response.send_message(embed=embed, view=UserProfileView(user_data, is_leader=is_leader),
                                            ephemeral=True)


@bot.tree.command(name="admin_panel", description="Админ панель")
@app_commands.check(check_admin_role)
async def admin_panel_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🛡️ Панель Администратора", color=discord.Color.red())
    embed.add_field(name="Управление", value="Используйте кнопки.", inline=False)
    await interaction.response.send_message(embed=embed, view=AdminPanelView(), ephemeral=True)


@bot.tree.command(name="admin_create", description="Создать счет")
@app_commands.describe(user="Пользователь")
@app_commands.check(check_admin_role)
async def admin_create_cmd(interaction: discord.Interaction, user: discord.Member):
    data = await get_user_data(user.id)
    if data:
        await interaction.response.send_message(f"⚠️ Счет уже есть: {data[3]}", ephemeral=True);
        return
    await interaction.response.send_modal(CreateAccountModal(user))


@bot.tree.command(name="admin_add", description="Зачислить")
@app_commands.describe(user="Пользователь")
@app_commands.check(check_admin_role)
async def admin_add_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(ManageBalanceModal("add"))


@bot.tree.command(name="admin_remove", description="Списать")
@app_commands.describe(user="Пользователь")
@app_commands.check(check_admin_role)
async def admin_remove_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(ManageBalanceModal("remove"))


@bot.tree.command(name="admin_set", description="Установить баланс")
@app_commands.describe(user="Пользователь")
@app_commands.check(check_admin_role)
async def admin_set_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(ManageBalanceModal("set"))


@admin_panel_cmd.error
@admin_create_cmd.error
@admin_add_cmd.error
@admin_remove_cmd.error
@admin_set_cmd.error
async def admin_error_handler(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Ошибка: {error}", ephemeral=True)


if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("Токен не найден!")