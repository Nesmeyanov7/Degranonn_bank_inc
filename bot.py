import os
import discord
from discord import app_commands, ButtonStyle
from discord.ui import View, Button, Modal, TextInput, button
from discord.ext import commands
from dotenv import load_dotenv
import aiosqlite
import datetime
import io
import csv

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DB_NAME = 'bank.db'
ADMIN_ROLE_NAME = "Техник"  # ЗАМЕНИ НА СВОЮ РОЛЬ
LEADER_ROLE_NAME = "Лидер Фракции"  # ЗАМЕНИ НА СВОЮ РОЛЬ

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)


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


# --- МОДАЛЬНЫЕ ОКНА (ОПЕРАЦИИ) ---

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


# --- ФИЛЬТРЫ ТРАНЗАКЦИЙ (ДВА ЭТАПА ЧЕРЕЗ КНОПКУ) ---

class TransactionFilterStep1(Modal, title="Фильтры (1/2)"):
    def __init__(self):
        super().__init__()
        self.type_input = TextInput(label="Тип (transfer/deposit...)", placeholder="Оставь пустым для всех",
                                    required=False, max_length=20)
        self.date_from = TextInput(label="Дата от (ГГГГ-ММ-ДД)", placeholder="2023-01-01", required=False,
                                   max_length=10)
        self.date_to = TextInput(label="Дата до (ГГГГ-ММ-ДД)", placeholder="2023-12-31", required=False, max_length=10)
        self.min_amount = TextInput(label="Мин. сумма", placeholder="0", required=False, max_length=10)
        self.max_amount = TextInput(label="Макс. сумма", placeholder="9999999", required=False, max_length=10)

        for item in [self.type_input, self.date_from, self.date_to, self.min_amount, self.max_amount]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        data = {
            'type': self.type_input.value,
            'd_from': self.date_from.value,
            'd_to': self.date_to.value,
            'min_a': self.min_amount.value,
            'max_a': self.max_amount.value
        }

        view = Step2LauncherView(data)
        await interaction.response.send_message(
            "🔽 Нажмите кнопку ниже, чтобы ввести ID счетов и завершить фильтр:",
            view=view,
            ephemeral=True
        )


class Step2LauncherView(View):
    def __init__(self, data):
        super().__init__(timeout=120)
        self.data = data

    @button(label="Заполнить остальное (ID)", style=ButtonStyle.primary)
    async def launch_step2(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(TransactionFilterStep2(self.data))


class TransactionFilterStep2(Modal, title="Фильтры (2/2)"):
    def __init__(self, step1_data):
        super().__init__()
        self.step1_data = step1_data

        self.sender_filter = TextInput(label="ID Отправителя", placeholder="Необязательно", required=False,
                                       max_length=15)
        self.receiver_filter = TextInput(label="ID Получателя", placeholder="Необязательно", required=False,
                                         max_length=15)
        self.tx_id_filter = TextInput(label="ID Транзакции", placeholder="Конкретный номер", required=False,
                                      max_length=10)

        for item in [self.sender_filter, self.receiver_filter, self.tx_id_filter]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        query = "SELECT * FROM transactions WHERE 1=1"
        params = []

        d = self.step1_data
        if d['type']:
            t = d['type'].lower()
            if 'перевод' in t or 'transfer' in t:
                val = 'user_transfer'
            elif 'зачисл' in t or 'deposit' in t:
                val = 'admin_deposit'
            elif 'сняти' in t or 'withdraw' in t:
                val = 'admin_withdraw'
            elif 'set' in t:
                val = 'admin_set'
            else:
                val = t
            query += " AND type = ?"
            params.append(val)

        if d['d_from']:
            query += " AND created_at >= ?"
            params.append(f"{d['d_from']} 00:00:00")
        if d['d_to']:
            query += " AND created_at <= ?"
            params.append(f"{d['d_to']} 23:59:59")

        if d['min_a'] and d['min_a'].isdigit():
            query += " AND amount >= ?"
            params.append(int(d['min_a']))
        if d['max_a'] and d['max_a'].isdigit():
            query += " AND amount <= ?"
            params.append(int(d['max_a']))

        if self.sender_filter.value:
            query += " AND sender_acc = ?"
            params.append(self.sender_filter.value)
        if self.receiver_filter.value:
            query += " AND receiver_acc = ?"
            params.append(self.receiver_filter.value)
        if self.tx_id_filter.value and self.tx_id_filter.value.isdigit():
            query += " AND id = ?"
            params.append(int(self.tx_id_filter.value))

        query += " ORDER BY id DESC LIMIT 100"

        try:
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()

            if not rows:
                await interaction.followup.send("❌ Ничего не найдено.", ephemeral=True)
                return

            total_pages = (len(rows) - 1) // 10 + 1
            await show_transaction_page(interaction, rows, 0, total_pages)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


# --- ПАГИНАЦИЯ И ВЫВОД ---

class TransactionPaginationView(View):
    def __init__(self, full_data, current_page, total_pages):
        super().__init__(timeout=120)
        self.full_data = full_data
        self.page = current_page
        self.total = total_pages
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == self.total - 1

    @button(label="⬅️ Назад", style=ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: Button):
        await show_transaction_page(interaction, self.full_data, self.page - 1, self.total)

    @button(label="➡️ Вперед", style=ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: Button):
        await show_transaction_page(interaction, self.full_data, self.page + 1, self.total)

    @button(label="📥 CSV", style=ButtonStyle.green, emoji="💾")
    async def export_btn(self, interaction: discord.Interaction, button: Button):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Дата", "Тип", "Отправитель", "Получатель", "Сумма"])
        for tx in self.full_data:
            writer.writerow(tx)
        file = discord.File(fp=io.BytesIO(output.getvalue().encode('utf-8-sig')), filename="transactions.csv")
        await interaction.response.send_message(file=file, ephemeral=True)


async def show_transaction_page(interaction, data, page, total_pages):
    start = page * 10
    end = start + 10
    items = data[start:end]

    desc = ""
    for tx in items:
        icon = "💸" if tx[2] == 'user_transfer' else "💰" if 'deposit' in tx[2] else "📉" if 'withdraw' in tx[2] else "⚙️"
        s_str = tx[3] if tx[3] else "—"
        r_str = tx[4] if tx[4] else "—"
        desc += f"**#{tx[0]:08d}** | {tx[1]} | {icon} `{tx[2]}`\nОт: `{s_str}` → Кому: `{r_str}` | Сумма: **{tx[5]}**\n\n"

    embed = discord.Embed(title="📜 История транзакций", description=desc, color=discord.Color.blue())
    embed.set_footer(text=f"Страница {page + 1}/{total_pages}")

    view = TransactionPaginationView(data, page, total_pages)

    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# --- ИНТЕРФЕЙСЫ ПОЛЬЗОВАТЕЛЯ И АДМИНА ---

class UserProfileView(View):
    def __init__(self, user_data, is_leader: bool = False):
        super().__init__(timeout=900)
        self.user_data = user_data

        transfer_btn = Button(label="Перевести средства", style=ButtonStyle.blurple, emoji="💸")
        transfer_btn.callback = self.transfer_callback
        self.add_item(transfer_btn)

        if is_leader:
            faction_btn = Button(label="Состав фракции", style=ButtonStyle.green, emoji="👥")
            faction_btn.callback = self.faction_callback
            self.add_item(faction_btn)

    async def transfer_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TransferModal())

    async def faction_callback(self, interaction: discord.Interaction):
        is_leader = any(role.name == LEADER_ROLE_NAME for role in interaction.user.roles)
        if not is_leader and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Доступ запрещен.", ephemeral=True)
            return

        my_prefix = self.user_data[5] if len(self.user_data) > 5 else str(self.user_data[3])[:2]
        members = await get_faction_members(my_prefix)

        if not members or len(members) <= 1:
            await interaction.response.send_message("ℹ️ Участников нет.", ephemeral=True)
            return

        embed = discord.Embed(title=f"🛡️ Фракция ({my_prefix})", color=discord.Color.blue())
        for m in members:
            if m[0] == self.user_data[0]: continue
            embed.add_field(name=f"🆔 `{m[3]}`", value=f"👤 `{m[2]}`\n📛 `{m[1]}`", inline=True)

        view_back = View(timeout=900)
        back_btn = Button(label="🔙 В профиль", style=ButtonStyle.red)

        async def back_callback(inter: discord.Interaction):
            emb = discord.Embed(title="🏦 Личный кабинет", color=discord.Color.green())
            emb.add_field(name="Пользователь", value=self.user_data[1], inline=False)
            emb.add_field(name="Ник", value=self.user_data[2], inline=True)
            emb.add_field(name="ID", value=f"`{self.user_data[3]}`", inline=True)
            emb.add_field(name="Баланс", value=f"**{self.user_data[4]}**", inline=False)
            await inter.response.edit_message(embed=emb, view=UserProfileView(self.user_data, is_leader=True))

        back_btn.callback = back_callback
        view_back.add_item(back_btn)

        tr_btn = Button(label="💸 Перевод", style=ButtonStyle.blurple)
        tr_btn.callback = self.transfer_callback
        view_back.add_item(tr_btn)

        await interaction.response.edit_message(embed=embed, view=view_back)


class AdminPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="Зачислить средства", style=ButtonStyle.green, emoji="➕")
    async def add_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ManageBalanceModal("add"))

    @button(label="Списать средства", style=ButtonStyle.red, emoji="➖")
    async def remove_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ManageBalanceModal("remove"))

    @button(label="Установить баланс", style=ButtonStyle.blurple, emoji="⚙️")
    async def set_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ManageBalanceModal("set"))

    @button(label="📜 История операций", style=ButtonStyle.gray, emoji="🔍")
    async def history_btn(self, interaction: discord.Interaction, button: Button):
        if not any(role.name == ADMIN_ROLE_NAME for role in
                   interaction.user.roles) and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        await interaction.response.send_modal(TransactionFilterStep1())

    @button(label="Создать счет", style=ButtonStyle.green, emoji="📝")
    async def create_account_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("ℹ️ Используйте команду `/admin_create @user`", ephemeral=True)


# --- КОМАНДЫ ---

@bot.event
async def on_ready():
    print(f'Бот запущен: {bot.user}')
    await init_db()
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
        await interaction.response.send_message("❌ Счета нет. Обратитесь к админу.", ephemeral=True)
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
    embed.add_field(name="Управление", value="Используйте кнопки ниже.", inline=False)
    await interaction.response.send_message(embed=embed, view=AdminPanelView(), ephemeral=True)


@bot.tree.command(name="admin_create", description="Создать счет")
@app_commands.describe(user="Пользователь")
@app_commands.check(check_admin_role)
async def admin_create_cmd(interaction: discord.Interaction, user: discord.Member):
    data = await get_user_data(user.id)
    if data:
        await interaction.response.send_message(f"⚠️ Счет уже есть: {data[3]}", ephemeral=True)
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
        await interaction.response.send_message("❌ Нет прав администратора.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Ошибка: {error}", ephemeral=True)


if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("Токен не найден!")